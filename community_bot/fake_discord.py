"""A fake Discord for tests and the local demo: the REST API (v10) and the gateway
websocket, for the calls this project and discord.py make, answering with Discord's JSON
shapes and Discord's error codes. The real discord.py library connects to it unchanged:
it logs in, identifies with its intents, receives READY and GUILD_CREATE, syncs slash
commands, and answers interactions, exactly as it would against discord.com.

Rules it enforces, because they are the ones that catch people out:
- a wrong token is 401 (REST) or close code 4004 (gateway);
- asking for a privileged intent (Server Members, Presence, Message Content) that is
  not switched on for the app closes the gateway with 4014 "Disallowed intent(s)";
- listing members needs the Server Members intent switched on (50001 Missing Access);
- giving or taking a role needs Manage Roles AND the bot's highest role above that role
  (50013 Missing Permissions); managed (integration) roles can't be given at all;
- unknown members, roles and channels are 10007, 10011, 10003;
- posting needs View Channel + Send Messages, channel overwrites included (50001 / 50013);
- message content over 2000 characters, or an empty message, is 50035 / 50006;
- an interaction must be answered within 3 seconds and only once (10062, 40060);
  follow-ups need an acknowledged interaction and a token under 15 minutes old (50027);
- per-route rate limits with Discord's X-RateLimit-* headers, and 429 bodies with retry_after;
- DMs to someone who blocks them are 50007.
Failures (a 500, a 429) can be injected on any route to test retries."""
import asyncio
import itertools
import json
import re
import time
import zlib
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import unquote

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

VIEW_CHANNEL, SEND_MESSAGES, EMBED_LINKS = 1 << 10, 1 << 11, 1 << 14
MANAGE_ROLES, ADMINISTRATOR, MANAGE_MESSAGES = 1 << 28, 1 << 3, 1 << 13
INTENT_GUILDS, INTENT_MEMBERS, INTENT_PRESENCES, INTENT_MESSAGE_CONTENT = 1, 1 << 1, 1 << 8, 1 << 15
PRIVILEGED = {INTENT_MEMBERS: "members", INTENT_PRESENCES: "presence", INTENT_MESSAGE_CONTENT: "message_content"}
DISCORD_EPOCH = 1420070400000
NAME_RE = re.compile(r"^[-_\w]{1,32}$")

_counter = itertools.count(1)


def snowflake() -> str:
    return str(((int(time.time() * 1000) - DISCORD_EPOCH) << 22) | (next(_counter) & 0x3FFFFF))


def iso(ts: Optional[float] = None) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts or time.time(), tz=timezone.utc).isoformat()


def user_obj(uid: str, username: str, bot: bool = False) -> dict:
    u = {"id": uid, "username": username, "discriminator": "0", "global_name": username, "avatar": None,
         "public_flags": 0, "flags": 0, "banner": None, "accent_color": None, "avatar_decoration_data": None}
    if bot:
        u["bot"] = True
    return u


class FakeError(Exception):
    def __init__(self, status: int, code: int, message: str, errors: Optional[dict] = None):
        self.status, self.code, self.message, self.errors = status, code, message, errors


@dataclass
class Interaction:
    id: str
    token: str
    payload: dict
    created: float
    response_type: Optional[int] = None
    response: Optional[dict] = None
    original: Optional[dict] = None
    followups: List[dict] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)


class FakeDiscord:
    def __init__(self, token: str = "fake-bot-token", app_id: Optional[str] = None,
                 privileged_intents: tuple = ("members",), interaction_window: float = 3.0):
        self.token = token
        self.app_id = app_id or snowflake()
        self.bot_user = user_obj(self.app_id, "Pixelforge Helper", bot=True)
        self.privileged_enabled = set(privileged_intents)
        self.interaction_window = interaction_window
        self.guilds: Dict[str, dict] = {}
        self.members: Dict[str, Dict[str, dict]] = {}
        self.messages: Dict[str, List[dict]] = {}
        self.dm_channels: Dict[str, str] = {}
        self.dm_blocked: set = set()
        self.commands: Dict[str, List[dict]] = {}
        self.interactions: Dict[str, Interaction] = {}
        self.audit_log: List[dict] = []
        self.requests: List[dict] = []
        self.injected: List[dict] = []
        self.limits: Dict[str, tuple] = {"role": (10, 10.0), "message": (5, 5.0)}
        self._windows: Dict[str, List[float]] = {}
        self.sessions: List["GatewaySession"] = []
        self.identify_log: List[dict] = []
        self.base_url = ""  # set by serve()
        self.app = Starlette(routes=[
            Route("/api/v10/{path:path}", self._rest, methods=["GET", "POST", "PUT", "PATCH", "DELETE"]),
            WebSocketRoute("/gateway", self._gateway),
        ])

    # ------------------------------------------------------------------ setup helpers
    def add_guild(self, guild_id: str, name: str, owner_id: Optional[str] = None) -> dict:
        g = {"id": guild_id, "name": name, "owner_id": owner_id or snowflake(), "roles": [], "channels": []}
        self.guilds[guild_id] = g
        self.members[guild_id] = {}
        self.add_role(guild_id, "@everyone", position=0, permissions=VIEW_CHANNEL | SEND_MESSAGES, role_id=guild_id)
        return g

    def add_role(self, guild_id: str, name: str, position: int, permissions: int = 0, role_id: Optional[str] = None,
                 managed: bool = False) -> dict:
        role = {"id": role_id or snowflake(), "name": name, "color": 0,
                "colors": {"primary_color": 0, "secondary_color": None, "tertiary_color": None},
                "hoist": False, "icon": None, "unicode_emoji": None, "position": position,
                "permissions": str(permissions), "managed": managed, "mentionable": False, "flags": 0}
        if managed:
            role["tags"] = {"bot_id": self.app_id}
        self.guilds[guild_id]["roles"].append(role)
        return role

    def add_channel(self, guild_id: str, name: str, channel_id: Optional[str] = None,
                    overwrites: Optional[list] = None) -> dict:
        ch = {"id": channel_id or snowflake(), "type": 0, "guild_id": guild_id, "name": name,
              "position": len(self.guilds[guild_id]["channels"]), "permission_overwrites": overwrites or [],
              "nsfw": False, "parent_id": None, "topic": None, "rate_limit_per_user": 0, "last_message_id": None,
              "flags": 0}
        self.guilds[guild_id]["channels"].append(ch)
        self.messages[ch["id"]] = []
        return ch

    def add_member(self, guild_id: str, user_id: str, username: str, roles: Optional[list] = None,
                   bot: bool = False) -> dict:
        m = {"user": user_obj(user_id, username, bot), "nick": None, "roles": list(roles or []),
             "joined_at": iso(), "premium_since": None, "deaf": False, "mute": False, "flags": 0,
             "pending": False, "avatar": None, "banner": None, "communication_disabled_until": None}
        self.members[guild_id][user_id] = m
        return m

    def add_bot_to_guild(self, guild_id: str, role_position: int, permissions: int) -> dict:
        """What the OAuth2 invite does: the bot joins with its own managed role."""
        role = self.add_role(guild_id, self.bot_user["username"], role_position, permissions, managed=True)
        self.add_member(guild_id, self.app_id, self.bot_user["username"], roles=[role["id"]], bot=True)
        return role

    def role(self, guild_id: str, role_id: str) -> Optional[dict]:
        return next((r for r in self.guilds[guild_id]["roles"] if r["id"] == role_id), None)

    def member_roles(self, guild_id: str, user_id: str) -> list:
        return list(self.members[guild_id][user_id]["roles"])

    def inject(self, method: str, path_pattern: str, status: int, body: Optional[dict] = None, times: int = 1,
               headers: Optional[dict] = None):
        self.injected.append({"method": method, "re": re.compile(path_pattern), "status": status,
                              "body": body or {"message": "injected", "code": 0}, "times": times,
                              "headers": headers or {}})

    # ------------------------------------------------------------------ permissions
    def _base_perms(self, guild_id: str, user_id: str) -> int:
        g = self.guilds[guild_id]
        if g["owner_id"] == user_id:
            return ~0
        roles = {r["id"]: r for r in g["roles"]}
        perms = int(roles[guild_id]["permissions"])
        for rid in self.members[guild_id][user_id]["roles"]:
            if rid in roles:
                perms |= int(roles[rid]["permissions"])
        return ~0 if perms & ADMINISTRATOR else perms

    def _channel_perms(self, ch: dict, user_id: str) -> int:
        gid = ch["guild_id"]
        perms = self._base_perms(gid, user_id)
        if perms == ~0:
            return perms
        ows = {o["id"]: o for o in ch["permission_overwrites"]}
        if gid in ows:
            perms = (perms & ~int(ows[gid]["deny"])) | int(ows[gid]["allow"])
        allow = deny = 0
        for rid in self.members[gid][user_id]["roles"]:
            if rid in ows:
                allow |= int(ows[rid]["allow"])
                deny |= int(ows[rid]["deny"])
        perms = (perms & ~deny) | allow
        if user_id in ows:
            perms = (perms & ~int(ows[user_id]["deny"])) | int(ows[user_id]["allow"])
        return perms

    def _top_position(self, guild_id: str, user_id: str) -> int:
        pos = {r["id"]: r["position"] for r in self.guilds[guild_id]["roles"]}
        return max((pos.get(r, 0) for r in self.members[guild_id][user_id]["roles"]), default=0)

    # ------------------------------------------------------------------ REST plumbing
    def _rate(self, bucket_key: str, kind: str):
        limit, window = self.limits[kind]
        now = time.monotonic()
        hits = [t for t in self._windows.get(bucket_key, []) if now - t < window]
        if len(hits) >= limit:
            retry = round(window - (now - hits[0]), 3)
            raise FakeError(429, 0, "You are being rate limited.", {"retry_after": retry, "global": False})
        hits.append(now)
        self._windows[bucket_key] = hits
        reset_after = round(window - (now - hits[0]), 3)
        return {"X-RateLimit-Limit": str(limit), "X-RateLimit-Remaining": str(limit - len(hits)),
                "X-RateLimit-Reset-After": str(reset_after), "X-RateLimit-Reset": f"{time.time() + reset_after:.3f}",
                "X-RateLimit-Bucket": f"{kind}-{abs(hash(bucket_key)) % 10**8:08d}"}

    async def _rest(self, request: Request) -> Response:
        path = "/" + request.path_params["path"]
        method = request.method
        raw = await request.body()
        entry = {"method": method, "path": path, "at": time.time(),
                 "reason": unquote(request.headers.get("x-audit-log-reason", "")) or None}
        self.requests.append(entry)
        for inj in self.injected:
            if inj["times"] > 0 and inj["method"] == method and inj["re"].search(path):
                inj["times"] -= 1
                entry["status"] = inj["status"]
                return JSONResponse(inj["body"], inj["status"], headers=inj["headers"])
        headers: Dict[str, str] = {}
        try:
            body = json.loads(raw) if raw else None
            if not path.startswith(("/interactions/", "/webhooks/")):
                if request.headers.get("authorization") != f"Bot {self.token}":
                    raise FakeError(401, 0, "401: Unauthorized")
            status, data, headers = await self._handle(method, path, body, request, entry)
        except FakeError as e:
            entry["status"] = e.status
            if e.status == 429:
                retry = e.errors["retry_after"]
                return JSONResponse({"message": e.message, "retry_after": retry, "global": False}, 429,
                                    headers={"Retry-After": str(max(1, int(retry + 0.999))),
                                             "X-RateLimit-Scope": "user", **headers})
            out = {"message": e.message, "code": e.code}
            if e.errors and e.status != 429:
                out["errors"] = e.errors
            return JSONResponse(out, e.status, headers=headers)
        except json.JSONDecodeError:
            entry["status"] = 400
            return JSONResponse({"message": "400: Bad Request", "code": 50109}, 400)
        entry["status"] = status
        if status == 204:
            return Response(status_code=204, headers=headers)
        return JSONResponse(data, status, headers=headers)

    async def _handle(self, method: str, path: str, body: Any, request: Request, entry: dict):
        m = lambda pattern: re.fullmatch(pattern, path)  # noqa: E731
        if method == "GET" and path == "/users/@me":
            return 200, self.bot_user, {}
        if method == "GET" and path == "/oauth2/applications/@me":
            return 200, self.application(), {}
        if method == "GET" and path in ("/gateway/bot", "/gateway"):
            return 200, {"url": self.base_url.replace("http", "ws", 1) + "/gateway", "shards": 1,
                         "session_start_limit": {"total": 1000, "remaining": 999, "reset_after": 0,
                                                 "max_concurrency": 1}}, {}
        if (r := m(r"/applications/(\d+)/guilds/(\d+)/commands")):
            self._need_guild(r.group(2))
            if method == "PUT":
                return 200, self._put_commands(r.group(2), body), {}
            return 200, self.commands.get(r.group(2), []), {}
        if (r := m(r"/applications/(\d+)/commands")):
            return 200, self._put_commands("global", body) if method == "PUT" else self.commands.get("global", []), {}
        if (r := m(r"/guilds/(\d+)")) and method == "GET":
            g = self._need_guild(r.group(1))
            return 200, {**self._guild_core(g), "roles": g["roles"]}, {}
        if (r := m(r"/guilds/(\d+)/roles")) and method == "GET":
            return 200, self._need_guild(r.group(1))["roles"], {}
        if (r := m(r"/guilds/(\d+)/members")) and method == "GET":
            self._need_guild(r.group(1))
            if "members" not in self.privileged_enabled:
                raise FakeError(403, 50001, "Missing Access")
            limit = min(int(request.query_params.get("limit", 1)), 1000)
            after = int(request.query_params.get("after", 0))
            ms = sorted(self.members[r.group(1)].values(), key=lambda x: int(x["user"]["id"]))
            return 200, [x for x in ms if int(x["user"]["id"]) > after][:limit], {}
        if (r := m(r"/guilds/(\d+)/members/(\d+)")) and method == "GET":
            self._need_guild(r.group(1))
            member = self.members[r.group(1)].get(r.group(2))
            if not member:
                raise FakeError(404, 10007, "Unknown Member")
            return 200, member, {}
        if (r := m(r"/guilds/(\d+)/members/(\d+)/roles/(\d+)")) and method in ("PUT", "DELETE"):
            return self._modify_role(method, r.group(1), r.group(2), r.group(3), entry)
        if (r := m(r"/channels/(\d+)")) and method == "GET":
            return 200, self._need_channel(r.group(1)), {}
        if (r := m(r"/channels/(\d+)/messages")) and method == "POST":
            return self._post_message(r.group(1), body or {})
        if path == "/users/@me/channels" and method == "POST":
            uid = str((body or {}).get("recipient_id"))
            cid = self.dm_channels.setdefault(uid, snowflake())
            self.messages.setdefault(cid, [])
            return 200, {"id": cid, "type": 1, "recipients": [user_obj(uid, f"user{uid[-4:]}")],
                         "last_message_id": None, "flags": 0}, {}
        if (r := m(r"/interactions/(\d+)/([^/]+)/callback")) and method == "POST":
            return self._callback(r.group(1), r.group(2), body or {}, request.query_params.get("with_response"))
        if (r := m(r"/webhooks/(\d+)/([^/]+)")) and method == "POST":
            return self._followup(r.group(1), r.group(2), body or {})
        if (r := m(r"/webhooks/(\d+)/([^/]+)/messages/@original")):
            return self._original(method, r.group(2), body or {})
        raise FakeError(404, 0, "404: Not Found")

    # ------------------------------------------------------------------ REST handlers
    def application(self) -> dict:
        flags = 0
        if "members" in self.privileged_enabled:
            flags |= 1 << 15  # GATEWAY_GUILD_MEMBERS_LIMITED
        return {"id": self.app_id, "name": self.bot_user["username"], "icon": None, "description": "",
                "rpc_origins": [], "bot_public": False, "bot_require_code_grant": False,
                "bot": self.bot_user, "owner": user_obj("1100000000000000001", "owner"), "summary": "",
                "verify_key": "0" * 64, "team": None, "flags": flags, "permissions": "0",
                "interactions_endpoint_url": None, "redirect_uris": [], "tags": [], "approximate_guild_count": 1}

    def _need_guild(self, gid: str) -> dict:
        g = self.guilds.get(gid)
        if not g:
            raise FakeError(404, 10004, "Unknown Guild")
        if self.app_id not in self.members[gid]:
            raise FakeError(403, 50001, "Missing Access")
        return g

    def _need_channel(self, cid: str) -> dict:
        for g in self.guilds.values():
            for ch in g["channels"]:
                if ch["id"] == cid:
                    if not self._channel_perms(ch, self.app_id) & VIEW_CHANNEL:
                        raise FakeError(403, 50001, "Missing Access")
                    return ch
        if cid in self.dm_channels.values():
            return {"id": cid, "type": 1}
        raise FakeError(404, 10003, "Unknown Channel")

    def _put_commands(self, scope: str, body: list) -> list:
        errors = {}
        for i, cmd in enumerate(body or []):
            if not NAME_RE.match(cmd.get("name", "")) or cmd["name"] != cmd["name"].lower():
                errors[str(i)] = {"name": {"_errors": [{"code": "APPLICATION_COMMAND_INVALID_NAME",
                                                        "message": "Command name is invalid"}]}}
            if not 1 <= len(cmd.get("description", "")) <= 100:
                errors[str(i)] = {"description": {"_errors": [{"code": "BASE_TYPE_BAD_LENGTH",
                                                               "message": "Must be between 1 and 100 in length."}]}}
            seen_optional = False
            for opt in cmd.get("options") or []:
                if not opt.get("required"):
                    seen_optional = True
                elif seen_optional:
                    errors[str(i)] = {"options": {"_errors": [{"code": "APPLICATION_COMMAND_OPTIONS_REQUIRED_INVALID",
                                                               "message": "Required options must be placed before "
                                                                          "non-required options"}]}}
        if errors:
            raise FakeError(400, 50035, "Invalid Form Body", errors)
        out = []
        for cmd in body or []:
            c = {"id": snowflake(), "application_id": self.app_id, "version": snowflake(), "type": 1,
                 "default_member_permissions": None, "nsfw": False, "name_localizations": None,
                 "description_localizations": None, "contexts": None, "integration_types": [0], **cmd}
            if c.get("default_member_permissions") is not None:  # accepted as a number, returned as a string
                c["default_member_permissions"] = str(c["default_member_permissions"])
            if scope != "global":
                c["guild_id"] = scope
            out.append(c)
        self.commands[scope] = out
        return out

    def _modify_role(self, method: str, gid: str, uid: str, rid: str, entry: dict):
        g = self._need_guild(gid)
        headers = self._rate(f"role:{gid}", "role")
        member = self.members[gid].get(uid)
        if not member:
            raise FakeError(404, 10007, "Unknown Member")
        role = self.role(gid, rid)
        if not role:
            raise FakeError(404, 10011, "Unknown Role")
        perms = self._base_perms(gid, self.app_id)
        if not perms & MANAGE_ROLES:
            raise FakeError(403, 50013, "Missing Permissions")
        if role["managed"] or role["id"] == g["id"] or (perms != ~0 and role["position"] >= self._top_position(gid, self.app_id)):
            raise FakeError(403, 50013, "Missing Permissions")
        if method == "PUT" and rid not in member["roles"]:
            member["roles"].append(rid)
            self._log_audit(gid, 25, uid, {"key": "$add", "new_value": [{"id": rid, "name": role["name"]}]}, entry)
            self._dispatch_member_update(gid, member)
        elif method == "DELETE" and rid in member["roles"]:
            member["roles"].remove(rid)
            self._log_audit(gid, 25, uid, {"key": "$remove", "new_value": [{"id": rid, "name": role["name"]}]}, entry)
            self._dispatch_member_update(gid, member)
        return 204, None, headers

    def _log_audit(self, gid, action_type, target_id, change, entry):
        self.audit_log.append({"guild_id": gid, "action_type": action_type, "target_id": target_id,
                               "user_id": self.app_id, "changes": [change], "reason": entry.get("reason")})

    def _message(self, cid: str, body: dict, webhook: bool = False, flags: int = 0) -> dict:
        msg = {"id": snowflake(), "channel_id": cid, "author": self.bot_user, "content": body.get("content") or "",
               "timestamp": iso(), "edited_timestamp": None, "tts": False, "mention_everyone": False,
               "mentions": [], "mention_roles": [], "attachments": [], "embeds": body.get("embeds") or [],
               "pinned": False, "type": 0, "flags": flags | int(body.get("flags") or 0), "components": [],
               "allowed_mentions": body.get("allowed_mentions")}
        if webhook:
            msg["webhook_id"] = self.app_id
            msg["application_id"] = self.app_id
        return msg

    def _validate_content(self, body: dict):
        content = body.get("content") or ""
        if len(content) > 2000:
            raise FakeError(400, 50035, "Invalid Form Body", {"content": {"_errors": [
                {"code": "BASE_TYPE_MAX_LENGTH", "message": "Must be 2000 or fewer in length."}]}})
        embeds = body.get("embeds") or []
        if not content and not embeds:
            raise FakeError(400, 50006, "Cannot send an empty message")
        if len(embeds) > 10 or sum(len(json.dumps(e)) for e in embeds) > 12000:
            raise FakeError(400, 50035, "Invalid Form Body", {"embeds": {"_errors": [
                {"code": "BASE_TYPE_MAX_LENGTH", "message": "Embed size exceeds maximum size of 6000"}]}})

    def _post_message(self, cid: str, body: dict):
        ch = self._need_channel(cid)
        headers = self._rate(f"msg:{cid}", "message")
        if ch.get("type") == 1:
            uid = next((u for u, c in self.dm_channels.items() if c == cid), None)
            if uid in self.dm_blocked:
                raise FakeError(403, 50007, "Cannot send messages to this user")
        elif not self._channel_perms(ch, self.app_id) & SEND_MESSAGES:
            raise FakeError(403, 50013, "Missing Permissions")
        self._validate_content(body)
        msg = self._message(cid, body)
        self.messages[cid].append(msg)
        return 200, msg, headers

    def _interaction_by_token(self, token: str) -> Interaction:
        it = next((i for i in self.interactions.values() if i.token == token), None)
        if not it:
            raise FakeError(401, 50027, "Invalid Webhook Token")
        if time.monotonic() - it.created > 15 * 60:
            raise FakeError(401, 50027, "Invalid Webhook Token")
        return it

    def _callback(self, iid: str, token: str, body: dict, with_response):
        it = self.interactions.get(iid)
        if not it or it.token != token or time.monotonic() - it.created > self.interaction_window:
            if it:
                it.errors.append("10062 Unknown interaction (answered too late)")
            raise FakeError(404, 10062, "Unknown interaction")
        if it.response_type is not None:
            it.errors.append("40060 already acknowledged")
            raise FakeError(400, 40060, "Interaction has already been acknowledged.")
        rtype = int(body.get("type", 0))
        data = body.get("data") or {}
        flags = int(data.get("flags") or 0)
        it.response_type, it.response = rtype, body
        msg = None
        if rtype == 4:
            self._validate_content(data)
            msg = self._message(it.payload["channel_id"], data, webhook=True, flags=flags)
            it.original = msg
        elif rtype == 5:
            msg = self._message(it.payload["channel_id"], {"content": ""}, webhook=True, flags=flags | 128)
            it.original = msg
        if not with_response:
            return 204, None, {}
        out = {"interaction": {"id": iid, "type": it.payload["type"], "response_message_id": msg["id"] if msg else None,
                               "response_message_loading": rtype == 5,
                               "response_message_ephemeral": bool(flags & 64)}}
        if msg:
            out["resource"] = {"type": rtype, "message": msg}
        return 200, out, {}

    def _followup(self, app_id: str, token: str, body: dict):
        it = self._interaction_by_token(token)
        if it.response_type is None:
            raise FakeError(404, 10015, "Unknown Webhook")
        self._validate_content(body)
        msg = self._message(it.payload["channel_id"], body, webhook=True)
        it.followups.append(msg)
        return 200, msg, {}

    def _original(self, method: str, token: str, body: dict):
        it = self._interaction_by_token(token)
        if not it.original:
            raise FakeError(404, 10008, "Unknown Message")
        if method == "PATCH":
            self._validate_content({**it.original, **body})
            it.original = {**it.original, **body, "edited_timestamp": iso()}
        return 200, it.original, {}

    # ------------------------------------------------------------------ gateway
    def _guild_core(self, g: dict) -> dict:
        return {"id": g["id"], "name": g["name"], "icon": None, "splash": None, "discovery_splash": None,
                "owner_id": g["owner_id"], "afk_channel_id": None, "afk_timeout": 300, "verification_level": 0,
                "default_message_notifications": 0, "explicit_content_filter": 0, "emojis": [], "stickers": [],
                "features": [], "mfa_level": 0, "application_id": None, "system_channel_id": None,
                "system_channel_flags": 0, "rules_channel_id": None, "vanity_url_code": None, "description": None,
                "banner": None, "premium_tier": 0, "preferred_locale": "en-US", "public_updates_channel_id": None,
                "nsfw_level": 0, "premium_progress_bar_enabled": False, "safety_alerts_channel_id": None}

    def guild_create(self, gid: str, intents: int) -> dict:
        g = self.guilds[gid]
        ms = self.members[gid]
        members = list(ms.values()) if intents & INTENT_MEMBERS else [ms[self.app_id]]
        return {**self._guild_core(g), "roles": g["roles"], "channels": g["channels"], "threads": [],
                "members": members, "member_count": len(ms), "large": False, "unavailable": False,
                "joined_at": ms[self.app_id]["joined_at"], "presences": [], "voice_states": [],
                "stage_instances": [], "guild_scheduled_events": [], "soundboard_sounds": []}

    async def _gateway(self, ws: WebSocket):
        await ws.accept()
        session = GatewaySession(self, ws, ws.query_params.get("compress"))
        self.sessions.append(session)
        try:
            await session.run()
        finally:
            if session in self.sessions:
                self.sessions.remove(session)

    async def dispatch(self, event: str, data: dict, needs_intent: int = 0):
        for s in list(self.sessions):
            if s.ready and (not needs_intent or s.intents & needs_intent):
                await s.send({"op": 0, "t": event, "d": data})

    def _dispatch_member_update(self, gid: str, member: dict):
        payload = {"guild_id": gid, **member}
        for s in list(self.sessions):
            if s.ready and s.intents & INTENT_MEMBERS:
                asyncio.get_event_loop().create_task(s.send({"op": 0, "t": "GUILD_MEMBER_UPDATE", "d": payload}))

    async def wait_ready(self, timeout: float = 10.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if any(s.ready for s in self.sessions):
                return
            await asyncio.sleep(0.02)
        raise TimeoutError("no gateway session became ready")

    # ------------------------------------------------------------------ test drivers
    async def member_join(self, gid: str, uid: str, username: str):
        m = self.add_member(gid, uid, username)
        await self.dispatch("GUILD_MEMBER_ADD", {"guild_id": gid, **m}, needs_intent=INTENT_MEMBERS)

    async def run_command(self, gid: str, channel_id: str, user_id: str, name: str, options: Optional[dict] = None,
                          permissions: Optional[int] = None) -> Interaction:
        """A member runs a slash command: Discord sends INTERACTION_CREATE to the bot."""
        cmd = next((c for c in self.commands.get(gid, []) if c["name"] == name), None)
        if not cmd:
            raise KeyError(f"/{name} is not registered in guild {gid}")
        member = self.members[gid][user_id]
        perms = self._base_perms(gid, user_id) if permissions is None else permissions
        ch = next(c for c in self.guilds[gid]["channels"] if c["id"] == channel_id)
        iid, token = snowflake(), "aW50ZXJhY3Rpb246" + snowflake()
        opts = []
        for k, v in (options or {}).items():
            spec = next(o for o in cmd.get("options") or [] if o["name"] == k)
            opts.append({"name": k, "type": spec["type"], "value": v})
        payload = {"id": iid, "application_id": self.app_id, "type": 2, "token": token, "version": 1,
                   "guild_id": gid, "channel_id": channel_id, "channel": ch,
                   "guild": {"id": gid, "locale": "en-US", "features": []},
                   "member": {**member, "permissions": str(perms & ((1 << 53) - 1))},
                   "data": {"id": cmd["id"], "name": name, "type": 1, "guild_id": gid, "options": opts},
                   "app_permissions": str(self._channel_perms(ch, self.app_id) & ((1 << 53) - 1)),
                   "locale": "en-GB", "guild_locale": "en-US", "entitlements": [],
                   "authorizing_integration_owners": {"0": gid}, "context": 0, "attachment_size_limit": 10485760}
        it = Interaction(iid, token, payload, time.monotonic())
        self.interactions[iid] = it
        await self.dispatch("INTERACTION_CREATE", payload)
        return it

    async def wait_reply(self, it: Interaction, followups: int = 1, timeout: float = 30.0) -> Interaction:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            done = it.response_type == 4 or (it.response_type == 5 and len(it.followups) >= followups)
            if done or it.errors:
                return it
            await asyncio.sleep(0.02)
        raise TimeoutError(f"/{it.payload['data']['name']} got no reply")

    def channel_messages(self, cid: str) -> List[dict]:
        return list(self.messages.get(cid, []))


class GatewaySession:
    HEARTBEAT_MS = 41250

    def __init__(self, fake: FakeDiscord, ws: WebSocket, compress: Optional[str]):
        self.fake, self.ws = fake, ws
        if compress not in (None, "zlib-stream"):
            self.unsupported = compress
        else:
            self.unsupported = None
        self.zlib = zlib.compressobj() if compress == "zlib-stream" else None
        self.seq = 0
        self.ready = False
        self.intents = 0
        self.session_id = snowflake()
        self.sent: List[dict] = []
        self.received: List[dict] = []

    async def send(self, msg: dict):
        if msg.get("op") == 0:
            self.seq += 1
            msg["s"] = self.seq
        else:
            msg.setdefault("s", None)
        msg.setdefault("t", None)
        self.sent.append(msg)
        data = json.dumps(msg)
        if self.zlib:
            await self.ws.send_bytes(self.zlib.compress(data.encode()) + self.zlib.flush(zlib.Z_SYNC_FLUSH))
        else:
            await self.ws.send_text(data)

    async def close(self, code: int, reason: str):
        self.ready = False
        await self.ws.close(code=code, reason=reason)

    async def run(self):
        if self.unsupported:
            await self.ws.close(code=4000, reason=f"unsupported compression {self.unsupported}")
            return
        await self.send({"op": 10, "d": {"heartbeat_interval": self.HEARTBEAT_MS}})
        try:
            while True:
                msg = json.loads(await self.ws.receive_text())
                self.received.append(msg)
                await self.on_message(msg)
        except (WebSocketDisconnect, RuntimeError):
            self.ready = False

    async def on_message(self, msg: dict):
        op, d = msg.get("op"), msg.get("d")
        if op == 1:
            await self.send({"op": 11})
        elif op == 2:
            self.fake.identify_log.append({"intents": d.get("intents"), "properties": d.get("properties")})
            if d.get("token") != self.fake.token:
                await self.close(4004, "Authentication failed.")
                return
            intents = int(d.get("intents") or 0)
            for bit, name in PRIVILEGED.items():
                if intents & bit and name not in self.fake.privileged_enabled:
                    await self.close(4014, "Disallowed intent(s).")
                    return
            self.intents = intents
            gids = [g for g in self.fake.guilds if self.fake.app_id in self.fake.members[g]]
            base = self.fake.base_url.replace("http", "ws", 1) + "/gateway"
            await self.send({"op": 0, "t": "READY", "d": {
                "v": 10, "user": self.fake.bot_user, "guilds": [{"id": g, "unavailable": True} for g in gids],
                "session_id": self.session_id, "resume_gateway_url": base, "shard": [0, 1],
                "application": {"id": self.fake.app_id, "flags": self.fake.application()["flags"]},
                "user_settings": {}, "private_channels": [], "presences": [], "relationships": [],
                "guild_join_requests": [], "geo_ordered_rtc_regions": [], "auth": {}}})
            if intents & INTENT_GUILDS:
                for g in gids:
                    await self.send({"op": 0, "t": "GUILD_CREATE", "d": self.fake.guild_create(g, intents)})
            self.ready = True
        elif op == 6:  # RESUME: this fake keeps no backlog, so it asks for a fresh IDENTIFY
            await self.send({"op": 9, "d": False})
        elif op == 8:
            gid = str(d.get("guild_id"))
            if not self.intents & INTENT_MEMBERS:
                await self.close(4013, "Invalid intent(s).")
                return
            await self.send({"op": 0, "t": "GUILD_MEMBERS_CHUNK", "d": {
                "guild_id": gid, "members": list(self.fake.members.get(gid, {}).values()), "chunk_index": 0,
                "chunk_count": 1, "nonce": d.get("nonce"), "not_found": []}})


async def serve(fake: FakeDiscord, host: str = "127.0.0.1", port: int = 0) -> Callable:
    """Run the fake on a real port in the current event loop. Returns an async stop()."""
    import socket

    import uvicorn

    sock = socket.socket()
    sock.bind((host, port))
    port = sock.getsockname()[1]
    fake.base_url = f"http://{host}:{port}"
    server = uvicorn.Server(uvicorn.Config(fake.app, log_level="warning", ws="websockets-sansio", lifespan="off"))
    task = asyncio.get_event_loop().create_task(server.serve(sockets=[sock]))
    while not server.started:
        await asyncio.sleep(0.01)

    async def stop():
        server.should_exit = True
        for s in list(fake.sessions):
            try:
                await s.ws.close(code=1001)
            except Exception:
                pass
        await asyncio.wait_for(task, 10)
    return stop
