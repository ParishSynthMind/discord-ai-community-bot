"""community-bot: check the setup, run the bot and the web server, reconcile roles, report."""
import argparse
import asyncio
import json
import logging
import sys
from typing import List, Optional

from .config import Secrets, load_settings
from .discord_rest import (
    EMBED_LINKS,
    MANAGE_ROLES,
    SEND_MESSAGES,
    VIEW_CHANNEL,
    DiscordError,
    DiscordREST,
    channel_permissions,
    guild_permissions,
    invite_url,
    role_position,
    role_rank,
)

GATEWAY_MEMBERS_FLAGS = (1 << 14) | (1 << 15)  # GATEWAY_GUILD_MEMBERS, _LIMITED


async def check(settings, secrets, rest: Optional[DiscordREST] = None, out=print) -> bool:
    """Read-only. Every line is OK, WARN or FAIL, with what to do about a FAIL."""
    ok = True

    def line(status, text):
        nonlocal ok
        ok = ok and status != "FAIL"
        out(f"{status:<5} {text}")

    token = secrets.get("DISCORD_BOT_TOKEN")
    if not token and rest is None:
        line("FAIL", "no bot token: set DISCORD_BOT_TOKEN or the Keychain item discord-bot-token")
        return False
    rest = rest or DiscordREST(token, base=settings.api_base)
    try:
        me = await rest.me()
        line("OK", f"token works: logged in as {me['username']} (id {me['id']})")
        app = await rest.application()
        members_on = bool(int(app.get("flags") or 0) & GATEWAY_MEMBERS_FLAGS)
        if settings.members_intent and not members_on:
            line("FAIL", "config asks for the Server Members intent but it is off in the Developer Portal "
                         "(Bot > Privileged Gateway Intents)")
        else:
            line("OK", f"Server Members intent: {'on' if members_on else 'off'} in the portal, "
                       f"{'used' if settings.members_intent else 'not used'} by config")
        guild = await rest.guild(settings.guild_id)
        line("OK", f"server: {guild['name']}")
        roles = await rest.roles(settings.guild_id)
        bot_member = await rest.member(settings.guild_id, me["id"])
        perms = guild_permissions(guild, roles, bot_member["roles"])
        top = role_position(roles, bot_member["roles"])
        if perms & MANAGE_ROLES:
            line("OK", "bot has Manage Roles")
        else:
            line("FAIL", "bot is missing Manage Roles: re-invite it with the link from `community-bot invite-url`")
        for label, rid in (("Premium", settings.premium_role_id), ("Call booked", settings.call_booked_role_id)):
            if not rid:
                continue
            role = next((r for r in roles if r["id"] == rid), None)
            if not role:
                line("FAIL", f"{label} role {rid} does not exist in this server (check config/bot.yaml)")
            elif role.get("managed"):
                line("FAIL", f"{label} role '{role['name']}' is managed by an integration and can't be given")
            elif role_rank(role) >= top and perms != ~0:
                line("FAIL", f"{label} role '{role['name']}' is above the bot's own role: in Server Settings > Roles, "
                             "drag the bot's role above it")
            else:
                line("OK", f"{label} role '{role['name']}' can be given and removed by the bot")
        for label, cid in (("mod queue", settings.mod_channel_id), ("bookings", settings.bookings_channel_id)):
            if not cid:
                continue
            try:
                ch = await rest.channel(cid)
            except DiscordError as exc:
                line("FAIL", f"{label} channel {cid}: {exc.message}")
                continue
            cperm = channel_permissions(guild, roles, bot_member["roles"], ch, me["id"])
            need = VIEW_CHANNEL | SEND_MESSAGES | EMBED_LINKS
            if cperm & need == need:
                line("OK", f"{label} channel #{ch.get('name')}: bot can read and post")
            else:
                line("FAIL", f"{label} channel #{ch.get('name')}: bot can't post there (needs View Channel, "
                             "Send Messages and Embed Links in that channel)")
        line("OK", f"invite link (minimal permissions): {invite_url(me['id'], settings.guild_id)}")
    except DiscordError as exc:
        if exc.status == 401:
            line("FAIL", "Discord refused the token (401): reset it in the Developer Portal and store it again")
        else:
            line("FAIL", f"Discord said: {exc}")
    provider_key = {"anthropic": "ANTHROPIC_API_KEY", "gemini": "GEMINI_API_KEY"}.get(settings.ai.provider)
    if provider_key:
        line("OK" if secrets.get(provider_key) else "FAIL",
             f"AI provider {settings.ai.provider} ({settings.ai.model}): key {'found' if secrets.get(provider_key) else 'missing'}")
    for name in ("STRIPE_WEBHOOK_SECRET", "LEMONSQUEEZY_SIGNING_SECRET", "PADDLE_WEBHOOK_SECRET",
                 "CALENDLY_SIGNING_KEY", "HOOK_SIGNING_SECRET", "ADMIN_TOKEN"):
        line("OK" if secrets.get(name) else "WARN", f"{name}: {'set' if secrets.get(name) else 'not set'}")
    return ok


async def run(settings, secrets, host: str, port: int, gateway: bool = True):
    import uvicorn

    from .bot import run_bot
    from .community import Community
    from .web import create_app

    c = Community.build(settings, secrets)
    server = uvicorn.Server(uvicorn.Config(create_app(c), host=host, port=port, log_level="info"))
    tasks = [server.serve(), c.worker()]
    if gateway:
        tasks.append(run_bot(c, secrets.require("DISCORD_BOT_TOKEN")))
    await asyncio.gather(*tasks)


def main(argv: Optional[List[str]] = None):
    p = argparse.ArgumentParser(prog="community-bot", description=__doc__)
    p.add_argument("--config", help="path to bot.yaml (default config/bot.yaml)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="check token, intents, roles, channels and keys (read-only)")
    sub.add_parser("invite-url", help="print the invite link with the minimal permissions")
    r = sub.add_parser("run", help="run the bot, the webhook server and the worker")
    r.add_argument("--host", default="127.0.0.1")
    r.add_argument("--port", type=int, default=8080)
    r.add_argument("--no-gateway", action="store_true", help="webhooks and dashboard only")
    rc = sub.add_parser("reconcile", help="compare Premium roles with payments (dry run unless --apply)")
    rc.add_argument("--apply", action="store_true")
    rc.add_argument("--remove-unpaid", action="store_true", help="also remove the role from people with no payment")
    sub.add_parser("report", help="counts from the local database")
    a = sub.add_parser("ask", help="ask the knowledge base from the terminal (real model call, no Discord)")
    a.add_argument("question")
    k = sub.add_parser("kb-search", help="show which sections a question finds (no model call)")
    k.add_argument("question")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings(args.config)
    secrets = Secrets()

    if args.cmd == "check":
        sys.exit(0 if asyncio.run(check(settings, secrets)) else 1)
    if args.cmd == "invite-url":
        async def _app_id():
            rest = DiscordREST(secrets.require("DISCORD_BOT_TOKEN"), base=settings.api_base)
            try:
                return (await rest.me())["id"]
            finally:
                await rest.aclose()
        print(invite_url(asyncio.run(_app_id()), settings.guild_id))
        return
    if args.cmd == "run":
        asyncio.run(run(settings, secrets, args.host, args.port, gateway=not args.no_gateway))
        return
    if args.cmd == "kb-search":
        from .kb import KnowledgeBase
        for h in KnowledgeBase.load(settings.kb_dir).search(args.question, settings.ai.max_sections):
            print(f"{h.score:6.2f}  {h.section.heading} ({h.section.source})")
        return
    from .community import Community
    from .store import Store
    if args.cmd == "ask":
        from .ai import AskService, make_provider
        from .kb import KnowledgeBase
        svc = AskService(settings, Store(settings.db_path), KnowledgeBase.load(settings.kb_dir),
                         make_provider(settings, secrets))
        res = asyncio.run(svc.ask("cli", args.question))
        print(json.dumps(res.__dict__, indent=2))
        return
    if args.cmd == "report":
        store = Store(settings.db_path)
        print(json.dumps({"links": len(store.links()), "payments": len(store.payments(100000)),
                          "open_tickets": len(store.tickets("open")), "audit_entries": len(store.audit_log(100000)),
                          "role_ops_failed": len(store.role_ops("failed")),
                          "hooks_dead": sum(1 for h in store.hooks() if h["status"] == "dead"),
                          "ai_usage": store.ai_usage()[:7]}, indent=2))
        return
    if args.cmd == "reconcile":
        c = Community.build(settings, secrets)
        plan = asyncio.run(c.membership.reconcile(apply=args.apply, remove_unpaid=args.remove_unpaid))
        print(json.dumps(plan, indent=2))


if __name__ == "__main__":
    main()
