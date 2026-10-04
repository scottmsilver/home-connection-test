import importlib
from datetime import datetime, timezone
from copy import deepcopy
import pytest


def module():
    try:
        return importlib.import_module("connection_monitoring.dirt_coordinator")
    except ModuleNotFoundError:
        pytest.fail("durable coordinator is missing")


def test_monthly_calendar_explicit_timezone():
    m = module()
    p = m.Policy(
        authority="controller",
        enabled=True,
        activation=0,
        timezone="UTC",
        weekday=0,
        week=1,
        hour=3,
        minute=4,
        sites=(m.Site("north", "backup", "main"),),
        required_checks=("north.internet", "south.internet"),
        lock_path="lock",
        db_path="state",
        evidence_dir="evidence",
        start_grace=600,
        window=10800,
        case_budget=2700,
        health_age=120,
        receipt_age=300,
    )
    assert (
        m.month_slot(p, 2026, 10)
        == datetime(2026, 10, 5, 3, 4, tzinfo=timezone.utc).timestamp()
    )


def test_inspection_requires_exact_fresh_positive_schema():
    m = module()
    value = dict(
        version=1,
        observed_at=1000,
        ipv4="clean",
        ipv6="clean",
        marker="absent",
        timer="absent",
        service="inactive",
        wan_roles="healthy",
        internet="healthy",
    )
    assert m.restored(value, 1000)
    for key, bad in [
        ("version", True),
        ("marker", "foreign"),
        ("observed_at", 879),
        ("internet", "unknown"),
    ]:
        assert not m.restored(dict(value, **{key: bad}), 1000)
    assert not m.restored(dict(value, extra=True), 1000)


@pytest.fixture
def rig(tmp_path):
    m = module()
    now = [datetime(2026, 10, 5, 3, 4, tzinfo=timezone.utc).timestamp()]
    calls = []
    sent = []
    p = m.Policy(
        "controller",
        True,
        now[0] - 1,
        "UTC",
        0,
        1,
        3,
        4,
        (m.Site("north", "backup", "main"), m.Site("south", "backup", "main")),
        ("north.internet", "south.internet"),
        str(tmp_path / "lock"),
        str(tmp_path / "state"),
        str(tmp_path / "evidence"),
        600,
        10800,
        2700,
        120,
        300,
    )

    def health():
        return dict(
            version=1,
            observed_at=now[0],
            checks={
                k: dict(ok=True, observed_at=now[0], details={})
                for k in p.required_checks
            },
        )

    def report(e):
        from connection_monitoring.dirt_report import payload_digest

        sent.append(e)
        return dict(
            version=1,
            event_id=e["event_id"],
            case_id=e["case_id"],
            payload_sha256=payload_digest(e),
            message_id=len(sent),
            accepted_at=now[0],
        )

    def inspect(site, wan, rid):
        return dict(
            version=1,
            observed_at=now[0],
            ipv4="clean",
            ipv6="clean",
            marker="absent",
            timer="absent",
            service="inactive",
            wan_roles="healthy",
            internet="healthy",
        )

    def run(site, wan, rid, emit, before_arm, before_inject):
        before_arm()
        before_inject()
        calls.append((site, wan))
        emit(
            "result",
            dict(
                passed=True,
                checks=dict(fault=True, alert=True, recovery=True, cleanup=True),
            ),
        )
        return True

    c = m.Coordinator(
        p,
        clock=lambda: now[0],
        health=health,
        report=report,
        run_case=run,
        inspect=inspect,
    )
    return c, now, calls, sent


def test_order_dedup_and_positive_report_intents(rig):
    c, now, calls, sent = rig
    assert c.scheduled("controller") == "passed"
    assert calls == [
        ("north", "backup"),
        ("north", "main"),
        ("south", "backup"),
        ("south", "main"),
    ]
    assert c.scheduled("controller") == "already_completed"
    assert len(calls) == 4
    assert sent[0]["phase"] == "starting" and sent[-1]["phase"] == "finished"


@pytest.mark.parametrize(
    "host,delay,result",
    [("other", 0, "unsupported"), ("controller", 601, "outside_window")],
)
def test_host_and_late_start_fail_closed(rig, host, delay, result):
    c, now, calls, sent = rig
    now[0] += delay
    assert c.scheduled(host) == result
    assert not calls and not sent


def test_unverified_restoration_blocks_next_month(rig):
    c, now, calls, sent = rig
    c.inspect = lambda *args: {"version": True}
    assert c.scheduled("controller") == "recovery_unverified"
    assert len(calls) == 1
    now[0] = datetime(2026, 11, 2, 3, 4, tzinfo=timezone.utc).timestamp()
    assert c.scheduled("controller") == "recovery_unverified"
    assert len(calls) == 1


def test_announcement_failure_never_faults_and_watchdog_retries_terminal(rig):
    c, now, calls, sent = rig
    original = c.report
    c.report = lambda e: None
    assert c.scheduled("controller") == "skipped"
    assert not calls
    c.report = original
    c.watchdog("controller")
    assert any(e["phase"] == "skipped" for e in sent)


def test_watchdog_orphan_only_inspects_and_keeps_interrupted(rig):
    c, now, calls, sent = rig

    def die(*args, **kwargs):
        raise SystemExit("simulated hard kill")

    c.run_case = die
    with pytest.raises(SystemExit):
        c.scheduled("controller")
    c.watchdog("controller")
    assert not calls
    assert any(e["outcome"] == "interrupted" for e in sent)


def test_failing_case_stops_suite_and_preserves_checks(rig):
    c, now, calls, sent = rig

    def fail(site, wan, rid, emit, **kwargs):
        calls.append((site, wan))
        emit(
            "result",
            dict(
                passed=False,
                checks=dict(fault=True, alert=False, recovery=True, cleanup=True),
            ),
        )
        return False

    c.run_case = fail
    assert c.scheduled("controller") == "failed"
    assert len(calls) == 1
    event = next(e for e in sent if e["phase"] == "case_finished")
    assert event["facts"]["checks"]["alert"] is False


def test_complete_health_gate_and_local_frozen_snapshot(rig):
    c, now, calls, sent = rig
    c.health = lambda: dict(version=1, observed_at=now[0], checks={})
    assert c.scheduled("controller") == "skipped"
    assert not calls and sent[0]["phase"] == "skipped"


def test_emit_fsync_failure_never_escapes_cleanup_and_stops_next_case(rig, monkeypatch):
    c, now, calls, sent = rig
    import os

    monkeypatch.setattr(os, "fsync", lambda fd: (_ for _ in ()).throw(OSError("disk")))
    cleaned = []

    def engine(site, wan, rid, emit, **kw):
        kw["before_arm"]()
        calls.append((site, wan))
        try:
            emit("armed", {})
            kw["before_inject"]()
        except module().HealthRejected:
            pass
        finally:
            cleaned.append(True)
            emit("cleanup", {"complete": True})
            emit("result", {"checks": {"cleanup": True}})
        return False

    c.run_case = engine
    assert c.scheduled("controller") == "failed"
    assert cleaned == [True] and len(calls) == 1


def test_replayed_receipt_never_refreshes_timestamp(rig):
    c, now, calls, sent = rig
    event, receipt = c.event("a" * 32, "starting", "pending")
    old = receipt["accepted_at"]
    now[0] += 301
    assert c.dispatch(event)["accepted_at"] == old
    assert not c.fresh(event, receipt)


def test_overlap_uses_same_lock_for_watchdog_manual_and_scheduled(rig):
    c, now, calls, sent = rig
    with c.lock() as acquired:
        assert acquired
        assert c.manual("controller") == "busy"
        assert c.scheduled("controller") == "busy"
        assert c.watchdog("controller") == "busy"
    assert not calls


def test_missed_months_are_watchdog_only_never_faults(rig):
    c, now, calls, sent = rig
    now[0] = datetime(2027, 1, 4, 4, 0, tzinfo=timezone.utc).timestamp()
    assert c.scheduled("controller") == "outside_window"
    assert not sent
    c.watchdog("controller")
    assert len([e for e in sent if e["phase"] == "skipped"]) == 4
    assert not calls


def test_stale_source_timestamp_rejected_even_with_fresh_envelope(rig):
    c, now, calls, sent = rig
    original = c.health

    def stale():
        value = original()
        value["checks"]["south.internet"]["observed_at"] -= 121
        return value

    c.health = stale
    assert c.scheduled("controller") == "skipped"
    assert not calls


def test_local_inject_gate_rejects_snapshot_that_ages_during_arm(rig):
    c, now, calls, sent = rig
    restores = []

    def engine(site, wan, rid, emit, **kw):
        kw["before_arm"]()
        calls.append((site, wan))
        now[0] += 121
        try:
            kw["before_inject"]()
            pytest.fail("stale injection authorized")
        except module().HealthRejected:
            pass
        finally:
            restores.append(True)
        return False

    c.run_case = engine
    assert c.scheduled("controller") == "failed"
    assert restores == [True] and len(calls) == 1


def test_terminal_delivery_retried_without_repeating_cases(rig):
    c, now, calls, sent = rig
    original = c.report
    c.report = lambda e: (
        None if e["phase"] in ("case_finished", "finished") else original(e)
    )
    assert c.scheduled("controller") == "passed"
    count = len(calls)
    now[0] += 99999
    c.report = original
    c.watchdog("controller")
    assert len(calls) == count
    assert sent[-1]["phase"] == "finished"


def test_activation_must_match_durable_epoch(rig):
    c, now, calls, sent = rig
    from dataclasses import replace

    with pytest.raises(ValueError, match="activation"):
        module().Coordinator(
            replace(c.policy, activation=c.policy.activation + 1),
            clock=c.clock,
            health=c.health,
            report=c.report,
            run_case=c.run_case,
            inspect=c.inspect,
        )


def test_manual_explicit_case_outside_monthly_slot(rig):
    c, now, calls, sent = rig
    now[0] += 86400
    assert c.manual("controller", selection=[("south", "main")]) == "passed"
    assert calls == [("south", "main")]
    assert sent[0]["facts"]["reason_codes"] == ["manual"]


def test_readonly_preflight_available_before_enable(rig):
    c, now, calls, sent = rig
    from dataclasses import replace

    c.policy = replace(c.policy, enabled=False)
    assert c.preflight("controller")["version"] == 1
    assert c.manual("controller") == "disabled"


def test_short_case_budget_is_invalid(rig):
    c, now, calls, sent = rig
    from dataclasses import replace

    with pytest.raises(ValueError):
        module().validate_policy(replace(c.policy, case_budget=1))


def test_second_case_uses_fresh_case_receipt_not_expired_initial_receipt(rig):
    c, now, calls, sent = rig
    original = c.run_case

    def slow(*args, **kw):
        result = original(*args, **kw)
        now[0] += 601
        return result

    c.run_case = slow
    assert c.scheduled("controller") == "passed"
    assert len(calls) == 4
    assert all(
        "checks" in item and "restoration" in item
        for item in sent[-1]["facts"]["cases"]
    )


def test_watchdog_finalizes_orphan_without_case_intent(rig):
    c, now, calls, sent = rig
    with c.db() as db:
        db.execute("INSERT INTO runs VALUES (?,?,?)", ("b" * 32, now[0], "running"))
    c.watchdog("controller")
    with c.db() as db:
        assert db.execute("SELECT outcome FROM runs").fetchone()[0] == "interrupted"
    assert any(e["phase"] == "finished" and e["outcome"] == "interrupted" for e in sent)


def test_backward_clock_after_arm_prevents_injection(rig):
    c, now, calls, sent = rig
    restores = []

    def engine(site, wan, rid, emit, **kw):
        kw["before_arm"]()
        now[0] -= 1
        try:
            kw["before_inject"]()
            pytest.fail("clock reversal authorized injection")
        except module().HealthRejected:
            pass
        finally:
            restores.append(True)
        now[0] += 1
        return False

    c.run_case = engine
    assert c.scheduled("controller") == "failed"
    assert restores == [True]


def test_bounded_stage_evidence_has_identity_and_sanitized_observations(rig):
    c, now, calls, sent = rig

    def engine(site, wan, rid, emit, **kw):
        kw["before_arm"]()
        kw["before_inject"]()
        emit(
            "fault_observation",
            {
                "router": {"fault_present": True, "secret": "private"},
                "observation": {
                    "internet": True,
                    "alert": {"state": "firing", "health": "ok"},
                    "log": "private",
                },
            },
        )
        emit(
            "result",
            {"checks": dict(fault=True, alert=True, recovery=True, cleanup=True)},
        )
        return True

    c.run_case = engine
    assert c.scheduled("controller") == "passed"
    import json
    from pathlib import Path

    records = [
        json.loads(p.read_text()) for p in Path(c.policy.evidence_dir).glob("*/*.jsonl")
    ]
    observed = next(
        r
        for r in records
        if r["stage"] == "fault_observation"
        and r["site"] == "north"
        and r["wan"] == "backup"
    )
    assert (
        observed["site"] == "north"
        and observed["wan"] == "backup"
        and observed["router_run_id"]
    )
    assert observed["observations"]["router"]["fault_present"] is True
    assert "private" not in json.dumps(records)


def test_watchdog_reconstructs_case_terminal_intent_after_commit_gap(rig):
    c, now, calls, sent = rig
    assert c.scheduled("controller") == "passed"
    with c.db() as db:
        db.execute(
            "DELETE FROM events WHERE json_extract(payload,'$.phase')='case_finished'"
        )
    sent.clear()
    c.watchdog("controller")
    assert len([e for e in sent if e["phase"] == "case_finished"]) == 4


def test_clock_rollback_from_later_observation_is_rejected(rig):
    c, now, calls, sent = rig
    c.now()
    now[0] += 20
    c.now()
    now[0] -= 1
    with pytest.raises(module().HealthRejected):
        c.now()


def test_nonexistent_dst_slot_is_not_normalized(rig):
    from dataclasses import replace

    c, now, calls, sent = rig
    p = replace(
        c.policy, timezone="America/New_York", weekday=6, week=2, hour=2, minute=30
    )
    assert module().month_slot(p, 2026, 3) is None


def test_ledger_emit_failure_does_not_prevent_engine_cleanup(rig):
    c, now, calls, sent = rig
    original = c.db
    cleaned = []

    def engine(site, wan, rid, emit, **kw):
        kw["before_arm"]()
        c.db = lambda: (_ for _ in ()).throw(OSError("ledger unavailable"))
        try:
            emit("injected", {})
        finally:
            cleaned.append(True)
            emit("cleanup", {"complete": True})
            c.db = original
        return False

    c.run_case = engine
    assert c.scheduled("controller") == "failed"
    assert cleaned == [True]


def test_explicit_failed_inspection_every_case_including_last(rig):
    c, now, calls, sent = rig
    original = c.inspect
    inspections = []

    def inspect(*args):
        inspections.append(args)
        v = original(*args)
        if len(inspections) == 4:
            v["marker"] = "foreign"
        return v

    c.inspect = inspect
    assert c.scheduled("controller") == "recovery_unverified"
    assert len(inspections) == 4


def test_keyboard_interrupt_stops_suite_and_inspects(rig):
    c, now, calls, sent = rig

    def engine(*args, **kwargs):
        raise KeyboardInterrupt()

    c.run_case = engine
    assert c.scheduled("controller") == "interrupted"
    assert any(
        e["phase"] == "case_finished" and e["outcome"] == "interrupted" for e in sent
    )


def test_unhealthy_report_preserves_only_safe_status_and_observations(rig):
    c, now, calls, sent = rig
    original = c.health

    def unhealthy():
        value = original()
        value["checks"]["north.internet"]["ok"] = False
        value["checks"]["north.internet"]["details"] = {
            "internet": False,
            "secret": "private",
        }
        return value

    c.health = unhealthy
    assert c.scheduled("controller") == "skipped"
    issue = next(
        x
        for x in sent[0]["facts"]["health_issues"]
        if x["check_id"] == "north.internet"
    )
    assert issue["healthy"] is False and issue["observations"] == {"internet": False}


def test_starting_report_declares_ordered_selected_cases(rig):
    c, now, calls, sent = rig
    assert c.scheduled("controller") == "passed"
    assert sent[0]["facts"]["selection"] == ["north", "south"]
    assert [(x["site"], x["connection"]) for x in sent[0]["facts"]["cases"]] == [
        ("north", "backup"),
        ("north", "main"),
        ("south", "backup"),
        ("south", "main"),
    ]


def test_engine_caught_keyboard_interrupt_retains_interrupted_verdict(rig):
    c, now, calls, sent = rig

    def engine(site, wan, rid, emit, **kw):
        kw["before_arm"]()
        emit("error", {"kind": "KeyboardInterrupt", "reason": "private"})
        emit("result", {"checks": {"cleanup": True}})
        return False

    c.run_case = engine
    assert c.scheduled("controller") == "interrupted"


def test_health_timeout_is_skipped_with_safe_reason(rig):
    c, now, calls, sent = rig
    c.health = lambda: (_ for _ in ()).throw(TimeoutError("private timeout"))
    assert c.scheduled("controller") == "skipped"
    assert sent[-1]["facts"]["reason_codes"] == ["health_unavailable"]


def test_failure_after_thirty_two_healthy_checks_is_reported(rig):
    c, now, calls, sent = rig
    from dataclasses import replace

    required = tuple("check-%d" % i for i in range(33))
    c.policy = replace(c.policy, required_checks=required)
    c.health = lambda: dict(
        version=1,
        observed_at=now[0],
        checks={
            k: dict(
                ok=i != 32,
                observed_at=now[0],
                details={"available": True, "healthy": i != 32},
            )
            for i, k in enumerate(required)
        },
    )
    assert c.scheduled("controller") == "skipped"
    issues = sent[-1]["facts"]["health_issues"]
    assert any(x["check_id"] == "check-32" and x["healthy"] is False for x in issues)


def test_missing_health_and_collector_unknown_are_truthful(rig):
    c, now, calls, sent = rig
    c.health = lambda: dict(
        version=1,
        observed_at=now[0],
        checks={
            "north.internet": dict(
                ok=False,
                observed_at=now[0],
                details={"available": False, "healthy": None},
            )
        },
    )
    assert c.scheduled("controller") == "skipped"
    issues = sent[-1]["facts"]["health_issues"]
    assert all(x["available"] is False and x["healthy"] is None for x in issues)
    assert {x["check_id"] for x in issues} == {"north.internet", "south.internet"}


def test_before_arm_rejection_retains_reason_and_health_issue(rig):
    c, now, calls, sent = rig
    original = c.health
    count = [0]

    def health():
        count[0] += 1
        v = original()
        if count[0] >= 3:
            v["checks"]["north.internet"]["observed_at"] -= 121
        return v

    c.health = health
    assert c.scheduled("controller") == "skipped"
    case = next(e for e in sent if e["phase"] == "case_finished")
    assert case["facts"]["reason_codes"] == ["health_stale"]
    assert case["facts"]["health_issues"]
    assert sent[-1]["facts"]["cases"][0]["reason_codes"] == ["health_stale"]


def test_sanitized_metrics_and_declared_wans_only(rig):
    c, now, calls, sent = rig
    selected = module().sanitized_observations(
        {
            "wans": {
                "backup": {"ready": True, "active": False, "uuid": "private"},
                "other": {"ready": True},
            },
            "usage_percent": 84,
            "sample_age_seconds": 12,
            "arbitrary": 42,
        },
        wan_names=("backup", "main"),
    )
    assert selected == {
        "wans": {"backup": {"ready": True, "active": False}},
        "usage_percent": 84,
        "sample_age_seconds": 12,
    }


def test_watchdog_recovery_transition_and_intent_are_atomic(rig, monkeypatch):
    c, now, calls, sent = rig

    def die(*args, **kwargs):
        raise SystemExit()

    c.run_case = die
    with pytest.raises(SystemExit):
        c.scheduled("controller")
    import sqlite3

    original = c.db

    class BrokenDB:
        def __init__(self):
            self.context = original()

        def __enter__(self):
            self.db = self.context.__enter__()
            return self

        def __exit__(self, *args):
            return self.context.__exit__(*args)

        def execute(self, sql, args=()):
            if (
                "INSERT INTO events" in sql
                and len(args) > 1
                and "recovery_update" in args[1]
            ):
                raise SystemExit("crash during intent")
            return self.db.execute(sql, args)

    c.db = BrokenDB
    with pytest.raises(SystemExit):
        c.watchdog("controller")
    c.db = original
    with c.db() as db:
        assert db.execute("SELECT restored FROM cases").fetchone()[0] == 0
    c.watchdog("controller")
    updates = [e for e in sent if e["phase"] == "recovery_update"]
    assert len(updates) == 1
    original_time = updates[0]["observed_at"]
    now[0] += 600
    c.watchdog("controller")
    with c.db() as db:
        import json

        recorded = [json.loads(x[0]) for x in db.execute("SELECT payload FROM events")]
    assert (
        next(e for e in recorded if e["phase"] == "recovery_update")["observed_at"]
        == original_time
    )


def test_inject_rejection_has_specific_stale_health_reason(rig):
    c, now, calls, sent = rig

    def engine(site, wan, rid, emit, **kw):
        kw["before_arm"]()
        now[0] += 301
        try:
            kw["before_inject"]()
        except module().HealthRejected:
            pass
        return False

    c.run_case = engine
    assert c.scheduled("controller") == "failed"
    case = next(e for e in sent if e["phase"] == "case_finished")
    assert case["facts"]["reason_codes"] == ["health_stale"]


def test_exact_inspection_proof_is_durable_for_every_case(rig):
    c, now, calls, sent = rig
    assert c.scheduled("controller") == "passed"
    with c.db() as db:
        import json

        proofs = [
            json.loads(row[0]) for row in db.execute("SELECT payload FROM inspections")
        ]
    assert len(proofs) == 4
    assert all(
        p["valid"]
        and p["snapshot"]["observed_at"] == now[0]
        and p["snapshot"]["marker"] == "absent"
        for p in proofs
    )


def test_numeric_wan_readiness_is_not_sanitized_as_valid_proof():
    assert module().sanitized_observations(
        {"wans": {"backup": {"ready": 1, "active": 0}}}, wan_names=("backup",)
    ) == {"wans": {"backup": {}}}


@pytest.mark.parametrize(
    "metadata,reason",
    [
        ({"available": False, "healthy": None}, "health_unavailable"),
        ({"available": True, "healthy": False}, "health_failed"),
        ({"available": 1, "healthy": True}, "state_invalid"),
        ({"available": True, "healthy": None}, "health_failed"),
    ],
)
def test_positive_ok_cannot_authorize_contradictory_metadata(rig, metadata, reason):
    c, now, calls, sent = rig
    original = c.health

    def health():
        value = original()
        value["checks"]["north.internet"]["details"] = metadata
        return value

    c.health = health
    assert c.scheduled("controller") == "skipped"
    assert not calls
    assert sent[-1]["facts"]["reason_codes"] == [reason]


def test_health_validation_uses_clock_after_collection(rig):
    c, now, calls, sent = rig
    original = c.health

    def health():
        now[0] += 6
        return original()

    c.health = health
    assert c.scheduled("controller") == "passed"
    assert len(calls) == 4


def test_next_case_health_timeout_retains_run_reason_and_prior_case(rig):
    c, now, calls, sent = rig
    original = c.health
    count = [0]

    def health():
        count[0] += 1
        if count[0] >= 4:
            raise TimeoutError("private")
        return original()

    c.health = health
    assert c.scheduled("controller") == "skipped"
    assert calls == [("north", "backup")]
    assert sent[-1]["facts"]["reason_codes"] == ["health_unavailable"]
    assert sent[-1]["facts"]["cases"][0]["outcome"] == "passed"


def test_clock_rollback_after_mutation_cannot_be_skipped(rig):
    c, now, calls, sent = rig

    def engine(site, wan, rid, emit, **kw):
        kw["before_arm"]()
        kw["before_inject"]()
        calls.append((site, wan))
        now[0] -= 1
        emit(
            "result",
            {"checks": dict(fault=True, alert=True, recovery=True, cleanup=True)},
        )
        return True

    c.run_case = engine
    assert c.scheduled("controller") == "recovery_unverified"
    with c.db() as db:
        assert db.execute("SELECT outcome FROM runs").fetchone()[0] != "skipped"
        outcome, clean = db.execute("SELECT outcome,restored FROM cases").fetchone()
    assert outcome == "recovery_unverified" and clean == 0
    assert sent[-1]["facts"]["reason_codes"] == ["state_invalid"]


def test_interrupted_case_keeps_intrinsic_verdict_until_recovery(rig):
    c, now, calls, sent = rig
    original = c.inspect

    def engine(site, wan, rid, emit, **kw):
        kw["before_arm"]()
        emit("error", {"kind": "KeyboardInterrupt"})
        return False

    c.run_case = engine
    c.inspect = lambda *args: None
    assert c.scheduled("controller") == "interrupted"
    c.inspect = original
    c.watchdog("controller")
    assert (
        next(e for e in sent if e["phase"] == "recovery_update")["outcome"]
        == "interrupted"
    )
    import json
    from pathlib import Path

    records = [
        json.loads(p.read_text()) for p in Path(c.policy.evidence_dir).glob("*/*.jsonl")
    ]
    assert any(r.get("interrupted") is True for r in records)


def test_database_context_releases_connection(rig):
    c, now, calls, sent = rig
    import sqlite3

    with c.db() as db:
        db.execute("SELECT 1")
    with pytest.raises(sqlite3.ProgrammingError):
        db.execute("SELECT 1")


@pytest.mark.parametrize("field", ["envelope", "check", "details"])
def test_huge_health_integer_is_safe_prearm_rejection(rig, field):
    c, now, calls, sent = rig
    original = c.health

    def health():
        v = original()
        if field == "envelope":
            v["observed_at"] = 10**1000
        elif field == "check":
            v["checks"]["north.internet"]["observed_at"] = 10**1000
        else:
            v["checks"]["north.internet"]["details"] = {"storage_error_count": 10**1000}
        return v

    c.health = health
    assert c.scheduled("controller") == "skipped"
    assert not calls


def test_database_descriptors_stable_with_garbage_collection_disabled(rig):
    c, now, calls, sent = rig
    import gc
    from pathlib import Path

    before = len(list(Path("/proc/self/fd").iterdir()))
    enabled = gc.isenabled()
    gc.disable()
    try:
        for _ in range(200):
            with c.db() as db:
                db.execute("SELECT 1")
        assert len(list(Path("/proc/self/fd").iterdir())) <= before + 1
    finally:
        if enabled:
            gc.enable()


def test_huge_numbers_are_safe_in_all_coordinator_boundaries(rig):
    c, now, calls, sent = rig
    from dataclasses import replace

    with pytest.raises(ValueError):
        module().validate_policy(replace(c.policy, health_age=10**1000))
    assert module().sanitized_observations({"usage_percent": 10**1000}) == {}
    assert not module().restored(
        {
            "version": 1,
            "observed_at": 10**1000,
            "ipv4": "clean",
            "ipv6": "clean",
            "marker": "absent",
            "timer": "absent",
            "service": "inactive",
            "wan_roles": "healthy",
            "internet": "healthy",
        },
        now[0],
    )
    c.clock = lambda: 10**1000
    with pytest.raises(module().CoordinatorRejected):
        c.now()


def test_clock_adapter_failure_after_mutation_terminalizes_safely(rig):
    c, now, calls, sent = rig

    def engine(site, wan, rid, emit, **kw):
        kw["before_arm"]()
        kw["before_inject"]()
        c.clock = lambda: (_ for _ in ()).throw(RuntimeError("private clock failure"))
        emit(
            "result",
            {"checks": dict(fault=True, alert=True, recovery=True, cleanup=True)},
        )
        return True

    c.run_case = engine
    assert c.scheduled("controller") == "recovery_unverified"
    assert sent[-1]["facts"]["reason_codes"] == ["state_invalid"]


def cloud_coordinator(rig, store=None):
    from tests.test_dirt_ledger import MemoryLedger
    from dataclasses import replace

    c, now, calls, sent = rig
    store = store or MemoryLedger()
    # Existing non-directory makes every accidental local evidence/db write fail.
    policy = replace(
        c.policy,
        db_path=c.policy.db_path + "/unused",
        lock_path=c.policy.db_path + "/lock",
        evidence_dir=c.policy.db_path + "/evidence",
    )
    fresh = module().Coordinator(
        policy,
        clock=c.clock,
        health=c.health,
        report=c.report,
        run_case=c.run_case,
        inspect=c.inspect,
        store=store,
    )
    return fresh, store


def test_explicit_store_is_lazy_and_fresh_coordinator_uses_durable_slot(rig):
    c, store = cloud_coordinator(rig)
    assert not store.calls and store.state["activation"] is None
    store.retry = True
    assert c.scheduled("controller") == "passed"
    assert len(rig[2]) == 4  # callback retries never repeat external work
    assert store.state["owner"] is None
    other, _ = cloud_coordinator(rig, store)
    assert other.scheduled("controller") == "already_completed"
    assert len(rig[2]) == 4
    assert len(store.state["inspections"]) == 4


@pytest.mark.parametrize(
    "selection,delay,result",
    [([], 0, "unsupported"), (None, -2, "not_due"), (None, 601, "outside_window")],
)
def test_cloud_invalid_admission_never_claims_or_initializes(
    rig, selection, delay, result
):
    c, store = cloud_coordinator(rig)
    rig[1][0] += delay
    assert c._start("controller", selection=selection) == result
    assert not store.calls and store.state["owner"] is None and not store.state["runs"]


def test_cloud_commit_failure_before_case_never_enters_engine(rig):
    c, store = cloud_coordinator(rig)

    def fail(state):
        if state["cases"]:
            raise OSError("case intent commit failed")

    store.fail = fail
    assert c.scheduled("controller") == "failed"
    assert not rig[2] and not store.state["cases"]


def test_cloud_claim_failure_leaves_no_activation_owner_slot_or_run(rig):
    c, store = cloud_coordinator(rig)
    store.fail = lambda state: (_ for _ in ()).throw(OSError("commit failed"))
    with pytest.raises(OSError):
        c.scheduled("controller")
    assert (
        store.state["activation"] is None
        and store.state["owner"] is None
        and not store.state["runs"]
    )
    assert not rig[2]


def test_cloud_interrupted_intent_needs_terminal_execution_proof(rig):
    c, store = cloud_coordinator(rig)
    c.run_case = lambda *args, **kwargs: (_ for _ in ()).throw(SystemExit("hard kill"))
    with pytest.raises(SystemExit):
        c.scheduled("controller")
    assert store.state["owner"] is not None
    inspector, _ = cloud_coordinator(rig, store)
    assert inspector.watchdog("controller") == "busy"
    assert not rig[2]
    store.terminal_proof = True
    assert inspector.watchdog("controller") == "inspected"
    assert store.state["owner"] is None
    assert all(
        c.outcome == "interrupted" and c.restored for c in store.state["cases"].values()
    )
    assert inspector.scheduled("controller") == "already_completed"


@pytest.mark.parametrize("failure", [None, "armed_write", "guard"])
def test_actual_engine_has_no_cloud_calls_between_arm_and_inject(
    rig, monkeypatch, failure
):
    from connection_monitoring import dirt
    import time

    c, store = cloud_coordinator(rig)
    gap = [False]
    phase = ["baseline"]
    actions = []
    normal = {
        "main": {"ready": True, "active": True},
        "backup": {"ready": True, "active": False},
    }

    def call():
        assert not gap[0], "remote ledger I/O inside verified arm-to-inject gap"

    store.on_call = call
    if failure == "armed_write":

        def fail(state):
            if any(
                stage["stage"] == "armed"
                for stages in state["stages"].values()
                for stage in stages
            ):
                raise OSError("late stage write failed")

        store.fail = fail

    def ssh(site, role, source, args=()):
        if role == "monitor":
            return dict(
                internet=True,
                collector_active=True,
                alert=dict(
                    state="firing" if phase[0] == "fault" else "inactive", health="ok"
                ),
                delivery_history=dict(
                    status="firing" if phase[0] == "fault" else "resolved",
                    accepted=time.time() + 10,
                ),
            )
        action = args[0]
        actions.append(action)
        if action == "arm":
            assert store.state["cases"], "potential-arm intent must precede arm"
            gap[0] = True
            if failure == "guard":
                rig[1][0] += 301
        if action in ("inject", "restore"):
            gap[0] = False
        if action == "inject":
            phase[0] = "fault"
        if action == "restore":
            phase[0] = "recovery"
        wans = deepcopy(normal)
        if phase[0] == "fault":
            wans["backup"]["ready"] = False
        return dict(fault_present=phase[0] == "fault", wans=wans, cleanup=True)

    monkeypatch.setattr(dirt, "SITES", {"north": {"primary": "main"}})
    monkeypatch.setattr(dirt, "router_source", lambda site: "fixture")
    monkeypatch.setattr(dirt, "ssh", ssh)
    c.run_case = lambda *args, **kwargs: dirt.run(*args, **kwargs, poll=0)
    result = c.manual("controller", [("north", "backup")])
    assert result == ("passed" if failure is None else "failed")
    assert actions.count("arm") == 1 and "restore" in actions
    if failure == "guard":
        assert "inject" not in actions
    else:
        assert actions.index("arm") < actions.index("inject") < actions.index("restore")
    if failure == "armed_write":
        assert c.broken
    else:
        stages = next(iter(store.state["stages"].values()))
        assert "armed" in [s["stage"] for s in stages]


def test_cloud_late_receipt_write_failure_stops_before_next_case_intent(rig):
    c, store = cloud_coordinator(rig)
    failed = []

    def fail(state):
        if not failed and any(
            e.payload["phase"] == "case_finished" and e.receipt is not None
            for e in state["events"].values()
        ):
            failed.append(True)
            raise OSError("terminal receipt commit failed")

    store.fail = fail
    assert c.scheduled("controller") == "failed"
    assert len(store.state["cases"]) == 1
    assert rig[2] == [("north", "backup")]
    assert c.broken and store.state["owner"] is not None
    # The successfully checked case result stays immutable; only the run fails.
    assert next(iter(store.state["cases"].values())).outcome == "passed"


def test_cloud_activation_mismatch_never_writes_or_faults(rig):
    from dataclasses import replace

    c, store = cloud_coordinator(rig)
    assert c.scheduled("controller") == "passed"
    state = deepcopy(store.state)
    c.policy = replace(c.policy, activation=c.policy.activation + 1)
    with pytest.raises(ValueError, match="activation"):
        c.manual("controller")
    assert store.state == state
    assert len(rig[2]) == 4


def test_cloud_ambiguous_case_intent_commit_never_replays_engine(rig):
    c, store = cloud_coordinator(rig)
    original = store.transact
    ambiguous = []

    def transact(callback, *, owner=None):
        value = original(callback, owner=owner)
        if store.state["cases"] and not ambiguous:
            ambiguous.append(True)
            raise OSError("committed but acknowledgment lost")
        return value

    store.transact = transact
    assert c.scheduled("controller") == "failed"
    assert not rig[2] and len(store.state["cases"]) == 1
    fresh, _ = cloud_coordinator(rig, store)
    assert fresh.scheduled("controller") == "busy"
    assert fresh.watchdog("controller") == "busy"
    store.terminal_proof = True
    assert fresh.watchdog("controller") == "inspected"
    assert fresh.scheduled("controller") == "already_completed"
    assert not rig[2]
