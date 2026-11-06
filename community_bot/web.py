"""The web side: signed payment and Calendly webhooks, and a small admin dashboard.

Webhook rules: the signature is checked on the raw bytes before anything is parsed,
bodies over 1 MB are refused, and an event that fails part-way is released so the
provider's own retry processes it again. Duplicates answer 200 and do nothing."""
import hashlib
import hmac
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import BackgroundTasks, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import payments as P
from .calendly import verify_calendly
from .community import Community
from .discord_rest import DiscordError

log = logging.getLogger("community_bot.web")
MAX_BODY = 1_000_000
SESSION_COOKIE = "cb_admin"
SESSION_HOURS = 12
TEMPLATES = Environment(loader=FileSystemLoader(Path(__file__).parent / "templates"),
                        autoescape=select_autoescape(["html"]))


def _when(ts) -> str:
    if not ts:
        return ""
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def _money(minor, currency) -> str:
    if minor is None:
        return ""
    return f"{int(minor) / 100:,.2f} {currency or ''}".strip()


TEMPLATES.filters["when"] = _when


def create_app(c: Community, clock=time.time) -> FastAPI:
    app = FastAPI(title="Discord AI community bot", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.community = c

    async def body_or_413(request: Request) -> Optional[bytes]:
        if int(request.headers.get("content-length") or 0) > MAX_BODY:
            return None
        raw = await request.body()
        return raw if len(raw) <= MAX_BODY else None

    async def run_event(ev: P.NormalizedEvent, background: BackgroundTasks) -> JSONResponse:
        try:
            result = await c.membership.apply(ev)
        except Exception as exc:
            return JSONResponse({"error": f"processing failed, retry later: {exc.__class__.__name__}"}, 500)
        background.add_task(c.hooks.flush)
        return JSONResponse({"received": True, **{k: v for k, v in result.items() if k != "discord_user_id"}})

    @app.get("/health")
    async def health():
        return {"ok": True}

    @app.post("/webhooks/stripe")
    async def stripe(request: Request, background: BackgroundTasks):
        raw = await body_or_413(request)
        if raw is None:
            return JSONResponse({"error": "body too large"}, 413)
        secrets = [s.strip() for s in (c.secrets.get("STRIPE_WEBHOOK_SECRET") or "").split(",") if s.strip()]
        if not secrets:
            return JSONResponse({"error": "Stripe webhook secret not configured"}, 503)
        try:
            event = P.verify_stripe(raw, request.headers.get("stripe-signature"), secrets,
                                    c.s.webhook_tolerance, now=clock)
        except (P.SignatureError, ValueError) as exc:
            return JSONResponse({"error": str(exc)}, 400)
        return await run_event(P.normalize_stripe(event), background)

    @app.post("/webhooks/lemonsqueezy")
    async def lemonsqueezy(request: Request, background: BackgroundTasks):
        raw = await body_or_413(request)
        if raw is None:
            return JSONResponse({"error": "body too large"}, 413)
        try:
            body = P.verify_lemonsqueezy(raw, request.headers.get("x-signature"),
                                         c.secrets.get("LEMONSQUEEZY_SIGNING_SECRET") or "")
        except (P.SignatureError, ValueError) as exc:
            return JSONResponse({"error": str(exc)}, 401)
        return await run_event(P.normalize_lemonsqueezy(body, raw), background)

    @app.post("/webhooks/paddle")
    async def paddle(request: Request, background: BackgroundTasks):
        raw = await body_or_413(request)
        if raw is None:
            return JSONResponse({"error": "body too large"}, 413)
        try:
            body = P.verify_paddle(raw, request.headers.get("paddle-signature"),
                                   c.secrets.get("PADDLE_WEBHOOK_SECRET") or "", c.s.webhook_tolerance, now=clock)
        except (P.SignatureError, ValueError) as exc:
            return JSONResponse({"error": str(exc)}, 401)
        return await run_event(P.normalize_paddle(body), background)

    @app.post("/webhooks/calendly")
    async def calendly(request: Request, background: BackgroundTasks):
        raw = await body_or_413(request)
        if raw is None:
            return JSONResponse({"error": "body too large"}, 413)
        try:
            body = verify_calendly(raw, request.headers.get("calendly-webhook-signature"),
                                   c.secrets.get("CALENDLY_SIGNING_KEY") or "", c.s.webhook_tolerance, now=clock)
        except (P.SignatureError, ValueError) as exc:
            return JSONResponse({"error": str(exc)}, 401)
        try:
            result = await c.bookings.handle(body)
        except DiscordError as exc:
            return JSONResponse({"error": f"discord: {exc.message}"}, 500)
        background.add_task(c.hooks.flush)
        return {"received": True, **{k: v for k, v in result.items() if k != "discord_user_id"}}

    # ------------------------------------------------------------------ admin
    def admin_token() -> Optional[str]:
        return c.secrets.get("ADMIN_TOKEN")

    def session_value(exp: int) -> str:
        sig = hmac.new(admin_token().encode(), f"session:{exp}".encode(), hashlib.sha256).hexdigest()
        return f"{exp}.{sig}"

    def logged_in(request: Request) -> bool:
        token, cookie = admin_token(), request.cookies.get(SESSION_COOKIE, "")
        if not token or "." not in cookie:
            return False
        exp = cookie.split(".", 1)[0]
        return exp.isdigit() and int(exp) > clock() and hmac.compare_digest(session_value(int(exp)), cookie)

    def csrf(request: Request) -> str:
        return hmac.new(admin_token().encode(), b"csrf:" + request.cookies.get(SESSION_COOKIE, "").encode(),
                        hashlib.sha256).hexdigest()

    def render(name: str, request: Request, **ctx) -> HTMLResponse:
        ctx.setdefault("community", c.s.community_name)
        ctx["csrf"] = csrf(request) if logged_in(request) else ""
        resp = HTMLResponse(TEMPLATES.get_template(name).render(**ctx))
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'unsafe-inline'"
        resp.headers["Cache-Control"] = "no-store"
        return resp

    def guard(request: Request):
        if not admin_token():
            return HTMLResponse("Dashboard disabled: set ADMIN_TOKEN.", 503)
        if not logged_in(request):
            return RedirectResponse("/admin/login", 303)
        return None

    def check_csrf(request: Request, token: str) -> bool:
        return logged_in(request) and hmac.compare_digest(csrf(request), token or "")

    @app.get("/admin/login", response_class=HTMLResponse)
    async def login_page(request: Request):
        if not admin_token():
            return HTMLResponse("Dashboard disabled: set ADMIN_TOKEN.", 503)
        return render("login.html", request, error="")

    @app.post("/admin/login")
    async def login(request: Request, token: str = Form("")):
        if not admin_token():
            return HTMLResponse("Dashboard disabled: set ADMIN_TOKEN.", 503)
        if not hmac.compare_digest(token.encode(), admin_token().encode()):
            return render("login.html", request, error="That token is not right.")
        resp = RedirectResponse("/admin", 303)
        resp.set_cookie(SESSION_COOKIE, session_value(int(clock()) + SESSION_HOURS * 3600), httponly=True,
                        samesite="strict", secure=request.url.scheme == "https", max_age=SESSION_HOURS * 3600)
        return resp

    @app.post("/admin/logout")
    async def logout():
        resp = RedirectResponse("/admin/login", 303)
        resp.delete_cookie(SESSION_COOKIE)
        return resp

    def overview_data() -> dict:
        s = c.store
        links = s.links()
        day = datetime.fromtimestamp(clock(), tz=timezone.utc).strftime("%Y-%m-%d")
        month = day[:7]
        paid = [p for p in s.payments(5000) if p["kind"] == "paid" and _when(p["at"]).startswith(month)]
        by_cur = {}
        for p in paid:
            by_cur[p["currency"]] = by_cur.get(p["currency"], 0) + (p["amount_minor"] or 0)
        return {
            "premium_active": sum(1 for x in links if c.membership.wants_role(x)),
            "past_due": sum(1 for x in links if x["status"] == "past_due"),
            "canceled": sum(1 for x in links if x["status"] in ("canceled", "lapsed")),
            "members": len([m for m in s.members() if not m["left_at"]]),
            "open_tickets": len(s.tickets("open")),
            "ai_today": s.ai_calls_today(day), "ai_cap": c.s.ai.daily_cap_total,
            "revenue_month": ", ".join(_money(v, k) for k, v in by_cur.items()) or "0.00",
            "role_failed": len(s.role_ops("failed")), "role_pending": len(s.role_ops("pending")),
            "hooks_pending": sum(1 for h in s.hooks() if h["status"] == "pending"),
            "hooks_dead": sum(1 for h in s.hooks() if h["status"] == "dead"),
            "needs_attention": [a for a in s.audit_log(500) if a["result"] not in ("ok", "ok (retried)")][:8],
        }

    @app.get("/admin", response_class=HTMLResponse)
    async def overview(request: Request):
        return guard(request) or render("overview.html", request, page="overview", d=overview_data())

    @app.get("/admin/api/summary")
    async def summary(request: Request):
        if guard(request):
            return JSONResponse({"error": "login required"}, 401)
        d = overview_data()
        d["needs_attention"] = len(d["needs_attention"])
        return d

    @app.get("/admin/members", response_class=HTMLResponse)
    async def members(request: Request):
        if (g := guard(request)):
            return g
        names = {m["discord_user_id"]: m for m in c.store.members()}
        rows = []
        for x in c.store.links():
            m = names.get(x["discord_user_id"]) or {}
            rows.append([m.get("username") or "", x["discord_user_id"] or "(not linked)", x["status"],
                         "yes" if c.membership.wants_role(x) else "no", x["provider"], x["customer_id"],
                         x["plan"] or "", _when(x["updated_at"])])
        linked = {x["discord_user_id"] for x in c.store.links()}
        for uid, m in names.items():
            if uid not in linked:
                rows.append([m["username"], uid, "no membership", "no", "", "", "",
                             _when(m["joined_at"]) + (" (left)" if m["left_at"] else "")])
        return render("table.html", request, page="members", title="Members and memberships",
                      columns=["Member", "Discord id", "Status", "Premium role", "Provider", "Customer", "Plan",
                               "Updated"], rows=rows)

    @app.get("/admin/payments", response_class=HTMLResponse)
    async def payments_page(request: Request):
        if (g := guard(request)):
            return g
        names = {m["discord_user_id"]: m["username"] for m in c.store.members()}
        rows = [[_when(p["at"]), p["provider"], p["kind"], _money(p["amount_minor"], p["currency"]),
                 names.get(p["discord_user_id"], p["discord_user_id"] or "(not linked)"), p["customer_id"] or "",
                 p["event_id"]] for p in c.store.payments()]
        events = [[_when(e["received_at"]), e["provider"], e["type"], e["status"], e["event_id"]]
                  for e in c.store.events(100)]
        return render("table.html", request, page="payments", title="Payments",
                      columns=["When", "Provider", "Kind", "Amount", "Member", "Customer", "Event"], rows=rows,
                      extra_title="Webhook events (latest 100)",
                      extra_columns=["Received", "Provider", "Type", "Outcome", "Event id"], extra_rows=events)

    @app.get("/admin/tickets", response_class=HTMLResponse)
    async def tickets(request: Request):
        if (g := guard(request)):
            return g
        return render("tickets.html", request, page="tickets", tickets=c.store.tickets())

    @app.post("/admin/tickets/{ticket_id}/close")
    async def close(request: Request, ticket_id: int, csrf_token: str = Form("")):
        if (g := guard(request)):
            return g
        if not check_csrf(request, csrf_token):
            return HTMLResponse("Bad form token.", 403)
        await c.close_ticket(ticket_id, "dashboard")
        await c.hooks.flush()
        return RedirectResponse("/admin/tickets", 303)

    @app.get("/admin/audit", response_class=HTMLResponse)
    async def audit(request: Request):
        if (g := guard(request)):
            return g
        rows = [[_when(a["at"]), a["actor"], a["action"], a["discord_user_id"] or "", a["role_id"] or "",
                 a["reason"] or "", a["result"]] for a in c.store.audit_log()]
        hooks = [[_when(h["created_at"]), h["event"], h["status"], h["attempts"], h["last_error"] or ""]
                 for h in c.store.hooks()[:100]]
        return render("table.html", request, page="audit", title="Audit log",
                      columns=["When", "Actor", "Action", "Member", "Role", "Reason", "Result"], rows=rows,
                      extra_title="Outbound hooks (n8n / Make / CRM)",
                      extra_columns=["Queued", "Event", "Status", "Attempts", "Last error"], extra_rows=hooks)

    @app.get("/admin/reconcile", response_class=HTMLResponse)
    async def reconcile_page(request: Request):
        if (g := guard(request)):
            return g
        try:
            plan = await c.membership.reconcile(apply=False)
            error = ""
        except DiscordError as exc:
            plan, error = {"add": [], "remove": [], "unpaid_role": [], "checked": 0}, exc.message
        return render("reconcile.html", request, page="reconcile", plan=plan, error=error,
                      members_intent=c.s.members_intent)

    @app.post("/admin/reconcile")
    async def reconcile_apply(request: Request, csrf_token: str = Form("")):
        if (g := guard(request)):
            return g
        if not check_csrf(request, csrf_token):
            return HTMLResponse("Bad form token.", 403)
        await c.membership.reconcile(apply=True)
        await c.hooks.flush()
        return RedirectResponse("/admin/reconcile", 303)

    @app.exception_handler(json.JSONDecodeError)
    async def bad_json(request: Request, exc):  # pragma: no cover
        return JSONResponse({"error": "invalid JSON"}, 400)

    return app
