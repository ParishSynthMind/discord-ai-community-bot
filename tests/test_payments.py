import hashlib
import hmac
import json

import pytest

from community_bot import payments as P
from tests.conftest import MAYA, NOW, STRIPE_SECRET, checkout_completed, invoice_event, sub_event


def _sign(body, secret, ts):
    return P.stripe_sign(body, secret, ts)


# ------------------------------------------------------------------ Stripe signatures
def test_stripe_signature_accepts_valid():
    body = json.dumps(checkout_completed()).encode()
    ev = P.verify_stripe(body, _sign(body, STRIPE_SECRET, int(NOW)), [STRIPE_SECRET], now=lambda: NOW)
    assert ev["type"] == "checkout.session.completed"


def test_stripe_signature_rejects_wrong_secret():
    body = b'{"id":"evt_1"}'
    with pytest.raises(P.SignatureError, match="does not match"):
        P.verify_stripe(body, _sign(body, "whsec_other", int(NOW)), [STRIPE_SECRET], now=lambda: NOW)


def test_stripe_signature_rejects_tampered_body():
    body = b'{"id":"evt_1","amount":900}'
    header = _sign(body, STRIPE_SECRET, int(NOW))
    with pytest.raises(P.SignatureError):
        P.verify_stripe(body.replace(b"900", b"9"), header, [STRIPE_SECRET], now=lambda: NOW)


def test_stripe_signature_rejects_replay_outside_window():
    body = b'{"id":"evt_1"}'
    header = _sign(body, STRIPE_SECRET, int(NOW) - 301)
    with pytest.raises(P.SignatureError, match="window"):
        P.verify_stripe(body, header, [STRIPE_SECRET], tolerance=300, now=lambda: NOW)


def test_stripe_signature_missing_or_malformed():
    with pytest.raises(P.SignatureError, match="missing"):
        P.verify_stripe(b"{}", None, [STRIPE_SECRET])
    with pytest.raises(P.SignatureError, match="malformed"):
        P.verify_stripe(b"{}", "v1=abc", [STRIPE_SECRET])


def test_stripe_secret_rotation_any_v1_and_any_secret():
    body = b'{"id":"evt_rot"}'
    ts = int(NOW)
    good = hmac.new(b"whsec_new", f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    header = f"t={ts},v1={'0' * 64},v1={good}"
    assert P.verify_stripe(body, header, ["whsec_old", "whsec_new"], now=lambda: NOW)["id"] == "evt_rot"


# ------------------------------------------------------------------ Stripe events
def test_checkout_completed_is_activation_with_member_from_client_reference():
    ev = P.normalize_stripe(checkout_completed())
    assert (ev.kind, ev.discord_user_id, ev.customer_id, ev.subscription_id) == (P.ACTIVATED, MAYA, "cus_maya",
                                                                                   "sub_maya")
    assert ev.email == "maya@example.com" and ev.currency == "USD"


def test_checkout_uses_metadata_when_no_client_reference():
    e = checkout_completed(uid=None, obj={"metadata": {"discord_user_id": MAYA}})
    assert P.normalize_stripe(e).discord_user_id == MAYA


def test_checkout_rejects_non_snowflake_reference():
    e = checkout_completed(uid="robert'); DROP TABLE")
    assert P.normalize_stripe(e).discord_user_id is None


def test_unpaid_async_checkout_is_ignored_until_it_succeeds():
    e = checkout_completed(obj={"payment_status": "unpaid"})
    assert P.normalize_stripe(e).kind == P.IGNORED
    e["type"] = "checkout.session.async_payment_succeeded"
    e["data"]["object"]["payment_status"] = "paid"
    assert P.normalize_stripe(e).kind == P.ACTIVATED


def test_one_time_payment_checkout_is_ignored():
    assert P.normalize_stripe(checkout_completed(obj={"mode": "payment"})).kind == P.IGNORED


@pytest.mark.parametrize("type_,status,cape,kind", [
    ("customer.subscription.deleted", "canceled", False, P.CANCELED),
    ("customer.subscription.updated", "past_due", False, P.PAYMENT_FAILED),
    ("customer.subscription.updated", "unpaid", False, P.CANCELED),
    ("customer.subscription.updated", "active", True, P.CANCEL_SCHEDULED),
    ("customer.subscription.updated", "active", False, P.ACTIVATED),
    ("customer.subscription.created", "trialing", False, P.ACTIVATED),
    ("customer.subscription.updated", "incomplete", False, P.IGNORED),
])
def test_subscription_status_mapping(type_, status, cape, kind):
    ev = P.normalize_stripe(sub_event(type_, status=status, cancel_at_period_end=cape))
    assert ev.kind == kind and ev.subscription_id == "sub_maya" and ev.plan == "price_monthly"


def test_invoice_failed_reads_subscription_from_new_and_old_shapes():
    new = P.normalize_stripe(invoice_event("invoice.payment_failed", amount=900))
    assert (new.kind, new.subscription_id, new.amount_minor) == (P.PAYMENT_FAILED, "sub_maya", 900)
    old = invoice_event("invoice.payment_failed")
    old["data"]["object"].pop("parent")
    old["data"]["object"]["subscription"] = "sub_old"
    assert P.normalize_stripe(old).subscription_id == "sub_old"


def test_unknown_stripe_event_ignored():
    assert P.normalize_stripe({"id": "evt_x", "type": "charge.refunded", "data": {"object": {}}}).kind == P.IGNORED


# ------------------------------------------------------------------ Lemon Squeezy
def _ls(name, status="active", custom=None, typ="subscriptions"):
    return {"meta": {"event_name": name, "custom_data": custom or {"discord_user_id": MAYA}},
            "data": {"type": typ, "id": "4242", "attributes": {"customer_id": 77, "status": status,
                                                               "variant_id": 555, "user_email": "m@example.com",
                                                               "updated_at": "2026-11-06T10:00:00Z",
                                                               "subscription_id": 4242, "total": 900,
                                                               "currency": "USD"}}}


def test_lemonsqueezy_signature_and_mapping():
    body = json.dumps(_ls("subscription_created")).encode()
    sig = hmac.new(b"ls_secret", body, hashlib.sha256).hexdigest()
    parsed = P.verify_lemonsqueezy(body, sig, "ls_secret")
    ev = P.normalize_lemonsqueezy(parsed, body)
    assert (ev.kind, ev.discord_user_id, ev.customer_id, ev.plan) == (P.ACTIVATED, MAYA, "77", "555")
    with pytest.raises(P.SignatureError):
        P.verify_lemonsqueezy(body, "0" * 64, "ls_secret")


@pytest.mark.parametrize("name,status,kind", [
    ("subscription_cancelled", "cancelled", P.CANCEL_SCHEDULED), ("subscription_expired", "expired", P.CANCELED),
    ("subscription_payment_failed", "past_due", P.PAYMENT_FAILED), ("subscription_updated", "past_due", P.PAYMENT_FAILED),
    ("order_created", "paid", P.IGNORED)])
def test_lemonsqueezy_kinds(name, status, kind):
    body = json.dumps(_ls(name, status)).encode()
    assert P.normalize_lemonsqueezy(json.loads(body), body).kind == kind


def test_lemonsqueezy_same_body_gives_same_event_id():
    body = json.dumps(_ls("subscription_created")).encode()
    a = P.normalize_lemonsqueezy(json.loads(body), body)
    b = P.normalize_lemonsqueezy(json.loads(body), body)
    assert a.event_id == b.event_id


# ------------------------------------------------------------------ Paddle
def _paddle(event_type, status="active", **data):
    return {"event_id": "evt_01pdl", "event_type": event_type, "occurred_at": "2026-11-06T10:00:00Z",
            "data": {"id": "sub_01", "status": status, "customer_id": "ctm_01", "currency_code": "USD",
                     "custom_data": {"discord_user_id": MAYA}, "items": [{"price": {"id": "pri_01"}}], **data}}


def test_paddle_signature_and_mapping():
    body = json.dumps(_paddle("subscription.activated")).encode()
    ts = int(NOW)
    h1 = hmac.new(b"pdl_secret", f"{ts}:".encode() + body, hashlib.sha256).hexdigest()
    parsed = P.verify_paddle(body, f"ts={ts};h1={h1}", "pdl_secret", now=lambda: NOW)
    ev = P.normalize_paddle(parsed)
    assert (ev.kind, ev.discord_user_id, ev.subscription_id, ev.plan) == (P.ACTIVATED, MAYA, "sub_01", "pri_01")
    old = ts - 1000
    h1_old = hmac.new(b"pdl_secret", f"{old}:".encode() + body, hashlib.sha256).hexdigest()
    with pytest.raises(P.SignatureError, match="window"):
        P.verify_paddle(body, f"ts={old};h1={h1_old}", "pdl_secret", now=lambda: NOW)


def test_paddle_scheduled_cancel_and_failed_transaction():
    ev = P.normalize_paddle(_paddle("subscription.updated", scheduled_change={"action": "cancel"}))
    assert ev.kind == P.CANCEL_SCHEDULED
    txn = _paddle("transaction.payment_failed", subscription_id="sub_01", details={"totals": {"grand_total": "900"}})
    ev = P.normalize_paddle(txn)
    assert (ev.kind, ev.subscription_id, ev.amount_minor) == (P.PAYMENT_FAILED, "sub_01", 900)
