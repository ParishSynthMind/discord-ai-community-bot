import hashlib
import hmac
import json
import re

import httpx

from community_bot import payments as P
from community_bot.calendly import calendly_sign
from community_bot.web import create_app
from tests.conftest import (
    MAYA,
    NOW,
    PREMIUM,
    async_test,
    checkout_completed,
    make_world,
    signed,
)


def client_for(w):
    app = create_app(w.community, clock=w.clock)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://bot.test")


@async_test
async def test_stripe_webhook_end_to_end():
    w = make_world()
    async with client_for(w) as c:
        body, headers = signed(checkout_completed())
        r = await c.post("/webhooks/stripe", content=body, headers=headers)
        assert r.status_code == 200 and r.json()["status"] == "applied"
        assert "discord_user_id" not in r.json()  # nothing personal echoed back
        again = await c.post("/webhooks/stripe", content=body, headers=headers)
        assert again.json()["status"] == "duplicate"
    assert PREMIUM in w.roles(MAYA)


@async_test
async def test_stripe_webhook_rejects_bad_signature_and_old_timestamp():
    w = make_world()
    async with client_for(w) as c:
        body, headers = signed(checkout_completed(), secret="whsec_wrong")
        assert (await c.post("/webhooks/stripe", content=body, headers=headers)).status_code == 400
        body, headers = signed(checkout_completed(), ts=NOW - 600)
        r = await c.post("/webhooks/stripe", content=body, headers=headers)
        assert r.status_code == 400 and "window" in r.json()["error"]
        assert (await c.post("/webhooks/stripe", content=b"{}")).status_code == 400
    assert PREMIUM not in w.roles(MAYA) and w.store.events() == []


@async_test
async def test_stripe_webhook_refuses_big_bodies():
    w = make_world()
    async with client_for(w) as c:
        r = await c.post("/webhooks/stripe", content=b"x" * 1_000_001, headers={"Stripe-Signature": "t=1,v1=a"})
    assert r.status_code == 413


@async_test
async def test_processing_error_answers_500_so_stripe_retries():
    w = make_world()

    async def boom(*a, **k):
        raise RuntimeError("disk full")
    w.m._apply = boom
    async with client_for(w) as c:
        body, headers = signed(checkout_completed(event_id="evt_retry"))
        assert (await c.post("/webhooks/stripe", content=body, headers=headers)).status_code == 500
        del w.m._apply  # back to the real method
        assert (await c.post("/webhooks/stripe", content=body, headers=headers)).json()["status"] == "applied"


@async_test
async def test_lemonsqueezy_and_paddle_routes():
    w = make_world()
    ls = json.dumps({"meta": {"event_name": "subscription_created", "custom_data": {"discord_user_id": MAYA}},
                     "data": {"type": "subscriptions", "id": "1", "attributes": {"customer_id": 9, "status": "active",
                                                                                "updated_at": "2026-11-06T10:00:00Z"}}}).encode()
    pd = json.dumps({"event_id": "evt_pd", "event_type": "subscription.canceled", "occurred_at": "2026-11-06T10:05:00Z",
                     "data": {"id": "sub_p", "status": "canceled", "customer_id": "ctm_1",
                              "custom_data": {"discord_user_id": MAYA}}}).encode()
    ts = int(NOW)
    async with client_for(w) as c:
        bad = await c.post("/webhooks/lemonsqueezy", content=ls, headers={"X-Signature": "00"})
        assert bad.status_code == 401
        sig = hmac.new(b"ls_secret", ls, hashlib.sha256).hexdigest()
        assert (await c.post("/webhooks/lemonsqueezy", content=ls, headers={"X-Signature": sig})).json()["status"] == "applied"
        assert PREMIUM in w.roles(MAYA)
        h1 = hmac.new(b"pdl_secret", f"{ts}:".encode() + pd, hashlib.sha256).hexdigest()
        r = await c.post("/webhooks/paddle", content=pd, headers={"Paddle-Signature": f"ts={ts};h1={h1}"})
        assert r.json()["status"] == "applied"
    # The Paddle subscription ending does not take away the role the Lemon Squeezy one still pays for
    assert PREMIUM in w.roles(MAYA)


def _calendly(event="invitee.created", uid=MAYA, answers=None):
    return {"event": event, "created_at": "2026-11-06T10:00:00Z", "payload": {
        "uri": "https://api.calendly.com/scheduled_events/EV1/invitees/INV1", "name": "Maya R",
        "email": "maya@example.com", "status": "active" if event == "invitee.created" else "canceled",
        "tracking": {"utm_content": uid} if uid else {}, "questions_and_answers": answers or [],
        "scheduled_event": {"name": "Intro call", "start_time": "2026-11-10T15:00:00.000000Z"}}}


@async_test
async def test_calendly_booking_gives_role_and_cancel_removes():
    from tests.conftest import BOOK_CH, CALL_BOOKED
    w = make_world()
    async with client_for(w) as c:
        body = json.dumps(_calendly()).encode()
        r = await c.post("/webhooks/calendly", content=body,
                         headers={"Calendly-Webhook-Signature": calendly_sign(body, "cal_key", int(NOW))})
        assert r.json()["status"] == "applied" and CALL_BOOKED in w.roles(MAYA)
        assert "booked \"Intro call\" for 2026-11-10 15:00" in w.fake.channel_messages(BOOK_CH)[-1]["content"]
        dup = await c.post("/webhooks/calendly", content=body,
                           headers={"Calendly-Webhook-Signature": calendly_sign(body, "cal_key", int(NOW))})
        assert dup.json()["status"] == "duplicate"
        body = json.dumps(_calendly("invitee.canceled")).encode()
        await c.post("/webhooks/calendly", content=body,
                     headers={"Calendly-Webhook-Signature": calendly_sign(body, "cal_key", int(NOW))})
    assert CALL_BOOKED not in w.roles(MAYA)
    await w.community.hooks.flush()
    assert [json.loads(h.content)["type"] for h in w.hooks_received] == ["booking.created", "booking.canceled"]


@async_test
async def test_calendly_matches_by_username_answer_or_reports_unmatched():
    w = make_world()
    w.store.remember_member(MAYA, "maya")
    b = w.community.bookings
    assert b.find_member(_calendly(uid=None, answers=[{"question": "Discord username", "answer": "@Maya"}])["payload"]) == MAYA
    r = await b.handle(_calendly(uid=None, answers=[{"question": "Discord username", "answer": "nobody"}]))
    assert r["status"] == "unmatched" and any("could not be matched" in m for m in w.mod_messages())


@async_test
async def test_calendly_bad_signature():
    w = make_world()
    async with client_for(w) as c:
        body = json.dumps(_calendly()).encode()
        r = await c.post("/webhooks/calendly", content=body,
                         headers={"Calendly-Webhook-Signature": calendly_sign(body, "wrong", int(NOW))})
    assert r.status_code == 401


# ------------------------------------------------------------------ dashboard
async def _login(c, token="admin-token-for-tests"):
    return await c.post("/admin/login", data={"token": token})


@async_test
async def test_dashboard_requires_login():
    w = make_world()
    async with client_for(w) as c:
        r = await c.get("/admin")
        assert r.status_code == 303 and r.headers["location"] == "/admin/login"
        assert (await c.get("/admin/api/summary")).status_code == 401
        bad = await _login(c, "nope")
        assert "not right" in bad.text and "cb_admin" not in bad.cookies


@async_test
async def test_dashboard_disabled_without_admin_token():
    w = make_world(settings_env={"ADMIN_TOKEN": ""})
    async with client_for(w) as c:
        assert (await c.get("/admin")).status_code == 503


@async_test
async def test_dashboard_pages_show_real_state():
    w = make_world()
    await w.m.apply(P.normalize_stripe(checkout_completed()))
    w.store.member_joined(MAYA, "maya")
    tid = await w.community.open_ticket(MAYA, "maya", "Role missing", "I paid", "/ticket")
    async with client_for(w) as c:
        r = await _login(c)
        assert r.status_code == 303
        home = await c.get("/admin")
        assert "Premium members" in home.text and re.search(r'class="v">1<', home.text)
        assert r.cookies.get("cb_admin") or c.cookies.get("cb_admin")
        assert home.headers["X-Frame-Options"] == "DENY"
        members = await c.get("/admin/members")
        assert "maya" in members.text and "cus_maya" in members.text
        tickets = await c.get("/admin/tickets")
        assert "Role missing" in tickets.text
        assert "checkout.session.completed" in (await c.get("/admin/payments")).text
        assert "add_role" in (await c.get("/admin/audit")).text
        summary = (await c.get("/admin/api/summary")).json()
        assert summary["premium_active"] == 1 and summary["open_tickets"] == 1
        # closing needs the form token
        assert (await c.post(f"/admin/tickets/{tid}/close", data={"csrf_token": "forged"})).status_code == 403
        token = re.search(r'name="csrf_token" value="([0-9a-f]+)"', tickets.text).group(1)
        assert (await c.post(f"/admin/tickets/{tid}/close", data={"csrf_token": token})).status_code == 303
    assert w.store.ticket(tid)["status"] == "closed"


@async_test
async def test_dashboard_escapes_member_text():
    w = make_world()
    await w.community.open_ticket(MAYA, "maya", "<script>alert(1)</script>", "x", "/ticket")
    async with client_for(w) as c:
        await _login(c)
        page = (await c.get("/admin/tickets")).text
    assert "<script>alert(1)</script>" not in page and "&lt;script&gt;" in page


@async_test
async def test_reconcile_page_dry_run_then_fix():
    w = make_world()
    await w.m.apply(P.normalize_stripe(checkout_completed()))
    w.fake.members["1300000000000000001"][MAYA]["roles"].remove(PREMIUM)
    async with client_for(w) as c:
        await _login(c)
        page = await c.get("/admin/reconcile")
        assert "add (paid, role missing)" in page.text and PREMIUM not in w.roles(MAYA)
        token = re.search(r'name="csrf_token" value="([0-9a-f]+)"', page.text).group(1)
        await c.post("/admin/reconcile", data={"csrf_token": token})
    assert PREMIUM in w.roles(MAYA)


@async_test
async def test_session_expires():
    w = make_world()
    async with client_for(w) as c:
        await _login(c)
        assert (await c.get("/admin")).status_code == 200
        w.clock.advance(13 * 3600)
        assert (await c.get("/admin")).status_code == 303


@async_test
async def test_health():
    w = make_world()
    async with client_for(w) as c:
        assert (await c.get("/health")).json() == {"ok": True}
