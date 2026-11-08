"""A small Discord REST client for the parts that run outside the gateway connection:
role changes from payment webhooks, mod-queue posts, the startup check and the role
reconcile. Uses the bot token, follows Discord's rate-limit headers, and turns error
bodies into DiscordError with Discord's own JSON error code."""
import asyncio
import time
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import httpx

# Permission bits (https://discord.com/developers/docs/topics/permissions)
VIEW_CHANNEL = 1 << 10
SEND_MESSAGES = 1 << 11
EMBED_LINKS = 1 << 14
ADMINISTRATOR = 1 << 3
MANAGE_ROLES = 1 << 28
MANAGE_MESSAGES = 1 << 13

# The bot asks for only what it uses: give/remove the Premium role, read and post in
# the mod-queue channel, and post embeds there.
BOT_PERMISSIONS = MANAGE_ROLES | VIEW_CHANNEL | SEND_MESSAGES | EMBED_LINKS

# JSON error codes this project reacts to.
UNKNOWN_MEMBER = 10007
UNKNOWN_ROLE = 10011
UNKNOWN_INTERACTION = 10062
MISSING_ACCESS = 50001
CANNOT_DM = 50007
MISSING_PERMISSIONS = 50013

NO_MENTIONS = {"parse": []}  # nothing the bot posts can ping @everyone, a role or a member


class DiscordError(Exception):
    def __init__(self, status: int, code: int, message: str, method: str = "", path: str = ""):
        super().__init__(f"{method} {path}: HTTP {status}, code {code}: {message}")
        self.status, self.code, self.message = status, code, message

    @property
    def transient(self) -> bool:
        """Worth retrying later (Discord down or rate limited), as opposed to a setup problem."""
        return self.status >= 500 or self.status == 429 or self.status == 0


def invite_url(application_id: str, guild_id: Optional[str] = None) -> str:
    url = (f"https://discord.com/oauth2/authorize?client_id={application_id}"
           f"&scope=bot%20applications.commands&permissions={BOT_PERMISSIONS}")
    if guild_id:
        url += f"&guild_id={guild_id}&disable_guild_select=true"
    return url


class DiscordREST:
    def __init__(self, token: str, base: str = "https://discord.com/api/v10", client: Optional[httpx.AsyncClient] = None,
                 max_retries: int = 4, sleep=asyncio.sleep, timeout: float = 15.0):
        self.base = base.rstrip("/")
        self._token = token
        self._client = client
        self._own_client = client is None
        self.max_retries = max_retries
        self.sleep = sleep
        self.timeout = timeout
        self._bucket_for_route: Dict[str, str] = {}
        self._bucket_reset: Dict[str, float] = {}

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    async def aclose(self):
        if self._client is not None and self._own_client:
            await self._client.aclose()
            self._client = None

    async def request(self, method: str, path: str, route: Optional[str] = None, json: Any = None,
                      params: Optional[dict] = None, reason: Optional[str] = None) -> Any:
        route = route or path
        headers = {"Authorization": f"Bot {self._token}",
                   "User-Agent": "DiscordBot (https://github.com/ParishSynthMind/discord-ai-community-bot, 0.1)"}
        if reason:
            headers["X-Audit-Log-Reason"] = quote(reason[:512], safe=" ")
        client = await self._http()
        for attempt in range(self.max_retries + 1):
            bucket = self._bucket_for_route.get(f"{method} {route}")
            wait = self._bucket_reset.get(bucket, 0) - time.monotonic() if bucket else 0
            if wait > 0:
                await self.sleep(wait)
            try:
                resp = await client.request(method, self.base + path, json=json, params=params, headers=headers)
            except httpx.HTTPError as exc:
                if attempt < self.max_retries:
                    await self.sleep(min(2 ** attempt, 10))
                    continue
                raise DiscordError(0, 0, f"network error: {exc}", method, path) from exc
            self._note_bucket(method, route, resp)
            if resp.status_code == 429:
                body = _json(resp)
                retry_after = float(body.get("retry_after") or resp.headers.get("Retry-After") or 1)
                if attempt < self.max_retries:
                    await self.sleep(retry_after)
                    continue
                raise DiscordError(429, int(body.get("code") or 0), body.get("message", "rate limited"), method, path)
            if resp.status_code >= 500 and attempt < self.max_retries:
                await self.sleep(min(2 ** attempt, 10))
                continue
            if resp.status_code >= 400:
                body = _json(resp)
                raise DiscordError(resp.status_code, int(body.get("code") or 0),
                                   str(body.get("message") or resp.text[:200]), method, path)
            return None if resp.status_code == 204 or not resp.content else resp.json()
        raise DiscordError(0, 0, "retries exhausted", method, path)  # pragma: no cover

    def _note_bucket(self, method: str, route: str, resp: httpx.Response):
        bucket = resp.headers.get("X-RateLimit-Bucket")
        if not bucket:
            return
        self._bucket_for_route[f"{method} {route}"] = bucket
        if resp.headers.get("X-RateLimit-Remaining") == "0":
            self._bucket_reset[bucket] = time.monotonic() + float(resp.headers.get("X-RateLimit-Reset-After") or 0)
        else:
            self._bucket_reset.pop(bucket, None)

    # --- calls used by this project
    async def me(self) -> dict:
        return await self.request("GET", "/users/@me")

    async def application(self) -> dict:
        return await self.request("GET", "/oauth2/applications/@me")

    async def guild(self, guild_id: str) -> dict:
        return await self.request("GET", f"/guilds/{guild_id}", route="/guilds/{g}")

    async def roles(self, guild_id: str) -> List[dict]:
        return await self.request("GET", f"/guilds/{guild_id}/roles", route="/guilds/{g}/roles")

    async def member(self, guild_id: str, user_id: str) -> dict:
        return await self.request("GET", f"/guilds/{guild_id}/members/{user_id}", route="/guilds/{g}/members/{u}")

    async def list_members(self, guild_id: str, page: int = 1000) -> List[dict]:
        out, after = [], "0"
        while True:
            batch = await self.request("GET", f"/guilds/{guild_id}/members", route="/guilds/{g}/members",
                                       params={"limit": page, "after": after})
            out.extend(batch)
            if len(batch) < page:
                return out
            after = batch[-1]["user"]["id"]

    async def add_role(self, guild_id: str, user_id: str, role_id: str, reason: str):
        await self.request("PUT", f"/guilds/{guild_id}/members/{user_id}/roles/{role_id}",
                           route="/guilds/{g}/members/{u}/roles/{r}", reason=reason)

    async def remove_role(self, guild_id: str, user_id: str, role_id: str, reason: str):
        await self.request("DELETE", f"/guilds/{guild_id}/members/{user_id}/roles/{role_id}",
                           route="/guilds/{g}/members/{u}/roles/{r}", reason=reason)

    async def channel(self, channel_id: str) -> dict:
        return await self.request("GET", f"/channels/{channel_id}", route="/channels/{c}")

    async def send_message(self, channel_id: str, content: str = "", embeds: Optional[list] = None) -> dict:
        payload: Dict[str, Any] = {"content": content[:2000], "allowed_mentions": NO_MENTIONS}
        if embeds:
            payload["embeds"] = embeds
        return await self.request("POST", f"/channels/{channel_id}/messages", route="/channels/{c}/messages",
                                  json=payload)

    async def dm(self, user_id: str, content: str) -> dict:
        channel = await self.request("POST", "/users/@me/channels", json={"recipient_id": user_id})
        return await self.send_message(channel["id"], content)


def _json(resp: httpx.Response) -> dict:
    try:
        data = resp.json()
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


def role_rank(role: dict) -> tuple:
    """Where a role sits in the hierarchy. Discord gives new roles the same position
    number and breaks the tie by id: the older role (lower id) ranks higher."""
    return (int(role.get("position", 0)), -int(role["id"]))


def role_position(roles: List[dict], role_ids) -> tuple:
    """Rank of the highest of these roles (compare with role_rank, never raw positions)."""
    ranks = [role_rank(r) for r in roles if r["id"] in set(role_ids)]
    return max(ranks, default=(0, 0))


def guild_permissions(guild: dict, roles: List[dict], member_role_ids) -> int:
    """Base permissions of a member: @everyone (role id == guild id) plus their roles."""
    perms = 0
    by_id = {r["id"]: r for r in roles}
    for rid in [guild["id"], *member_role_ids]:
        if rid in by_id:
            perms |= int(by_id[rid]["permissions"])
    if perms & ADMINISTRATOR:
        return ~0
    return perms


def channel_permissions(guild: dict, roles: List[dict], member_role_ids, channel: dict, user_id: str) -> int:
    """Discord's own order: @everyone, the member's roles, then channel overwrites for
    @everyone, for their roles, then for the member."""
    perms = guild_permissions(guild, roles, member_role_ids)
    if perms == ~0:
        return perms
    ows = {o["id"]: o for o in channel.get("permission_overwrites") or []}
    if guild["id"] in ows:
        perms = (perms & ~int(ows[guild["id"]]["deny"])) | int(ows[guild["id"]]["allow"])
    allow = deny = 0
    for rid in member_role_ids:
        if rid in ows:
            allow |= int(ows[rid]["allow"])
            deny |= int(ows[rid]["deny"])
    perms = (perms & ~deny) | allow
    if user_id in ows:
        perms = (perms & ~int(ows[user_id]["deny"])) | int(ows[user_id]["allow"])
    return perms
