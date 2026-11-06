"""Calendly bookings to Discord. invitee.created gives the member a "Call booked" role
and posts a notice; invitee.canceled takes the role off when they have no other booking.

Finding the member: /book hands out the booking link with utm_content=<discord id>, and
Calendly sends UTM values back in payload.tracking. If that is missing, the answer to
the event type's "Discord username" question is matched against known members.

Signature: Calendly-Webhook-Signature: t=<unix>,v1=<hex HMAC-SHA256(key, "t.body")>."""
import hashlib
import hmac
import json
import time
from typing import Optional

from .payments import SignatureError, _digits


def verify_calendly(payload: bytes, header: Optional[str], key: str, tolerance: int = 300, now=time.time) -> dict:
    if not header or not key:
        raise SignatureError("missing Calendly-Webhook-Signature header or signing key")
    parts = dict(p.strip().split("=", 1) for p in header.split(",") if "=" in p)
    ts, sig = parts.get("t", ""), parts.get("v1", "")
    if not ts.isdigit() or not sig:
        raise SignatureError("malformed Calendly-Webhook-Signature header")
    expected = hmac.new(key.encode(), ts.encode() + b"." + payload, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        raise SignatureError("signature does not match")
    if abs(now() - int(ts)) > tolerance:
        raise SignatureError(f"timestamp outside the {tolerance}s window")
    return json.loads(payload)


def calendly_sign(payload: bytes, key: str, ts: Optional[int] = None) -> str:
    ts = int(time.time()) if ts is None else ts
    return f"t={ts},v1={hmac.new(key.encode(), str(ts).encode() + b'.' + payload, hashlib.sha256).hexdigest()}"


class Bookings:
    def __init__(self, settings, store, membership, hooks=None, post=None):
        self.s = settings
        self.store = store
        self.m = membership
        self.hooks = hooks
        self.post = post  # async callable(channel_id, text)

    def find_member(self, payload: dict) -> Optional[str]:
        tracking = payload.get("tracking") or {}
        uid = _digits(tracking.get("utm_content"))
        if uid:
            return uid
        wanted = self.s.calendly_question.strip().lower()
        for qa in payload.get("questions_and_answers") or []:
            if str(qa.get("question", "")).strip().lower() == wanted:
                answer = str(qa.get("answer", "")).strip()
                if _digits(answer):
                    return answer
                member = self.store.member_by_username(answer)
                if member:
                    return member["discord_user_id"]
        return None

    async def handle(self, body: dict) -> dict:
        event = body.get("event")
        payload = body.get("payload") or {}
        invitee = payload.get("uri") or ""
        if event not in ("invitee.created", "invitee.canceled") or not invitee:
            return {"status": "ignored", "event": event}
        # Calendly retries deliveries: the invitee URI plus the event name is unique.
        if not self.store.claim_event("calendly", f"{event}:{invitee}", event, int(time.time())):
            return {"status": "duplicate"}
        sched = payload.get("scheduled_event") or {}
        uid = self.find_member(payload)
        status = "active" if event == "invitee.created" else "canceled"
        self.store.upsert_booking(invitee, discord_user_id=uid, name=payload.get("name"), email=payload.get("email"),
                                  event_name=sched.get("name"), start_time=sched.get("start_time"), status=status)
        data = {"discord_user_id": uid, "name": payload.get("name"), "email": payload.get("email"),
                "event_name": sched.get("name"), "start_time": sched.get("start_time")}
        if self.hooks:
            self.hooks.emit("booking.created" if status == "active" else "booking.canceled", data, ref=invitee + event)
        result = {"status": "applied", "event": event, "discord_user_id": uid, "role_change": None}
        if not uid:
            result["status"] = "unmatched"
            if self.post and self.s.mod_channel_id:
                await self.post(self.s.mod_channel_id,
                                f"Calendly booking by {payload.get('name')} ({sched.get('name')}, "
                                f"{sched.get('start_time')}) could not be matched to a Discord member.")
            self.store.finish_event("calendly", f"{event}:{invitee}", "unmatched")
            return result
        role = self.s.call_booked_role_id
        if role:
            if status == "active":
                result["role_change"] = await self.m._change(uid, role, "add", "Calendly booking", invitee, "calendly")
            elif not self.store.active_bookings_for(uid):
                result["role_change"] = await self.m._change(uid, role, "remove", "Calendly booking canceled",
                                                             invitee, "calendly")
        if self.post and self.s.bookings_channel_id:
            when = (sched.get("start_time") or "")[:16].replace("T", " ")
            verb = "booked" if status == "active" else "canceled"
            await self.post(self.s.bookings_channel_id, f"<@{uid}> {verb} \"{sched.get('name')}\" for {when} UTC.")
        self.store.finish_event("calendly", f"{event}:{invitee}", result["status"])
        return result
