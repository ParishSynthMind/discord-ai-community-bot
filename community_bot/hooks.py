"""Outbound events for n8n, Make, Zapier or a CRM. Each event is stored in an outbox
first, then POSTed as JSON, signed so the receiver can check it came from this bot:

    X-Community-Timestamp: <unix seconds>
    X-Community-Signature: sha256=<hex HMAC-SHA256(secret, "<timestamp>.<body>")>
    Idempotency-Key: <event id>   (the same id on every retry, so the receiver can dedupe)

A 2xx marks it sent. 429, 5xx and network errors are retried with backoff; any other
4xx is a setup problem (wrong URL, receiver refuses) and is parked as dead."""
import asyncio
import hashlib
import hmac
import json
import logging
import time
import uuid
from typing import Optional

import httpx

log = logging.getLogger("community_bot.hooks")
MAX_ATTEMPTS = 8


def sign(secret: str, timestamp: int, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), str(timestamp).encode() + b"." + body, hashlib.sha256).hexdigest()


class Hooks:
    def __init__(self, settings, store, secret: Optional[str], client: Optional[httpx.AsyncClient] = None,
                 clock=None):
        self.s = settings
        self.store = store
        self.secret = secret
        self.client = client
        self.clock = clock or store.clock
        # The web server flushes after each webhook and the worker flushes every 30 s. Without
        # this lock two flushes can both pick up the same pending row and send it twice.
        self._lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.s.hook_url)

    def emit(self, event: str, data: dict, ref: Optional[str] = None) -> Optional[str]:
        """Queue an event. `ref` makes the id deterministic, so the same cause (say, one
        Stripe event) can never queue the same hook twice."""
        if not self.enabled or (self.s.hook_events and event not in self.s.hook_events):
            return None
        hook_id = ("evt_" + hashlib.sha256(f"{event}|{ref}".encode()).hexdigest()[:24]) if ref else \
            "evt_" + uuid.uuid4().hex[:24]
        body = json.dumps({"id": hook_id, "type": event, "occurred_at": int(self.clock()),
                           "community": self.s.community_name, "data": data}, separators=(",", ":"))
        return hook_id if self.store.enqueue_hook(hook_id, event, body) else None

    async def flush(self) -> dict:
        if not self.enabled:
            return {"sent": 0, "failed": 0}
        async with self._lock:
            return await self._flush()

    async def _flush(self) -> dict:
        sent = failed = 0
        client = self.client or httpx.AsyncClient(timeout=10)
        try:
            for row in self.store.due_hooks():
                body = row["body"].encode()
                ts = int(self.clock())
                headers = {"Content-Type": "application/json", "X-Community-Event": row["event"],
                           "X-Community-Timestamp": str(ts), "Idempotency-Key": row["id"],
                           "User-Agent": "discord-ai-community-bot/0.1"}
                if self.secret:
                    headers["X-Community-Signature"] = sign(self.secret, ts, body)
                try:
                    resp = await client.post(self.s.hook_url, content=body, headers=headers)
                    status, err = resp.status_code, f"HTTP {resp.status_code}"
                except httpx.HTTPError as exc:
                    status, err = 0, f"network error: {exc.__class__.__name__}"
                if 200 <= status < 300:
                    self.store.update_hook(row["id"], "sent")
                    sent += 1
                elif (status in (0, 429) or status >= 500) and row["attempts"] + 1 < MAX_ATTEMPTS:
                    self.store.update_hook(row["id"], "pending", err, delay=min(15 * 2 ** row["attempts"], 3600))
                    failed += 1
                else:
                    self.store.update_hook(row["id"], "dead", err)
                    log.warning("hook %s parked as dead: %s", row["id"], err)
                    failed += 1
        finally:
            if self.client is None:
                await client.aclose()
        return {"sent": sent, "failed": failed}


def verify(secret: str, timestamp: str, body: bytes, signature: str, tolerance: int = 300, now=time.time) -> bool:
    """What the receiving side (an n8n Code node, say) does. Kept here for the tests and the docs."""
    if not timestamp.isdigit() or abs(now() - int(timestamp)) > tolerance:
        return False
    return hmac.compare_digest(sign(secret, int(timestamp), body), signature)
