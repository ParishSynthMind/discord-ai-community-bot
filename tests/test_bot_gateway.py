"""The real discord.py client, over a real websocket and real HTTP, against the fake."""
import asyncio
import contextlib
import json

import discord
import pytest

from community_bot import payments as P
from community_bot.ai import Answer
from community_bot.bot import CommunityBot, point_at
from community_bot.discord_rest import DiscordREST
from community_bot.fake_discord import INTENT_GUILDS, INTENT_MEMBERS, serve
from tests.conftest import (
    GENERAL,
    GUILD,
    MAYA,
    MOD,
    OMAR,
    PREMIUM,
    async_test,
    build_fake,
    checkout_completed,
    make_world,
)


@contextlib.asynccontextmanager
async def live(fake=None, token=None, members_intent=False, replies=None, **settings_over):
    fake = fake or build_fake()
    stop = await serve(fake)
    w = make_world(fake=fake, members_intent=members_intent, **settings_over)
    if replies:
        w.provider.replies = list(replies)
    w.community.rest = w.m.rest = DiscordREST(fake.token, base=fake.base_url + "/api/v10")
    point_at(fake.base_url + "/api/v10", fake.base_url.replace("http", "ws") + "/gateway")
    bot = CommunityBot(w.community)
    task = asyncio.create_task(bot.start(token or fake.token))
    try:
        if token is None:
            await fake.wait_ready()
            for _ in range(200):
                if bot.ready_seen:
                    break
                await asyncio.sleep(0.02)
        yield w, bot, task
    finally:
        await bot.close()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(task, 5)
        await w.community.rest.aclose()
        await stop()
        point_at("https://discord.com/api/v10", "wss://gateway.discord.gg/")


@async_test
async def test_login_identify_minimal_intents_and_command_sync():
    async with live() as (w, bot, _):
        ident = w.fake.identify_log[-1]
        assert ident["intents"] == INTENT_GUILDS  # GUILDS only: no members, no message content
        assert ident["properties"]["browser"] == "discord.py"
        names = sorted(c["name"] for c in w.fake.commands[GUILD])
        assert names == ["ask", "book", "contact", "premium", "ticket", "ticket-close"]
        close = next(c for c in w.fake.commands[GUILD] if c["name"] == "ticket-close")
        assert close["default_member_permissions"] == str(1 << 13)
        assert bot.get_guild(int(GUILD)).name == "Pixelforge Creators Club"
        # the gateway really is compressed zlib-stream, as discord.py asks for
        assert w.fake.sessions and w.fake.sessions[0].zlib is not None


@async_test
async def test_ask_defers_then_answers_with_sources():
    reply = Answer("answered", "Premium gets the lounge, portfolio reviews and early asset packs.", [1], 400, 30)
    async with live(replies=[reply]) as (w, _, _):
        it = await w.fake.run_command(GUILD, GENERAL, MAYA, "ask", {"question": "What does Premium include?"})
        await w.fake.wait_reply(it)
        assert it.response_type == 5 and not it.errors  # acknowledged inside 3 s, then a follow-up
        text = it.followups[0]["content"]
        assert text.startswith("Premium gets the lounge") and "Sources: Membership > What Premium includes" in text
        assert it.followups[0]["allowed_mentions"] == {"parse": []}


@async_test
async def test_ask_that_needs_a_human_opens_a_ticket():
    reply = Answer("needs_human", "A moderator will look at your charge.", [], 100, 10)
    async with live(replies=[reply]) as (w, _, _):
        it = await w.fake.run_command(GUILD, GENERAL, MAYA, "ask", {"question": "I was charged twice this month"})
        await w.fake.wait_reply(it)
        assert "(ticket #1)" in it.followups[0]["content"]
        assert w.fake.channel_messages("1300000000000000201")[-1]["embeds"][0]["title"].startswith("Ticket #1")


@async_test
async def test_slow_model_still_answered_because_of_defer():
    async with live() as (w, _, _):
        orig = w.provider.answer

        async def slow(q, hits):
            await asyncio.sleep(0.4)
            return await orig(q, hits)
        w.provider.answer = slow
        w.fake.interaction_window = 0.2  # would be too late without the deferral
        it = await w.fake.run_command(GUILD, GENERAL, MAYA, "ask", {"question": "When is the review call?"})
        await w.fake.wait_reply(it)
        assert not it.errors and it.followups


@async_test
async def test_premium_command_gives_link_then_restores_missing_role():
    async with live() as (w, _, _):
        it = await w.fake.run_command(GUILD, GENERAL, MAYA, "premium")
        await w.fake.wait_reply(it)
        assert f"client_reference_id={MAYA}" in it.followups[0]["content"]
        assert it.followups[0]["flags"] & 64 or it.response["data"]["flags"] & 64  # only the member sees it
        await w.m.apply(P.normalize_stripe(checkout_completed()))
        w.fake.members[GUILD][MAYA]["roles"].remove(PREMIUM)
        it = await w.fake.run_command(GUILD, GENERAL, MAYA, "premium")
        await w.fake.wait_reply(it)
        assert "has been put back" in it.followups[0]["content"] and PREMIUM in w.roles(MAYA)


@async_test
async def test_ticket_contact_and_book():
    async with live() as (w, _, _):
        it = await w.fake.run_command(GUILD, GENERAL, OMAR, "ticket", {"subject": "Report", "details": "Spam in #showcase"})
        await w.fake.wait_reply(it)
        assert "Ticket #1 is open" in it.followups[0]["content"] and w.store.ticket(1)["username"] == "omar"
        it = await w.fake.run_command(GUILD, GENERAL, OMAR, "contact",
                                      {"email": "omar@studio.example", "message": "Sponsoring the November challenge"})
        await w.fake.wait_reply(it)
        assert "the team has your details" in it.followups[0]["content"]
        types = [json.loads(r.content)["type"] for r in w.hooks_received]
        assert types == ["ticket.opened", "lead.captured"]
        it = await w.fake.run_command(GUILD, GENERAL, OMAR, "book")
        await w.fake.wait_reply(it)
        assert f"utm_content={OMAR}" in it.response["data"]["content"]


@async_test
async def test_ticket_close_is_for_moderators_only():
    async with live() as (w, _, _):
        await w.community.open_ticket(MAYA, "maya", "x", "y", "/ticket")
        it = await w.fake.run_command(GUILD, GENERAL, MAYA, "ticket-close", {"ticket_id": 1})
        await w.fake.wait_reply(it)
        assert "Only moderators" in it.response["data"]["content"] and w.store.ticket(1)["status"] == "open"
        it = await w.fake.run_command(GUILD, GENERAL, MOD, "ticket-close", {"ticket_id": 1})
        await w.fake.wait_reply(it)
        assert "closed" in it.response["data"]["content"] and w.store.ticket(1)["closed_by"] == "sam_mod"


@async_test
async def test_member_join_event_with_members_intent():
    async with live(members_intent=True) as (w, _, _):
        assert w.fake.identify_log[-1]["intents"] == INTENT_GUILDS | INTENT_MEMBERS
        await w.m.apply(P.normalize_stripe(checkout_completed(uid="1300000000000009077", customer="c7", sub="s7")))
        await w.fake.member_join(GUILD, "1300000000000009077", "newbie")
        for _ in range(100):
            if PREMIUM in w.roles("1300000000000009077"):
                break
            await asyncio.sleep(0.02)
        assert PREMIUM in w.roles("1300000000000009077")  # paid before joining, role on arrival
        assert any(json.loads(r.content)["type"] == "member.joined" for r in w.hooks_received)


@async_test
async def test_privileged_intent_not_enabled_is_refused_by_gateway():
    fake = build_fake(privileged_intents=())
    with pytest.raises(discord.PrivilegedIntentsRequired):
        async with live(fake=fake, members_intent=True, token=fake.token) as (_, _, task):
            await asyncio.wait_for(task, 10)


@async_test
async def test_wrong_token_fails_login():
    with pytest.raises(discord.LoginFailure):
        async with live(token="not-the-token") as (_, _, task):
            await asyncio.wait_for(task, 10)
