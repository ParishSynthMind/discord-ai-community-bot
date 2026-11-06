import json

from community_bot import hooks as H
from tests.conftest import MAYA, NOW, async_test, make_world


@async_test
async def test_hook_is_signed_and_verifiable():
    w = make_world()
    await w.community.capture_lead(MAYA, "maya", "maya@example.com", "Sponsorship for our art jam")
    await w.community.hooks.flush()
    req = w.hooks_received[0]
    body = json.loads(req.content)
    assert body["type"] == "lead.captured" and body["data"]["email"] == "maya@example.com"
    assert req.headers["Idempotency-Key"] == body["id"]
    assert H.verify("hook_secret", req.headers["X-Community-Timestamp"], req.content,
                    req.headers["X-Community-Signature"], now=lambda: NOW)
    assert not H.verify("other", req.headers["X-Community-Timestamp"], req.content,
                        req.headers["X-Community-Signature"], now=lambda: NOW)


@async_test
async def test_bad_email_is_refused_and_nothing_sent():
    w = make_world()
    problem = await w.community.capture_lead(MAYA, "maya", "not-an-email", "hi")
    assert "doesn't look right" in problem
    await w.community.hooks.flush()
    assert w.hooks_received == []


@async_test
async def test_5xx_is_retried_with_backoff_then_sent():
    calls = []

    def status(req):
        calls.append(req)
        return 502 if len(calls) == 1 else 200
    w = make_world(hook_status=status)
    w.community.hooks.emit("member.joined", {"discord_user_id": MAYA}, ref="join:1")
    assert (await w.community.hooks.flush())["failed"] == 1
    assert (await w.community.hooks.flush())["sent"] == 0  # not due yet
    w.clock.advance(16)
    assert (await w.community.hooks.flush())["sent"] == 1
    assert calls[0].headers["Idempotency-Key"] == calls[1].headers["Idempotency-Key"]


@async_test
async def test_4xx_is_parked_as_dead():
    w = make_world(hook_status=404)
    w.community.hooks.emit("member.joined", {"discord_user_id": MAYA})
    await w.community.hooks.flush()
    assert w.store.hooks()[0]["status"] == "dead"


@async_test
async def test_same_ref_never_queues_twice_and_event_filter():
    w = make_world()
    hooks = w.community.hooks
    assert hooks.emit("member.joined", {"x": 1}, ref="same")
    assert hooks.emit("member.joined", {"x": 1}, ref="same") is None
    w.settings.hook_events = ["ticket.opened"]
    assert hooks.emit("member.joined", {"x": 2}) is None


@async_test
async def test_no_url_means_no_hooks():
    w = make_world()
    w.settings.hook_url = ""
    assert w.community.hooks.emit("member.joined", {}) is None
    assert await w.community.hooks.flush() == {"sent": 0, "failed": 0}


@async_test
async def test_ticket_posts_embed_to_mod_queue_and_hook():
    w = make_world()
    tid = await w.community.open_ticket(MAYA, "maya", "Refund @everyone", "I want my yearly plan refunded", "/ticket")
    msg = w.fake.channel_messages("1300000000000000201")[-1]
    assert msg["embeds"][0]["title"].startswith(f"Ticket #{tid}") and "@​everyone" in msg["embeds"][0]["title"]
    assert w.store.ticket(tid)["mod_message_id"] == msg["id"]
    await w.community.close_ticket(tid, "sam_mod")
    await w.community.hooks.flush()
    assert [json.loads(r.content)["type"] for r in w.hooks_received] == ["ticket.opened", "ticket.closed"]


@async_test
async def test_ticket_is_saved_even_if_mod_queue_post_fails():
    w = make_world()
    w.fake.inject("POST", r"/channels/\d+/messages", 403, {"message": "Missing Access", "code": 50001}, times=1)
    tid = await w.community.open_ticket(MAYA, "maya", "Help", "x", "/ticket")
    assert w.store.ticket(tid)["status"] == "open" and w.store.ticket(tid)["mod_message_id"] is None


@async_test
async def test_concurrent_flushes_send_each_hook_once():
    import asyncio

    import httpx

    seen = []

    async def slow_receiver(request):  # a receiver that takes a moment, so flushes overlap
        await asyncio.sleep(0.01)
        seen.append(request.headers["Idempotency-Key"])
        return httpx.Response(200)
    w = make_world()
    w.community.hooks.client = httpx.AsyncClient(transport=httpx.MockTransport(slow_receiver))
    for i in range(5):
        w.community.hooks.emit("member.joined", {"n": i}, ref=f"join:{i}")
    await asyncio.gather(w.community.hooks.flush(), w.community.hooks.flush(), w.community.hooks.flush())
    assert len(seen) == 5 and len(set(seen)) == 5
