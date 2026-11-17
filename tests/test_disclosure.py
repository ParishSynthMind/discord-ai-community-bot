"""AI disclosure (EU AI Act Article 50): the first message to a member in a conversation
says it is an AI assistant, on every path that messages members, and a handover says an AI
is passing them to a person. Removing it needs a recorded written request."""
from community_bot import payments as P
from community_bot.ai import Answer
from community_bot.config import load_settings
from community_bot.disclosure import disclosure_line
from tests.conftest import GENERAL, GUILD, MAYA, OMAR, async_test, checkout_completed, invoice_event, make_world
from tests.test_bot_gateway import live

LINE = "I'm an AI assistant for Pixelforge Creators Club."


async def ask(w, uid, question="What does Premium include?"):
    it = await w.fake.run_command(GUILD, GENERAL, uid, "ask", {"question": question})
    await w.fake.wait_reply(it)
    return it.followups[0]["content"]


async def command(w, uid, name, options=None):
    it = await w.fake.run_command(GUILD, GENERAL, uid, name, options or {})
    await w.fake.wait_reply(it)
    return it.followups[0]["content"] if it.followups else it.response["data"]["content"]


# ------------------------------------------------------------------ config
def test_default_line_uses_the_community_name():
    assert disclosure_line(load_settings(env={"NO_KEYCHAIN": "1"})) == LINE


def test_custom_text_from_env():
    s = load_settings(env={"NO_KEYCHAIN": "1", "AI_DISCLOSURE": "You're talking to Pixel, an AI assistant."})
    assert disclosure_line(s) == "You're talking to Pixel, an AI assistant."


def test_blank_text_without_written_request_falls_back_to_default():
    s = load_settings(env={"NO_KEYCHAIN": "1", "AI_DISCLOSURE": ""})
    s.ai_disclosure = "   "
    assert disclosure_line(s) == LINE


def test_removed_only_with_written_request_reference():
    s = load_settings(env={"NO_KEYCHAIN": "1",
                           "AI_DISCLOSURE_REMOVED_BY_WRITTEN_REQUEST": "2026-11-17 email from the client"})
    assert disclosure_line(s) == ""


# ------------------------------------------------------------------ /ask over the real gateway
@async_test
async def test_first_answer_discloses_and_second_does_not():
    async with live() as (w, _, _):
        first = await ask(w, MAYA)
        second = await ask(w, MAYA, "When is the review call?")
        assert first.startswith(LINE + "\n\n")
        assert "AI assistant" not in second


@async_test
async def test_each_member_is_told_once():
    async with live() as (w, _, _):
        await ask(w, MAYA)
        assert (await ask(w, OMAR)).startswith(LINE)


@async_test
async def test_new_conversation_after_a_long_gap_discloses_again():
    async with live() as (w, _, _):
        await ask(w, MAYA)
        w.clock.advance(23 * 3600)
        assert "AI assistant" not in await ask(w, MAYA)  # same conversation
        w.clock.advance(25 * 3600)
        assert (await ask(w, MAYA)).startswith(LINE)  # a new one


@async_test
async def test_handover_says_an_ai_is_passing_to_a_person_every_time():
    replies = [Answer("answered", "Premium is 9 US dollars a month.", [1], 1, 1),
               Answer("needs_human", "A moderator will look at your charge.", [], 1, 1)]
    async with live(replies=replies) as (w, _, _):
        await ask(w, MAYA, "How much is Premium?")
        text = await ask(w, MAYA, "I was charged twice this month")
        assert not text.startswith(LINE)  # not the opening line again...
        assert "I'm an AI assistant, so I've passed this to a human moderator" in text  # ...but the handover says so
        assert "(ticket #1)" in text


@async_test
async def test_model_error_handover_discloses():
    async with live(replies=[RuntimeError("model down")]) as (w, _, _):
        text = await ask(w, MAYA)
        assert text.startswith(LINE) and "passed this to a human moderator" in text


@async_test
async def test_every_member_command_discloses_when_it_is_the_first_message():
    async with live() as (w, _, _):
        assert (await command(w, MAYA, "premium")).startswith(LINE)
        assert (await command(w, OMAR, "book")).startswith(LINE)
        assert "AI assistant" not in await command(w, OMAR, "premium")
    async with live() as (w, _, _):
        assert (await command(w, MAYA, "ticket", {"subject": "Help", "details": "Role missing"})).startswith(LINE)
        assert (await command(w, OMAR, "contact", {"email": "omar@studio.example", "message": "Hi"})).startswith(LINE)
    async with live(booking_url="") as (w, _, _):
        assert (await command(w, MAYA, "book")).startswith(LINE)


@async_test
async def test_ticket_reply_says_a_person_will_answer():
    async with live() as (w, _, _):
        text = await command(w, MAYA, "ticket", {"subject": "Help", "details": "Role missing"})
        assert "A moderator (a person) will reply" in text


@async_test
async def test_blank_disclosure_still_shows_the_default():
    async with live(ai_disclosure="") as (w, _, _):
        assert (await ask(w, MAYA)).startswith(LINE)


@async_test
async def test_removed_by_written_request_shows_no_opening_line():
    async with live(ai_disclosure_removed_by_written_request="2026-11-17 email from the client") as (w, _, _):
        assert "AI assistant" not in await ask(w, MAYA)


# ------------------------------------------------------------------ DMs from the payment flow
def dms(w):
    return [m["content"] for cid, msgs in w.fake.messages.items() if cid in w.fake.dm_channels.values() for m in msgs]


@async_test
async def test_payment_dm_discloses_when_it_is_the_first_message():
    w = make_world()
    await w.m.apply(P.normalize_stripe(checkout_completed()))
    await w.m.apply(P.normalize_stripe(invoice_event("invoice.payment_failed")))
    assert dms(w)[0].startswith(LINE) and "didn't go through" in dms(w)[0]
    await w.rest.aclose()


@async_test
async def test_payment_dm_does_not_repeat_inside_a_conversation():
    w = make_world()
    opening = w.community.disclosure.opening(MAYA)
    w.community.disclosure.sent(MAYA, opening)  # e.g. Maya just used /ask
    await w.m.apply(P.normalize_stripe(checkout_completed()))
    await w.m.apply(P.normalize_stripe(invoice_event("invoice.payment_failed")))
    assert "AI assistant" not in dms(w)[0]
    await w.rest.aclose()


def test_state_survives_in_sqlite():
    w = make_world()
    d = w.community.disclosure
    assert d.opening(MAYA) == LINE + "\n\n"
    d.sent(MAYA, d.opening(MAYA))
    assert d.opening(MAYA) == ""
    assert w.store.ai_disclosure_state(MAYA)["disclosed_at"] == w.clock()


# ------------------------------------------------------------------ community-bot check
@async_test
async def test_check_shows_the_disclosure_and_flags_a_removal():
    from tests.test_cli import run_check

    w = make_world()
    _, out = await run_check(w, {"ANTHROPIC_API_KEY": "x"})
    assert f"AI disclosure on the first message: {LINE!r}" in out
    w.settings.ai_disclosure_removed_by_written_request = "2026-11-17 email from the client"
    _, out = await run_check(w, {"ANTHROPIC_API_KEY": "x"})
    assert "AI disclosure removed by written request: 2026-11-17 email from the client" in out
    await w.rest.aclose()


# ------------------------------------------------------------------ ordering (CI race, 17 Nov)
def test_line_is_recorded_before_the_send_and_undone_if_it_fails():
    from community_bot.disclosure import Disclosure
    w = make_world()
    d = Disclosure(w.settings, w.store)
    first = d.opening(MAYA)
    assert first == LINE + "\n\n"
    d.sent(MAYA, first)                 # recorded before the network call
    assert d.opening(MAYA) == ""        # a second command arriving mid-send doesn't repeat it
    d.unsent(MAYA, first)               # the send failed
    assert d.opening(MAYA) == LINE + "\n\n"
