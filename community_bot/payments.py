"""Payment webhooks: check the signature on the raw bytes, then turn each provider's
event into one small NormalizedEvent the membership code understands. Stripe is the
main one; Lemon Squeezy and Paddle are here because Stripe is not open to every seller,
and the role logic should not care which one sends the money."""
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Callable, Iterable, Optional

# What happened, as far as a member's role is concerned.
ACTIVATED = "activated"            # paid and active: role on
PAYMENT_SUCCEEDED = "payment_succeeded"
PAYMENT_FAILED = "payment_failed"  # renewal failed: grace period, then role off
CANCEL_SCHEDULED = "cancel_scheduled"  # will end at the period end: role stays for now
CANCELED = "canceled"              # ended: role off
IGNORED = "ignored"


class SignatureError(Exception):
    pass


@dataclass
class NormalizedEvent:
    provider: str
    event_id: str
    type: str
    kind: str
    created: int
    customer_id: Optional[str] = None
    subscription_id: Optional[str] = None
    discord_user_id: Optional[str] = None
    email: Optional[str] = None
    amount_minor: Optional[int] = None
    currency: Optional[str] = None
    plan: Optional[str] = None


def _hmac_hex(secret: str, message: bytes) -> str:
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def _parse_kv(header: str, item_sep: str, kv_sep: str = "="):
    pairs = []
    for part in header.split(item_sep):
        if kv_sep in part:
            k, v = part.strip().split(kv_sep, 1)
            pairs.append((k.strip(), v.strip()))
    return pairs


def _check_age(ts: int, tolerance: int, now: float):
    if abs(now - ts) > tolerance:
        raise SignatureError(f"timestamp outside the {tolerance}s window (replayed or clock skew)")


def _digits(value) -> Optional[str]:
    value = str(value or "").strip()
    return value if value.isdigit() and 15 <= len(value) <= 21 else None  # a Discord snowflake


# --------------------------------------------------------------------------- Stripe
def verify_stripe(payload: bytes, header: Optional[str], secrets: Iterable[str], tolerance: int = 300,
                  now: Callable[[], float] = time.time) -> dict:
    """Stripe-Signature: t=<unix>,v1=<hex>[,v1=<hex>...]. v1 = HMAC-SHA256(secret, "t.payload").
    Several v1 values appear while a secret is being rolled, and several secrets can be
    configured for the same reason."""
    if not header:
        raise SignatureError("missing Stripe-Signature header")
    pairs = _parse_kv(header, ",")
    ts = next((v for k, v in pairs if k == "t"), None)
    sigs = [v for k, v in pairs if k == "v1"]
    if not ts or not ts.isdigit() or not sigs:
        raise SignatureError("malformed Stripe-Signature header")
    signed = ts.encode() + b"." + payload
    if not any(hmac.compare_digest(_hmac_hex(s, signed), sig) for s in secrets if s for sig in sigs):
        raise SignatureError("signature does not match")
    _check_age(int(ts), tolerance, now())
    return json.loads(payload)


def stripe_sign(payload: bytes, secret: str, ts: Optional[int] = None) -> str:
    ts = int(time.time()) if ts is None else ts
    return f"t={ts},v1={_hmac_hex(secret, str(ts).encode() + b'.' + payload)}"


def _stripe_sub_id(invoice: dict) -> Optional[str]:
    # Older API versions put it on the invoice; newer ones under parent.subscription_details.
    if isinstance(invoice.get("subscription"), str):
        return invoice["subscription"]
    details = (invoice.get("parent") or {}).get("subscription_details") or {}
    return details.get("subscription")


def _stripe_price(obj: dict) -> Optional[str]:
    items = ((obj.get("items") or {}).get("data")) or []
    if items and isinstance(items[0].get("price"), dict):
        return items[0]["price"].get("id")
    return None


def normalize_stripe(event: dict) -> NormalizedEvent:
    obj = (event.get("data") or {}).get("object") or {}
    etype = event.get("type", "")
    base = dict(provider="stripe", event_id=event["id"], type=etype, created=int(event.get("created") or 0))
    meta = obj.get("metadata") or {}
    if etype in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
        paid = obj.get("payment_status") in ("paid", "no_payment_required")
        if obj.get("mode") != "subscription" or not paid:
            return NormalizedEvent(kind=IGNORED, **base)  # unpaid async methods finish later
        return NormalizedEvent(
            kind=ACTIVATED, customer_id=obj.get("customer"), subscription_id=obj.get("subscription"),
            discord_user_id=_digits(obj.get("client_reference_id")) or _digits(meta.get("discord_user_id")),
            email=(obj.get("customer_details") or {}).get("email"), amount_minor=obj.get("amount_total"),
            currency=(obj.get("currency") or "").upper() or None, plan=meta.get("plan"), **base)
    if etype in ("customer.subscription.created", "customer.subscription.updated", "customer.subscription.deleted"):
        status = obj.get("status")
        if etype == "customer.subscription.deleted" or status in ("canceled", "unpaid", "incomplete_expired"):
            kind = CANCELED
        elif status == "past_due":
            kind = PAYMENT_FAILED
        elif status in ("active", "trialing"):
            kind = CANCEL_SCHEDULED if obj.get("cancel_at_period_end") else ACTIVATED
        else:
            kind = IGNORED  # incomplete, paused: wait for the next event
        return NormalizedEvent(kind=kind, customer_id=obj.get("customer"), subscription_id=obj.get("id"),
                               discord_user_id=_digits(meta.get("discord_user_id")), plan=_stripe_price(obj), **base)
    if etype in ("invoice.payment_failed", "invoice.paid"):
        return NormalizedEvent(
            kind=PAYMENT_FAILED if etype == "invoice.payment_failed" else PAYMENT_SUCCEEDED,
            customer_id=obj.get("customer"), subscription_id=_stripe_sub_id(obj),
            email=obj.get("customer_email"),
            amount_minor=obj.get("amount_due") if etype == "invoice.payment_failed" else obj.get("amount_paid"),
            currency=(obj.get("currency") or "").upper() or None, **base)
    return NormalizedEvent(kind=IGNORED, **base)


# --------------------------------------------------------------------- Lemon Squeezy
def verify_lemonsqueezy(payload: bytes, header: Optional[str], secret: str) -> dict:
    """X-Signature: hex HMAC-SHA256(secret, raw body). There is no timestamp in it, so
    replays are stopped by the event store, not by age."""
    if not header or not secret:
        raise SignatureError("missing X-Signature header or secret")
    if not hmac.compare_digest(_hmac_hex(secret, payload), header.strip()):
        raise SignatureError("signature does not match")
    return json.loads(payload)


def normalize_lemonsqueezy(body: dict, raw: bytes) -> NormalizedEvent:
    meta = body.get("meta") or {}
    name = meta.get("event_name", "")
    data = body.get("data") or {}
    attrs = data.get("attributes") or {}
    custom = meta.get("custom_data") or {}
    # No event id is sent: hash the delivery so a redelivery of the same body is caught.
    event_id = meta.get("webhook_id") or hashlib.sha256(raw).hexdigest()[:32]
    created = int(_iso_to_unix(attrs.get("updated_at") or attrs.get("created_at")) or time.time())
    kinds = {"subscription_created": ACTIVATED, "subscription_resumed": ACTIVATED,
             "subscription_payment_success": PAYMENT_SUCCEEDED, "subscription_payment_failed": PAYMENT_FAILED,
             "subscription_cancelled": CANCEL_SCHEDULED, "subscription_expired": CANCELED}
    kind = kinds.get(name, IGNORED)
    if name == "subscription_updated":
        kind = {"active": ACTIVATED, "on_trial": ACTIVATED, "past_due": PAYMENT_FAILED, "unpaid": CANCELED,
                "cancelled": CANCEL_SCHEDULED, "expired": CANCELED}.get(attrs.get("status"), IGNORED)
    is_invoice = data.get("type") == "subscription-invoices"
    return NormalizedEvent(
        provider="lemonsqueezy", event_id=str(event_id), type=name, kind=kind, created=created,
        customer_id=str(attrs.get("customer_id")) if attrs.get("customer_id") else None,
        subscription_id=str(attrs.get("subscription_id") if is_invoice else data.get("id") or "") or None,
        discord_user_id=_digits(custom.get("discord_user_id")), email=attrs.get("user_email"),
        amount_minor=attrs.get("total") if is_invoice else None,
        currency=(attrs.get("currency") or "").upper() or None,
        plan=str(attrs.get("variant_id")) if attrs.get("variant_id") else None)


# ------------------------------------------------------------------------- Paddle
def verify_paddle(payload: bytes, header: Optional[str], secret: str, tolerance: int = 300,
                  now: Callable[[], float] = time.time) -> dict:
    """Paddle-Signature: ts=<unix>;h1=<hex>, h1 = HMAC-SHA256(secret, "ts:payload")."""
    if not header or not secret:
        raise SignatureError("missing Paddle-Signature header or secret")
    pairs = _parse_kv(header, ";")
    ts = next((v for k, v in pairs if k == "ts"), None)
    sigs = [v for k, v in pairs if k == "h1"]
    if not ts or not ts.isdigit() or not sigs:
        raise SignatureError("malformed Paddle-Signature header")
    expected = _hmac_hex(secret, ts.encode() + b":" + payload)
    if not any(hmac.compare_digest(expected, s) for s in sigs):
        raise SignatureError("signature does not match")
    _check_age(int(ts), tolerance, now())
    return json.loads(payload)


def normalize_paddle(body: dict) -> NormalizedEvent:
    etype = body.get("event_type", "")
    data = body.get("data") or {}
    custom = data.get("custom_data") or {}
    created = int(_iso_to_unix(body.get("occurred_at")) or time.time())
    kinds = {"subscription.activated": ACTIVATED, "subscription.resumed": ACTIVATED,
             "subscription.past_due": PAYMENT_FAILED, "subscription.canceled": CANCELED,
             "transaction.completed": PAYMENT_SUCCEEDED, "transaction.payment_failed": PAYMENT_FAILED}
    kind = kinds.get(etype, IGNORED)
    if etype == "subscription.updated":
        if (data.get("scheduled_change") or {}).get("action") == "cancel":
            kind = CANCEL_SCHEDULED
        else:
            kind = {"active": ACTIVATED, "trialing": ACTIVATED, "past_due": PAYMENT_FAILED,
                    "canceled": CANCELED, "paused": CANCELED}.get(data.get("status"), IGNORED)
    is_txn = etype.startswith("transaction.")
    totals = (data.get("details") or {}).get("totals") or {}
    items = data.get("items") or []
    price = (items[0].get("price") or {}).get("id") if items else None
    return NormalizedEvent(
        provider="paddle", event_id=body.get("event_id", ""), type=etype, kind=kind, created=created,
        customer_id=data.get("customer_id"),
        subscription_id=data.get("subscription_id") if is_txn else data.get("id"),
        discord_user_id=_digits(custom.get("discord_user_id")),
        amount_minor=int(totals["grand_total"]) if is_txn and totals.get("grand_total") else None,
        currency=data.get("currency_code"), plan=price)


def _iso_to_unix(value) -> Optional[float]:
    if not value:
        return None
    from datetime import datetime
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None
