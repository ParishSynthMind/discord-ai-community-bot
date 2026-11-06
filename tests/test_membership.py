import asyncio
import json

from community_bot import payments as P
from community_bot.membership import DAY
from tests.conftest import (
    GUILD,
    LENA,
    MAYA,
    MOD_CH,
    OMAR,
    PREMIUM,
    async_test,
    build_fake,
    checkout_completed,
    invoice_event,
    make_world,
    sub_event,
)


def norm(e):
    return P.normalize_stripe(e)


@async_test
async def test_checkout_gives_premium_role_with_audit_reason():
    w = make_world()
    r = await w.m.apply(norm(checkout_completed()))
    assert r["status"] == "applied" and r["role_change"] == "added"
    assert PREMIUM in w.roles(MAYA)
    put = [q for q in w.fake.requests if q["method"] == "PUT" and "/roles/" in q["path"]][0]
    assert "stripe checkout.session.completed" in put["reason"]
    assert w.fake.audit_log[-1]["changes"][0]["key"] == "$add"
    assert w.store.audit_log()[0]["result"] == "ok"
    assert any("Premium given to" in m for m in w.mod_messages())


@async_test
async def test_redelivered_event_does_nothing():
    w = make_world()
    e = checkout_completed()
    await w.m.apply(norm(e))
    puts = len([q for q in w.fake.requests if q["method"] == "PUT"])
    again = await w.m.apply(norm(e))
    assert again["status"] == "duplicate"
    assert len([q for q in w.fake.requests if q["method"] == "PUT"]) == puts


@async_test
async def test_subscription_deleted_removes_role_by_customer_link():
    w = make_world()
    await w.m.apply(norm(checkout_completed()))
    r = await w.m.apply(norm(sub_event("customer.subscription.deleted", status="canceled", created=w.clock() + 60)))
    assert r["role_change"] == "removed" and PREMIUM not in w.roles(MAYA)
    assert w.store.link_for_user(MAYA)["status"] == "canceled"


@async_test
async def test_older_event_cannot_undo_newer_one():
    w = make_world()
    await w.m.apply(norm(checkout_completed(created=w.clock())))
    await w.m.apply(norm(sub_event("customer.subscription.deleted", status="canceled", created=w.clock() + 100)))
    stale = await w.m.apply(norm(sub_event("customer.subscription.updated", status="active", created=w.clock() + 50)))
    assert stale["status"] == "stale" and PREMIUM not in w.roles(MAYA)


@async_test
async def test_payment_failed_keeps_role_during_grace_then_worker_removes_it():
    w = make_world()
    await w.m.apply(norm(checkout_completed()))
    r = await w.m.apply(norm(invoice_event("invoice.payment_failed", created=w.clock() + 10)))
    assert r["link_status"] == "past_due" and PREMIUM in w.roles(MAYA)
    dm = [msgs for cid, msgs in w.fake.messages.items() if cid in w.fake.dm_channels.values()]
    assert dm and "didn't go through" in dm[0][0]["content"]
    w.clock.advance(2 * DAY)
    assert (await w.m.tick())["lapsed"] == 0 and PREMIUM in w.roles(MAYA)
    w.clock.advance(1.1 * DAY)
    assert (await w.m.tick())["lapsed"] == 1 and PREMIUM not in w.roles(MAYA)
    assert w.store.link_for_user(MAYA)["status"] == "lapsed"
    assert (await w.m.tick())["lapsed"] == 0  # only once


@async_test
async def test_payment_after_failure_restores_active():
    w = make_world()
    await w.m.apply(norm(checkout_completed()))
    await w.m.apply(norm(invoice_event("invoice.payment_failed", created=w.clock() + 10)))
    r = await w.m.apply(norm(invoice_event("invoice.paid", created=w.clock() + 20)))
    assert r["link_status"] == "active" and w.store.link_for_user(MAYA)["past_due_since"] is None
    kinds = [p["kind"] for p in w.store.payments()]
    assert kinds == ["paid", "failed"]


@async_test
async def test_zero_grace_removes_on_first_failure():
    w = make_world(past_due_grace_days=0)
    await w.m.apply(norm(checkout_completed()))
    r = await w.m.apply(norm(invoice_event("invoice.payment_failed", created=w.clock() + 10)))
    assert r["role_change"] == "removed" and PREMIUM not in w.roles(MAYA)


@async_test
async def test_cancel_at_period_end_keeps_role():
    w = make_world()
    await w.m.apply(norm(checkout_completed()))
    r = await w.m.apply(norm(sub_event("customer.subscription.updated", cancel_at_period_end=True,
                                        created=w.clock() + 5)))
    assert r["link_status"] == "canceling" and PREMIUM in w.roles(MAYA)


@async_test
async def test_invoice_before_checkout_is_recorded_then_linked():
    w = make_world()
    first = await w.m.apply(norm(invoice_event("invoice.paid")))
    assert first["status"] == "recorded" and not w.mod_messages()
    r = await w.m.apply(norm(checkout_completed()))
    assert r["role_change"] == "added"
    assert len(w.store.payments()) == 1  # the checkout itself is not counted as a second payment


@async_test
async def test_activation_without_member_is_flagged_for_mods():
    w = make_world()
    r = await w.m.apply(norm(checkout_completed(uid=None)))
    assert r["status"] == "unlinked"
    assert any("has no Discord member attached" in m for m in w.mod_messages())
    assert w.store.audit_log()[0]["result"] == "needs_attention"


@async_test
async def test_member_not_in_server_gets_role_when_joining():
    w = make_world()
    r = await w.m.apply(norm(checkout_completed(uid=LENA, customer="cus_lena", sub="sub_lena")))
    assert r["role_change"] == "member_not_in_server"
    w.fake.add_member(GUILD, LENA, "lena")
    await w.community.member_joined(LENA, "lena")
    assert PREMIUM in w.roles(LENA)


@async_test
async def test_hierarchy_problem_is_reported_not_retried():
    fake = build_fake()
    next(r for r in fake.guilds[GUILD]["roles"] if r["id"] == PREMIUM)["position"] = 9  # above the bot
    w = make_world(fake=fake)
    r = await w.m.apply(norm(checkout_completed()))
    assert r["role_change"] == "failed" and not w.store.role_ops()
    assert any("must sit ABOVE that role" in m for m in w.mod_messages())


@async_test
async def test_missing_manage_roles_is_reported():
    fake = build_fake()
    bot_role = next(r for r in fake.guilds[GUILD]["roles"] if r["managed"])
    bot_role["permissions"] = str(1 << 10 | 1 << 11)
    w = make_world(fake=fake)
    assert (await w.m.apply(norm(checkout_completed())))["role_change"] == "failed"
    assert "Missing Permissions" in w.store.audit_log()[0]["result"]


@async_test
async def test_discord_outage_queues_role_change_and_worker_retries():
    w = make_world()
    w.fake.inject("PUT", r"/roles/", 503, {"message": "Service Unavailable", "code": 0}, times=5)
    r = await w.m.apply(norm(checkout_completed()))
    assert r["role_change"] == "queued_retry" and PREMIUM not in w.roles(MAYA)
    w.clock.advance(31)
    assert (await w.m.tick())["retried"] == 1 and PREMIUM in w.roles(MAYA)
    assert w.store.role_ops("done")


@async_test
async def test_rate_limit_headers_are_respected_before_a_429():
    w = make_world()
    w.fake.limits["role"] = (2, 0.2)
    w.rest.sleep = asyncio.sleep  # really wait, as in production
    for i, uid in enumerate([MAYA, OMAR]):
        await w.m.apply(norm(checkout_completed(uid=uid, customer=f"cus_{i}", sub=f"sub_{i}")))
    w.fake.add_member(GUILD, LENA, "lena")
    r = await w.m.apply(norm(checkout_completed(uid=LENA, customer="cus_l", sub="sub_l")))
    assert r["role_change"] == "added"
    # X-RateLimit-Remaining hit 0, so the client waited for the reset instead of earning a 429
    assert not any(q.get("status") == 429 for q in w.fake.requests)


@async_test
async def test_a_429_is_retried_after_retry_after():
    w = make_world()
    w.fake.inject("PUT", r"/roles/", 429, {"message": "You are being rate limited.", "retry_after": 0.05,
                                           "global": False})
    slept = []

    async def sleep(s):
        slept.append(s)
    w.rest.sleep = sleep
    r = await w.m.apply(norm(checkout_completed()))
    assert r["role_change"] == "added" and slept == [0.05]


@async_test
async def test_plan_roles_and_plan_change():
    w = make_world()
    gold = w.fake.add_role(GUILD, "Gold", 2)["id"]
    w.settings.plan_roles = {"price_gold": gold}
    await w.m.apply(norm(sub_event("customer.subscription.created", metadata={"discord_user_id": MAYA},
                                   price="price_monthly")))
    assert PREMIUM in w.roles(MAYA)
    await w.m.apply(norm(sub_event("customer.subscription.updated", price="price_gold", created=w.clock() + 5)))
    assert gold in w.roles(MAYA) and PREMIUM not in w.roles(MAYA)


@async_test
async def test_failure_in_processing_releases_event_for_redelivery():
    w = make_world()

    async def boom(*a, **k):
        raise RuntimeError("database locked")
    original = w.m._apply
    w.m._apply = boom
    e = checkout_completed()
    try:
        await w.m.apply(norm(e))
    except RuntimeError:
        pass
    w.m._apply = original
    assert (await w.m.apply(norm(e)))["status"] == "applied"


@async_test
async def test_reconcile_without_members_intent_fixes_linked_members():
    w = make_world()
    await w.m.apply(norm(checkout_completed()))
    w.fake.members[GUILD][MAYA]["roles"].remove(PREMIUM)  # someone removed it by hand
    plan = await w.m.reconcile()
    assert plan["add"] == [(MAYA, PREMIUM)] and plan["unpaid_role"] == []
    assert PREMIUM not in w.roles(MAYA)  # dry run changed nothing
    await w.m.reconcile(apply=True)
    assert PREMIUM in w.roles(MAYA)


@async_test
async def test_reconcile_with_members_intent_finds_unpaid_roles():
    w = make_world(members_intent=True)
    w.fake.members[GUILD][OMAR]["roles"].append(PREMIUM)  # role given by hand, no payment
    plan = await w.m.reconcile()
    assert plan["unpaid_role"] == [(OMAR, PREMIUM)]
    await w.m.reconcile(apply=True)
    assert PREMIUM in w.roles(OMAR)  # left alone unless asked
    await w.m.reconcile(apply=True, remove_unpaid=True)
    assert PREMIUM not in w.roles(OMAR)


@async_test
async def test_hooks_fire_for_premium_changes_with_stable_ids():
    w = make_world()
    await w.m.apply(norm(checkout_completed(event_id="evt_same")))
    await w.community.hooks.flush()
    sent = [json.loads(r.content) for r in w.hooks_received]
    assert [s["type"] for s in sent] == ["premium.granted"]
    assert sent[0]["data"]["discord_user_id"] == MAYA


@async_test
async def test_notices_never_ping():
    w = make_world()
    await w.m.apply(norm(checkout_completed()))
    for msg in w.fake.channel_messages(MOD_CH):
        assert msg["allowed_mentions"] == {"parse": []}


@async_test
async def test_renewals_and_repeat_events_post_nothing():
    w = make_world()
    await w.m.apply(norm(checkout_completed()))
    await w.m.apply(norm(sub_event("customer.subscription.created", created=w.clock() + 1)))
    await w.m.apply(norm(invoice_event("invoice.paid", created=w.clock() + 2)))
    assert len([q for q in w.fake.requests if q["method"] == "PUT" and "/roles/" in q["path"]]) == 1
    assert sum("Premium given" in m for m in w.mod_messages()) == 1


@async_test
async def test_two_memberships_role_stays_until_both_end():
    w = make_world()
    await w.m.apply(norm(checkout_completed()))
    await w.m.apply(norm(checkout_completed(customer="cus_maya2", sub="sub_maya2")))
    await w.m.apply(norm(sub_event("customer.subscription.deleted", status="canceled", created=w.clock() + 5)))
    assert PREMIUM in w.roles(MAYA)
    await w.m.apply(norm(sub_event("customer.subscription.deleted", status="canceled", customer="cus_maya2",
                                   sub="sub_maya2", created=w.clock() + 6)))
    assert PREMIUM not in w.roles(MAYA)


@async_test
async def test_sync_member_reports_only_real_changes():
    w = make_world()
    await w.m.apply(norm(checkout_completed()))
    assert await w.m.sync_member(MAYA) == "unchanged"
    w.fake.members[GUILD][MAYA]["roles"].remove(PREMIUM)
    assert await w.m.sync_member(MAYA) == "added"
    assert await w.m.sync_member(OMAR) == "no_membership"
