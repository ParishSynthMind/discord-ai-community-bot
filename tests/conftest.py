import asyncio
import functools
import json
from dataclasses import dataclass, field
from typing import List

import httpx
import pytest

from community_bot.ai import ScriptedProvider
from community_bot.community import Community
from community_bot.config import Secrets, load_settings
from community_bot.discord_rest import DiscordREST
from community_bot.fake_discord import (
    EMBED_LINKS,
    MANAGE_MESSAGES,
    MANAGE_ROLES,
    SEND_MESSAGES,
    VIEW_CHANNEL,
    FakeDiscord,
)
from community_bot.payments import stripe_sign
from community_bot.store import Store

GUILD = "1300000000000000001"
PREMIUM = "1300000000000000101"
CALL_BOOKED = "1300000000000000102"
MOD_CH = "1300000000000000201"
BOOK_CH = "1300000000000000202"
GENERAL = "1300000000000000301"
MAYA = "1300000000000009001"
OMAR = "1300000000000009002"
LENA = "1300000000000009003"
MOD = "1300000000000009009"
STRIPE_SECRET = "whsec_test_secret"
NOW = 1_794_000_000.0  # 2026-11-06


class Clock:
    def __init__(self, t=NOW):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


SECRETS = {"NO_KEYCHAIN": "1", "STRIPE_WEBHOOK_SECRET": STRIPE_SECRET, "LEMONSQUEEZY_SIGNING_SECRET": "ls_secret",
           "PADDLE_WEBHOOK_SECRET": "pdl_secret", "CALENDLY_SIGNING_KEY": "cal_key", "HOOK_SIGNING_SECRET": "hook_secret",
           "ADMIN_TOKEN": "admin-token-for-tests", "DISCORD_BOT_TOKEN": "fake-bot-token"}


def build_fake(**kw) -> FakeDiscord:
    fake = FakeDiscord(**kw)
    fake.add_guild(GUILD, "Pixelforge Creators Club")
    fake.add_role(GUILD, "Moderator", 3, MANAGE_MESSAGES | VIEW_CHANNEL | SEND_MESSAGES)
    fake.add_role(GUILD, "Premium", 1, role_id=PREMIUM)
    fake.add_role(GUILD, "Call booked", 2, role_id=CALL_BOOKED)
    fake.add_bot_to_guild(GUILD, 5, MANAGE_ROLES | VIEW_CHANNEL | SEND_MESSAGES | EMBED_LINKS)
    fake.add_channel(GUILD, "general", channel_id=GENERAL)
    # mod-queue: hidden from @everyone, visible to the bot through its role
    bot_role = next(r for r in fake.guilds[GUILD]["roles"] if r["managed"])
    fake.add_channel(GUILD, "mod-queue", channel_id=MOD_CH, overwrites=[
        {"id": GUILD, "type": 0, "allow": "0", "deny": str(VIEW_CHANNEL)},
        {"id": bot_role["id"], "type": 0, "allow": str(VIEW_CHANNEL | SEND_MESSAGES), "deny": "0"}])
    fake.add_channel(GUILD, "bookings", channel_id=BOOK_CH)
    fake.add_member(GUILD, MAYA, "maya")
    fake.add_member(GUILD, OMAR, "omar")
    mod_role = next(r for r in fake.guilds[GUILD]["roles"] if r["name"] == "Moderator")
    fake.add_member(GUILD, MOD, "sam_mod", roles=[mod_role["id"]])
    return fake


@dataclass
class World:
    settings: object
    fake: FakeDiscord
    store: Store
    rest: DiscordREST
    community: Community
    clock: Clock
    provider: ScriptedProvider
    hooks_received: List[httpx.Request] = field(default_factory=list)

    @property
    def m(self):
        return self.community.membership

    def roles(self, uid):
        return self.fake.member_roles(GUILD, uid)

    def mod_messages(self):
        return [m["content"] for m in self.fake.channel_messages(MOD_CH)]


def make_world(fake=None, hook_status=200, settings_env=None, provider=None, **settings_over) -> World:
    env = {**SECRETS, **(settings_env or {})}
    settings = load_settings(env=env)
    settings.db_path = ":memory:"
    settings.hook_url = "https://hooks.example.test/community"
    for k, v in settings_over.items():
        setattr(settings, k, v)
    fake = fake or build_fake()
    clock = Clock()
    store = Store(":memory:", clock=clock)
    rest = DiscordREST(fake.token, base="http://fake/api/v10",
                       client=httpx.AsyncClient(transport=httpx.ASGITransport(app=fake.app)),
                       sleep=_no_sleep)
    received: List[httpx.Request] = []

    def hook_handler(request: httpx.Request):
        received.append(request)
        status = hook_status(request) if callable(hook_status) else hook_status
        return httpx.Response(status, json={"ok": status < 300})

    provider = provider or ScriptedProvider()
    community = Community(settings, store, rest, Secrets(env=env), provider=provider,
                          hook_client=httpx.AsyncClient(transport=httpx.MockTransport(hook_handler)), clock=clock)
    return World(settings, fake, store, rest, community, clock, provider, received)


async def _no_sleep(_seconds):
    await asyncio.sleep(0)


def async_test(fn):
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        return asyncio.run(fn(*a, **kw))
    return wrapper


# ------------------------------------------------------------------ Stripe events
_n = [0]


def stripe_event(type_, obj, created=None, event_id=None):
    _n[0] += 1
    return {"id": event_id or f"evt_test_{_n[0]:05d}", "object": "event", "type": type_, "api_version": "2026-10-28",
            "created": int(created if created is not None else NOW), "livemode": False, "data": {"object": obj}}


def checkout_completed(uid=MAYA, customer="cus_maya", sub="sub_maya", **kw):
    obj = {"id": "cs_test_1", "object": "checkout.session", "mode": "subscription", "payment_status": "paid",
           "client_reference_id": uid, "customer": customer, "subscription": sub, "amount_total": 900,
           "currency": "usd", "customer_details": {"email": "maya@example.com"}, "metadata": {}}
    obj.update(kw.pop("obj", {}))
    return stripe_event("checkout.session.completed", obj, **kw)


def sub_event(type_, status="active", customer="cus_maya", sub="sub_maya", **kw):
    obj = {"id": sub, "object": "subscription", "customer": customer, "status": status,
           "cancel_at_period_end": kw.pop("cancel_at_period_end", False), "metadata": kw.pop("metadata", {}),
           "items": {"data": [{"price": {"id": kw.pop("price", "price_monthly")}}]}}
    return stripe_event(type_, obj, **kw)


def invoice_event(type_, customer="cus_maya", sub="sub_maya", amount=900, **kw):
    obj = {"id": "in_1", "object": "invoice", "customer": customer, "currency": "usd",
           "parent": {"type": "subscription_details", "subscription_details": {"subscription": sub}}}
    obj["amount_due" if type_ == "invoice.payment_failed" else "amount_paid"] = amount
    return stripe_event(type_, obj, **kw)


def signed(event, secret=STRIPE_SECRET, ts=None):
    body = json.dumps(event).encode()
    return body, {"Stripe-Signature": stripe_sign(body, secret, int(ts if ts is not None else NOW)),
                  "Content-Type": "application/json"}


@pytest.fixture
def world():
    return make_world
