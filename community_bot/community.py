"""Wires the pieces together and holds the actions both the bot and the web server use:
tickets, leads, member joins, mod-queue notices, and the background worker."""
import asyncio
import logging
import re
from typing import Optional

from .ai import AskService, clean_for_discord, make_provider
from .calendly import Bookings
from .config import Secrets, Settings
from .discord_rest import DiscordError, DiscordREST
from .hooks import Hooks
from .kb import KnowledgeBase
from .membership import Membership
from .store import Store

log = logging.getLogger("community_bot")
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class Community:
    def __init__(self, settings: Settings, store: Store, rest: DiscordREST, secrets: Secrets,
                 provider=None, hook_client=None, clock=None):
        self.s = settings
        self.store = store
        self.rest = rest
        self.secrets = secrets
        self.hooks = Hooks(settings, store, secrets.get("HOOK_SIGNING_SECRET"), client=hook_client, clock=clock)
        self.membership = Membership(settings, store, rest, hooks=self.hooks, notify=self.notify, clock=clock)
        self.bookings = Bookings(settings, store, self.membership, hooks=self.hooks, post=self.post)
        self.kb = KnowledgeBase.load(settings.kb_dir)
        self._provider = provider
        self._ask: Optional[AskService] = None
        self.clock = clock or store.clock

    @classmethod
    def build(cls, settings: Settings, secrets: Optional[Secrets] = None, **kw) -> "Community":
        secrets = secrets or Secrets()
        store = Store(settings.db_path)
        rest = DiscordREST(secrets.require("DISCORD_BOT_TOKEN"), base=settings.api_base)
        return cls(settings, store, rest, secrets, **kw)

    @property
    def ask_service(self) -> AskService:
        if self._ask is None:
            provider = self._provider or make_provider(self.s, self.secrets)
            self._ask = AskService(self.s, self.store, self.kb, provider, clock=self.clock)
        return self._ask

    # ------------------------------------------------------------- posting
    async def post(self, channel_id: str, text: str, embeds=None) -> Optional[dict]:
        if not channel_id:
            return None
        return await self.rest.send_message(channel_id, text, embeds=embeds)

    async def notify(self, text: str):
        if self.s.mod_channel_id:
            await self.rest.send_message(self.s.mod_channel_id, text)

    # ------------------------------------------------------------- tickets and leads
    async def open_ticket(self, user_id: str, username: str, subject: str, body: str, source: str) -> int:
        subject, body = subject.strip()[:120] or "(no subject)", body.strip()[:1500]
        self.store.remember_member(user_id, username)
        tid = self.store.open_ticket(user_id, username, subject, body, source)
        embed = {"title": f"Ticket #{tid}: {clean_for_discord(subject)}"[:256],
                 "description": clean_for_discord(body)[:4000] or "(no details)",
                 "fields": [{"name": "Member", "value": f"<@{user_id}> ({username})", "inline": True},
                            {"name": "From", "value": source, "inline": True}],
                 "footer": {"text": f"Close with /ticket-close {tid}"}}
        try:
            msg = await self.post(self.s.mod_channel_id, f"New ticket #{tid}", embeds=[embed])
            if msg:
                self.store.set_ticket_message(tid, msg["id"])
        except DiscordError as exc:  # the ticket is saved and shows on the dashboard either way
            log.warning("ticket %s not posted to the mod queue: %s", tid, exc)
        self.hooks.emit("ticket.opened", {"ticket_id": tid, "discord_user_id": user_id, "username": username,
                                          "subject": subject, "body": body, "source": source}, ref=f"ticket:{tid}")
        return tid

    async def close_ticket(self, ticket_id: int, closed_by: str) -> Optional[dict]:
        t = self.store.close_ticket(ticket_id, closed_by)
        if t:
            self.hooks.emit("ticket.closed", {"ticket_id": ticket_id, "discord_user_id": t["discord_user_id"],
                                              "closed_by": closed_by}, ref=f"ticket-closed:{ticket_id}")
        return t

    async def capture_lead(self, user_id: str, username: str, email: str, message: str) -> Optional[str]:
        """/contact: a sales lead for the CRM. Returns an error message for the member, or None."""
        email = email.strip()
        if not EMAIL.match(email) or len(email) > 254:
            return "That email address doesn't look right. Please check it and try again."
        self.store.remember_member(user_id, username)
        self.hooks.emit("lead.captured", {"discord_user_id": user_id, "username": username, "email": email,
                                          "message": message.strip()[:1500], "source": "discord /contact"})
        try:
            await self.notify(f"New lead from <@{user_id}> ({username}): {clean_for_discord(message)[:300]}")
        except DiscordError as exc:
            log.warning("lead notice not posted: %s", exc)
        return None

    async def member_joined(self, user_id: str, username: str):
        self.store.member_joined(user_id, username)
        self.hooks.emit("member.joined", {"discord_user_id": user_id, "username": username})
        if self.store.link_for_user(user_id):  # paid before joining: give the role now
            await self.membership.sync_member(user_id, actor="join")

    async def member_left(self, user_id: str):
        self.store.member_left(user_id)
        self.hooks.emit("member.left", {"discord_user_id": user_id})

    # ------------------------------------------------------------- worker
    async def work_once(self) -> dict:
        done = await self.membership.tick()
        done.update(await self.hooks.flush())
        return done

    async def worker(self, every: float = 30.0):
        while True:
            try:
                await self.work_once()
            except Exception:
                log.exception("worker pass failed")
            await asyncio.sleep(every)
