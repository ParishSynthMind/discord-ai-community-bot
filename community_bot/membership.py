"""Paid-membership role sync. Every payment event, from any provider, goes through
`apply`: claim the event id once (redeliveries do nothing), find the Discord member it
belongs to, update the link's status, then make the member's roles match that status.
Role changes are PUT/DELETE calls, which Discord treats as idempotent, so applying the
same state twice is harmless. Every change is written to the audit log with its cause."""
import logging
from typing import Optional

from . import payments as P
from .discord_rest import MISSING_PERMISSIONS, UNKNOWN_MEMBER, UNKNOWN_ROLE, DiscordError

log = logging.getLogger("community_bot.membership")

STATUS_FOR_KIND = {P.ACTIVATED: "active", P.CANCEL_SCHEDULED: "canceling", P.CANCELED: "canceled",
                   P.PAYMENT_FAILED: "past_due"}
DAY = 86400


class Membership:
    def __init__(self, settings, store, rest, hooks=None, notify=None, clock=None):
        self.s = settings
        self.store = store
        self.rest = rest
        self.hooks = hooks
        self.notify = notify  # async callable(text) posting to the mod queue, best effort
        self.clock = clock or store.clock

    # ------------------------------------------------------------------ decisions
    def wants_role(self, link: Optional[dict]) -> bool:
        if not link:
            return False
        status = link.get("status")
        if status in ("active", "canceling"):
            return True
        if status == "past_due":
            since = link.get("past_due_since") or self.clock()
            return self.clock() - since < self.s.past_due_grace_days * DAY
        return False

    def roles_wanted(self, user_id: str) -> set:
        """Every paid role a member should hold, across all their memberships (someone can
        pay through two providers, or hold two plans)."""
        return {self.s.role_for_plan(x.get("plan")) for x in self.store.links_for_user(user_id) if self.wants_role(x)}

    # ------------------------------------------------------------------ events
    async def apply(self, ev: P.NormalizedEvent) -> dict:
        if not self.store.claim_event(ev.provider, ev.event_id, ev.type, ev.created):
            return {"status": "duplicate", "event_id": ev.event_id}
        try:
            result = await self._apply(ev)
        except Exception as exc:  # nothing was changed in Discord: let the provider redeliver
            log.exception("event %s failed", ev.event_id)
            self.store.release_event(ev.provider, ev.event_id)
            raise exc
        self.store.finish_event(ev.provider, ev.event_id, result["status"], result)
        return result

    async def _apply(self, ev: P.NormalizedEvent) -> dict:
        if ev.kind == P.IGNORED:
            return {"status": "ignored", "type": ev.type}
        link = self.store.get_link(ev.provider, ev.customer_id) if ev.customer_id else None
        user_id = ev.discord_user_id or (link or {}).get("discord_user_id")

        if ev.kind in (P.PAYMENT_SUCCEEDED, P.PAYMENT_FAILED) and ev.amount_minor is not None:
            self.store.add_payment(ev.provider, ev.event_id, "paid" if ev.kind == P.PAYMENT_SUCCEEDED else "failed",
                                   ev.amount_minor, ev.currency, ev.customer_id, user_id)

        if not user_id:
            # A first invoice can arrive before the checkout that names the member; that is
            # normal and the checkout event links it. An activation or cancellation we can't
            # place is a real problem a person should see.
            if ev.kind in (P.ACTIVATED, P.CANCELED):
                self.store.audit("webhook", "unlinked_payment", reason=f"{ev.provider} {ev.type}",
                                 ref=ev.event_id, result="needs_attention")
                await self._tell_mods(f"Payment event `{ev.type}` from {ev.provider} has no Discord member attached "
                                      f"(customer `{ev.customer_id}`). Check the payment link includes the member id.")
                return {"status": "unlinked", "type": ev.type}
            return {"status": "recorded", "type": ev.type}

        if link and ev.created < (link.get("last_event_created") or 0):
            # Providers don't promise order. An older event must not undo a newer one.
            return {"status": "stale", "type": ev.type, "discord_user_id": user_id}

        before = self.roles_wanted(user_id)
        fields = dict(discord_user_id=user_id, email=ev.email, subscription_id=ev.subscription_id, plan=ev.plan,
                      last_event_created=ev.created)
        if ev.kind in STATUS_FOR_KIND:
            fields["status"] = STATUS_FOR_KIND[ev.kind]
        elif ev.kind == P.PAYMENT_SUCCEEDED and (link or {}).get("status") in ("past_due", "lapsed", None):
            fields["status"] = "active"
        if fields.get("status") == "past_due":
            fields["past_due_since"] = (link or {}).get("past_due_since") or self.clock()
        elif fields.get("status"):
            fields["past_due_since"] = None
        customer = ev.customer_id or f"discord:{user_id}"
        link = self.store.upsert_link(ev.provider, customer, **fields)
        after = self.roles_wanted(user_id)
        role = self.s.role_for_plan(link.get("plan"))
        cause = f"{ev.provider} {ev.type}"

        # Only a change of state touches Discord. A renewal, or a second event saying the same
        # thing (Stripe sends checkout.session.completed and customer.subscription.created for
        # one purchase), changes nothing and posts nothing. A role someone removed by hand is
        # put back by /premium or the reconcile.
        outcome = "unchanged"
        for r in sorted(before - after):
            outcome = await self._change(user_id, r, "remove", cause, ev.event_id)
        for r in sorted(after - before):
            outcome = await self._change(user_id, r, "add", cause, ev.event_id)

        if ev.kind == P.PAYMENT_FAILED and self.wants_role(link):
            days = self.s.past_due_grace_days
            await self._tell_mods(f"Payment failed for <@{user_id}> ({cause}). Premium stays on for {days} "
                                  "day(s) while the card is retried.")
            await self._dm(user_id, f"Your {self.s.community_name} Premium payment didn't go through. Premium stays "
                                    f"on for {days} day{'s' if days != 1 else ''} while the card is retried; "
                                    "updating your card keeps it on.")
        return {"status": "applied", "type": ev.type, "discord_user_id": user_id, "role": role,
                "link_status": link.get("status"), "role_change": outcome}

    # ------------------------------------------------------------------ role changes
    async def _change(self, user_id: str, role_id: str, op: str, reason: str, ref: str, actor="webhook") -> str:
        audit_reason = f"{self.s.community_name} bot: {reason}"
        try:
            if op == "add":
                await self.rest.add_role(self.s.guild_id, user_id, role_id, audit_reason)
            else:
                await self.rest.remove_role(self.s.guild_id, user_id, role_id, audit_reason)
        except DiscordError as exc:
            if exc.code == UNKNOWN_MEMBER:
                # Paid but not in the server yet (or left). The role is given when they join or run /premium.
                self.store.audit(actor, f"{op}_role", user_id, role_id, reason, ref, "member_not_in_server")
                return "member_not_in_server"
            if exc.transient:
                self.store.queue_role_op(user_id, role_id, op, reason, ref, str(exc))
                self.store.audit(actor, f"{op}_role", user_id, role_id, reason, ref, "queued_retry")
                return "queued_retry"
            hint = ""
            if exc.code == MISSING_PERMISSIONS:
                hint = (" The bot needs Manage Roles, and its own role must sit ABOVE that role in "
                        "Server Settings > Roles.")
            elif exc.code == UNKNOWN_ROLE:
                hint = " The role id in config/bot.yaml does not exist in this server."
            self.store.audit(actor, f"{op}_role", user_id, role_id, reason, ref, f"failed: {exc.message}")
            await self._tell_mods(f"Could not {op} role {role_id} for <@{user_id}>: {exc.message}.{hint}")
            return "failed"
        self.store.audit(actor, f"{op}_role", user_id, role_id, reason, ref, "ok")
        if self.hooks and role_id in self.s.all_paid_roles():
            self.hooks.emit("premium.granted" if op == "add" else "premium.removed",
                            {"discord_user_id": user_id, "role_id": role_id, "reason": reason}, ref=f"{op}:{ref}")
        if actor == "webhook":
            verb = "given to" if op == "add" else "removed from"
            await self._tell_mods(f"Premium {verb} <@{user_id}> ({reason}).")
        return "added" if op == "add" else "removed"

    async def sync_member(self, user_id: str, actor="sync") -> str:
        """Make one member's paid roles match their payments (for /premium and new joins).
        Reads the member first, so it only reports (and logs) a change it really made."""
        if not self.store.links_for_user(user_id):
            return "no_membership"
        want = self.roles_wanted(user_id)
        try:
            has = set((await self.rest.member(self.s.guild_id, user_id)).get("roles", [])) & self.s.all_paid_roles()
        except DiscordError as exc:
            if exc.code == UNKNOWN_MEMBER:
                return "member_not_in_server"
            raise
        outcome = "unchanged"
        for r in sorted(has - want):
            outcome = await self._change(user_id, r, "remove", f"{actor}: no active membership", f"sync:{user_id}", actor)
        for r in sorted(want - has):
            outcome = await self._change(user_id, r, "add", f"{actor}: membership active", f"sync:{user_id}", actor)
        return outcome

    async def tick(self) -> dict:
        """Run by the worker every half minute: retry queued role changes, end grace periods."""
        done = {"retried": 0, "lapsed": 0}
        for op in self.store.due_role_ops():
            try:
                if op["op"] == "add":
                    await self.rest.add_role(self.s.guild_id, op["discord_user_id"], op["role_id"], op["reason"])
                else:
                    await self.rest.remove_role(self.s.guild_id, op["discord_user_id"], op["role_id"], op["reason"])
            except DiscordError as exc:
                if exc.transient and op["attempts"] < 8:
                    self.store.update_role_op(op["id"], "pending", str(exc), delay=min(30 * 2 ** op["attempts"], 3600))
                else:
                    self.store.update_role_op(op["id"], "failed", str(exc))
                    self.store.audit("worker", f"{op['op']}_role", op["discord_user_id"], op["role_id"], op["reason"],
                                     op["ref"], f"failed: {exc.message}")
                continue
            self.store.update_role_op(op["id"], "done")
            self.store.audit("worker", f"{op['op']}_role", op["discord_user_id"], op["role_id"], op["reason"],
                             op["ref"], "ok (retried)")
            done["retried"] += 1
        for link in self.store.links():
            if link["status"] == "past_due" and not self.wants_role(link) and link.get("discord_user_id"):
                self.store.upsert_link(link["provider"], link["customer_id"], status="lapsed",
                                       past_due_since=link["past_due_since"])
                role = self.s.role_for_plan(link.get("plan"))
                if role not in self.roles_wanted(link["discord_user_id"]):  # unless another membership pays for it
                    await self._change(link["discord_user_id"], role, "remove",
                                       f"grace period of {self.s.past_due_grace_days} days ended",
                                       f"lapse:{link['customer_id']}")
                done["lapsed"] += 1
        return done

    async def reconcile(self, apply: bool = False, remove_unpaid: bool = False) -> dict:
        """Compare Discord with the payment records and list (or fix) the differences.
        With the members intent the whole member list is read, so people holding a paid
        role without a payment are found too; without it only linked members are checked."""
        plan = {"add": [], "remove": [], "unpaid_role": [], "checked": 0}
        paid_roles = self.s.all_paid_roles()
        wanted = {}
        for link in self.store.links():
            uid = link.get("discord_user_id")
            if uid:
                wanted[uid] = self.roles_wanted(uid)
        if self.s.members_intent:
            members = {m["user"]["id"]: m for m in await self.rest.list_members(self.s.guild_id)}
        else:
            members = {}
            for uid in wanted:
                try:
                    members[uid] = await self.rest.member(self.s.guild_id, uid)
                except DiscordError as exc:
                    if exc.code != UNKNOWN_MEMBER:
                        raise
        for uid, m in members.items():
            plan["checked"] += 1
            has = set(m.get("roles") or []) & paid_roles
            want = wanted.get(uid, set())
            for r in sorted(want - has):
                plan["add"].append((uid, r))
            for r in sorted(has - want):
                (plan["remove"] if uid in wanted else plan["unpaid_role"]).append((uid, r))
        if apply:
            for uid, r in plan["add"]:
                await self._change(uid, r, "add", "reconcile", "reconcile", actor="reconcile")
            for uid, r in plan["remove"] + (plan["unpaid_role"] if remove_unpaid else []):
                await self._change(uid, r, "remove", "reconcile", "reconcile", actor="reconcile")
        return plan

    # ------------------------------------------------------------------ messages
    async def _tell_mods(self, text: str):
        if self.notify:
            try:
                await self.notify(text)
            except Exception:  # a notice must never break role sync
                log.warning("mod-queue notice failed", exc_info=True)

    async def _dm(self, user_id: str, text: str):
        try:
            await self.rest.dm(user_id, text)
        except DiscordError as exc:  # DMs closed (50007) or not in the server: not worth failing over
            self.store.audit("webhook", "dm", user_id, None, text[:80], None, f"not delivered: {exc.message}")
