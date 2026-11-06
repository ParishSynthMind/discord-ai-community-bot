"""The fake has to fail the way Discord fails, or the tests above prove nothing."""
import time

import httpx
import pytest

from community_bot.discord_rest import DiscordError, DiscordREST
from tests.conftest import GENERAL, GUILD, MAYA, MOD_CH, PREMIUM, async_test, build_fake


def rest_for(fake, token=None):
    return DiscordREST(token or fake.token, base="http://fake/api/v10",
                       client=httpx.AsyncClient(transport=httpx.ASGITransport(app=fake.app)), max_retries=0)


@async_test
async def test_bad_token_is_401():
    fake = build_fake()
    with pytest.raises(DiscordError) as e:
        await rest_for(fake, token="wrong").me()
    assert e.value.status == 401


@async_test
async def test_unknown_member_and_role_codes():
    fake = build_fake()
    rest = rest_for(fake)
    with pytest.raises(DiscordError) as e:
        await rest.add_role(GUILD, "1399999999999999999", PREMIUM, "x")
    assert e.value.code == 10007
    with pytest.raises(DiscordError) as e:
        await rest.add_role(GUILD, MAYA, "1399999999999999998", "x")
    assert e.value.code == 10011


@async_test
async def test_role_above_bot_and_managed_role_are_50013():
    fake = build_fake()
    rest = rest_for(fake)
    high = fake.add_role(GUILD, "Admin", 10)["id"]
    with pytest.raises(DiscordError) as e:
        await rest.add_role(GUILD, MAYA, high, "x")
    assert (e.value.status, e.value.code) == (403, 50013)
    managed = next(r for r in fake.guilds[GUILD]["roles"] if r["managed"])["id"]
    with pytest.raises(DiscordError):
        await rest.add_role(GUILD, MAYA, managed, "x")


@async_test
async def test_add_role_is_idempotent_and_records_audit_reason():
    fake = build_fake()
    rest = rest_for(fake)
    await rest.add_role(GUILD, MAYA, PREMIUM, "Paid: stripe evt_1")
    await rest.add_role(GUILD, MAYA, PREMIUM, "Paid: stripe evt_1")
    assert fake.member_roles(GUILD, MAYA).count(PREMIUM) == 1
    assert len(fake.audit_log) == 1 and fake.audit_log[0]["reason"] == "Paid: stripe evt_1"


@async_test
async def test_listing_members_needs_the_privileged_intent():
    fake = build_fake(privileged_intents=())
    with pytest.raises(DiscordError) as e:
        await rest_for(fake).list_members(GUILD)
    assert e.value.code == 50001
    ok = build_fake()
    assert len(await rest_for(ok).list_members(GUILD)) == 4


@async_test
async def test_message_rules():
    fake = build_fake()
    rest = rest_for(fake)
    with pytest.raises(DiscordError) as e:
        await rest.request("POST", f"/channels/{GENERAL}/messages", json={"content": "x" * 2001})
    assert e.value.code == 50035
    with pytest.raises(DiscordError) as e:
        await rest.request("POST", f"/channels/{GENERAL}/messages", json={"content": ""})
    assert e.value.code == 50006
    # the client trims to 2000 itself
    msg = await rest.send_message(GENERAL, "y" * 2500)
    assert len(msg["content"]) == 2000


@async_test
async def test_channel_overwrites_hide_channel_from_bot():
    fake = build_fake()
    bot_role = next(r for r in fake.guilds[GUILD]["roles"] if r["managed"])["id"]
    ch = next(c for c in fake.guilds[GUILD]["channels"] if c["id"] == MOD_CH)
    ch["permission_overwrites"] = [o for o in ch["permission_overwrites"] if o["id"] != bot_role]
    with pytest.raises(DiscordError) as e:
        await rest_for(fake).send_message(MOD_CH, "hello")
    assert e.value.code == 50001


@async_test
async def test_dm_blocked_is_50007():
    fake = build_fake()
    fake.dm_blocked.add(MAYA)
    with pytest.raises(DiscordError) as e:
        await rest_for(fake).dm(MAYA, "hi")
    assert e.value.code == 50007


@async_test
async def test_rate_limit_429_body_and_headers():
    fake = build_fake()
    fake.limits["message"] = (1, 5.0)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=fake.app)) as c:
        h = {"Authorization": f"Bot {fake.token}"}
        url = f"http://fake/api/v10/channels/{GENERAL}/messages"
        first = await c.post(url, json={"content": "a"}, headers=h)
        assert first.headers["X-RateLimit-Remaining"] == "0" and first.headers["X-RateLimit-Bucket"]
        second = await c.post(url, json={"content": "b"}, headers=h)
    assert second.status_code == 429 and second.json()["retry_after"] > 0 and "Retry-After" in second.headers


@async_test
async def test_interaction_three_second_window_and_single_response():
    fake = build_fake(interaction_window=3.0)
    fake.commands[GUILD] = [{"id": "1", "name": "ask", "options": []}]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=fake.app)) as c:
        it = await fake.run_command(GUILD, GENERAL, MAYA, "ask")
        url = f"http://fake/api/v10/interactions/{it.id}/{it.token}/callback"
        ok = await c.post(url, json={"type": 5})
        assert ok.status_code == 204
        twice = await c.post(url, json={"type": 4, "data": {"content": "hi"}})
        assert twice.json()["code"] == 40060
        late = await fake.run_command(GUILD, GENERAL, MAYA, "ask")
        late.created = time.monotonic() - 3.5
        r = await c.post(f"http://fake/api/v10/interactions/{late.id}/{late.token}/callback", json={"type": 5})
        assert r.json()["code"] == 10062


@async_test
async def test_followup_needs_ack_and_fresh_token():
    fake = build_fake()
    fake.commands[GUILD] = [{"id": "1", "name": "ask", "options": []}]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=fake.app)) as c:
        it = await fake.run_command(GUILD, GENERAL, MAYA, "ask")
        url = f"http://fake/api/v10/webhooks/{fake.app_id}/{it.token}"
        assert (await c.post(url, json={"content": "x"})).json()["code"] == 10015
        await c.post(f"http://fake/api/v10/interactions/{it.id}/{it.token}/callback", json={"type": 5})
        assert (await c.post(url, json={"content": "x"})).status_code == 200
        it.created -= 16 * 60
        assert (await c.post(url, json={"content": "x"})).json()["code"] == 50027


@async_test
async def test_command_registration_validation():
    fake = build_fake()
    rest = rest_for(fake)
    with pytest.raises(DiscordError) as e:
        await rest.request("PUT", f"/applications/{fake.app_id}/guilds/{GUILD}/commands",
                           json=[{"name": "Ask", "description": "x"}])
    assert e.value.code == 50035
    with pytest.raises(DiscordError):
        await rest.request("PUT", f"/applications/{fake.app_id}/guilds/{GUILD}/commands",
                           json=[{"name": "ask", "description": "x", "options": [
                               {"name": "a", "type": 3, "description": "a", "required": False},
                               {"name": "b", "type": 3, "description": "b", "required": True}]}])
