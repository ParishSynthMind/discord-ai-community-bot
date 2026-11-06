from community_bot.cli import check, main
from community_bot.config import Secrets
from community_bot.discord_rest import invite_url
from tests.conftest import GUILD, MOD_CH, PREMIUM, SECRETS, async_test, build_fake, make_world


async def run_check(w, env=None):
    lines = []
    ok = await check(w.settings, Secrets(env={**SECRETS, **(env or {})}), rest=w.rest, out=lines.append)
    return ok, "\n".join(lines)


@async_test
async def test_check_all_green():
    w = make_world()
    ok, out = await run_check(w, {"ANTHROPIC_API_KEY": "x"})
    assert ok, out
    assert "token works" in out and "Premium role 'Premium' can be given" in out
    assert "mod queue channel #mod-queue: bot can read and post" in out
    assert "permissions=268454912" in out
    assert "sk-" not in out and "x" not in out.split("key ")[-1][:3]  # never prints a key


@async_test
async def test_check_explains_role_hierarchy_fix():
    fake = build_fake()
    next(r for r in fake.guilds[GUILD]["roles"] if r["id"] == PREMIUM)["position"] = 8
    ok, out = await run_check(make_world(fake=fake), {"ANTHROPIC_API_KEY": "x"})
    assert not ok and "drag the bot's role above it" in out


@async_test
async def test_check_finds_hidden_mod_channel_and_missing_key():
    fake = build_fake()
    ch = next(c for c in fake.guilds[GUILD]["channels"] if c["id"] == MOD_CH)
    bot_role = next(r for r in fake.guilds[GUILD]["roles"] if r["managed"])["id"]
    ch["permission_overwrites"] = [o for o in ch["permission_overwrites"] if o["id"] != bot_role]
    ok, out = await run_check(make_world(fake=fake))
    assert not ok and "mod queue channel" in out and "Missing Access" in out
    assert "key missing" in out


@async_test
async def test_check_members_intent_mismatch():
    fake = build_fake(privileged_intents=())
    w = make_world(fake=fake, members_intent=True)
    ok, out = await run_check(w, {"ANTHROPIC_API_KEY": "x"})
    assert not ok and "Privileged Gateway Intents" in out


@async_test
async def test_check_bad_token():
    w = make_world()
    w.rest._token = "wrong"
    ok, out = await run_check(w)
    assert not ok and "refused the token (401)" in out


def test_invite_url_minimal_permissions():
    url = invite_url("123", "456")
    assert "scope=bot%20applications.commands" in url and "permissions=268454912" in url
    assert "guild_id=456" in url and "permissions=8&" not in url  # never Administrator


def test_kb_search_cli(capsys, monkeypatch):
    monkeypatch.setenv("NO_KEYCHAIN", "1")
    main(["kb-search", "refund on a yearly plan"])
    assert "FAQ > Refunds (faq.md)" in capsys.readouterr().out.splitlines()[0]
