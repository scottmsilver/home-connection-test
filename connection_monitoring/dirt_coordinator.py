"""Caller configured monthly drill policy and durable controller state (Python 3.9+)."""

import hashlib
import math
from itertools import islice
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import json
import os
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Optional
from .dirt_ledger import (
    SQLiteLedger,
    RunRecord,
    CaseRecord,
    EventRecord,
    InspectionRecord,
    CaseTransition,
)
from .dirt_health import HealthRejected, require_healthy
from .dirt_report import (
    make_event,
    validate_receipt,
    validate_announcement_receipt,
    CHECKS,
    _name,
    event_id,
    REASONS,
)


def finite(value):
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except (OverflowError, ValueError):
        return False


@dataclass(frozen=True)
class Site:
    name: str
    standby: str
    primary: str


@dataclass(frozen=True)
class Policy:
    authority: str
    enabled: bool
    activation: float
    timezone: str
    weekday: int
    week: int
    hour: int
    minute: int
    sites: tuple
    required_checks: tuple
    lock_path: str
    db_path: str
    evidence_dir: str
    start_grace: float
    window: float
    case_budget: float
    health_age: float
    receipt_age: float
    manual_window: Optional[tuple] = None


def month_slot(policy, year, month):
    first = datetime(
        year, month, 1, policy.hour, policy.minute, tzinfo=ZoneInfo(policy.timezone)
    )
    slot = first + timedelta(
        days=(policy.weekday - first.weekday()) % 7 + 7 * (policy.week - 1)
    )
    if slot.month != month:
        return None
    # A nonexistent local minute is not silently normalized into a delayed start.
    epoch = slot.timestamp()
    if datetime.fromtimestamp(epoch, slot.tzinfo).replace(tzinfo=None) != slot.replace(
        tzinfo=None
    ):
        return None
    return epoch


def restored(value, now):
    expected = dict(
        ipv4="clean",
        ipv6="clean",
        marker="absent",
        timer="absent",
        service="inactive",
        wan_roles="healthy",
        internet="healthy",
    )
    return (
        type(value) is dict
        and set(value) == set(expected) | {"version", "observed_at"}
        and type(value["version"]) is int
        and value["version"] == 1
        and type(now) in (int, float)
        and finite(now)
        and now >= 0
        and type(value["observed_at"]) in (int, float)
        and finite(value["observed_at"])
        and value["observed_at"] >= 0
        and -5 <= now - value["observed_at"] <= 120
        and all(value[k] == v for k, v in expected.items())
    )


class CoordinatorRejected(HealthRejected):
    """Only protocol reason codes may reach report facts."""

    def __init__(self, reason):
        if reason not in REASONS:
            raise ValueError("invalid rejection reason")
        self.reason = reason
        super().__init__(reason)


def validate_policy(p):
    numbers = (
        p.activation,
        p.start_grace,
        p.window,
        p.case_budget,
        p.health_age,
        p.receipt_age,
    )
    if (
        not _name(p.authority)
        or type(p.enabled) is not bool
        or any(type(n) not in (int, float) or not finite(n) or n < 0 for n in numbers)
        or not 0 < p.start_grace <= 600
        or not 0 < p.window <= 10800
        or p.case_budget < 2700
        or not 0 < p.health_age <= 120
        or not 0 < p.receipt_age <= 300
        or p.case_budget > p.window
        or (
            p.manual_window is not None
            and (
                type(p.manual_window) is not tuple
                or len(p.manual_window) != 2
                or any(not finite(n) or n <= 0 for n in p.manual_window)
                or not 0 < p.manual_window[1] - p.manual_window[0] <= p.window
            )
        )
        or any(type(n) is not int for n in (p.weekday, p.week, p.hour, p.minute))
        or not 0 <= p.weekday <= 6
        or not 1 <= p.week <= 5
        or not 0 <= p.hour <= 23
        or not 0 <= p.minute <= 59
        or not p.sites
        or len(p.sites) > 16
        or len({s.name for s in p.sites}) != len(p.sites)
        or any(
            not all(_name(x) for x in (s.name, s.standby, s.primary))
            or s.standby == s.primary
            for s in p.sites
        )
        or not p.required_checks
        or len(set(p.required_checks)) != len(p.required_checks)
        or any(
            not isinstance(x, str) or not x
            for x in (p.lock_path, p.db_path, p.evidence_dir)
        )
    ):
        raise ValueError("invalid coordinator policy")
    ZoneInfo(p.timezone)


def sanitized_observations(value, depth=0, *, wan_names=(), budget=None):
    """Select finite measurements and protocol enums, never arbitrary source text."""
    if budget is None:
        budget = [128]
    budget[0] -= 1
    keys = {
        "router",
        "observation",
        "wans",
        "alert",
        "delivery_history",
        "fault_present",
        "cleanup",
        "complete",
        "internet",
        "collector_active",
        "state",
        "health",
        "status",
        "accepted",
        "ready",
        "active",
        "timer_armed",
        "timer_cancelled",
        "elapsed_seconds",
        "ipv4",
        "ipv6",
        "marker",
        "timer",
        "service",
        "wan_roles",
        "available",
        "healthy",
        "age_seconds",
        "sample_age_seconds",
        "storage_error_count",
        "usage_percent",
        "capacity_threshold",
        "journal_bytes",
        "journal_records",
        "coverage_complete",
        "service_active",
        "mount_read_write",
        "rules_expected",
        "rules_observed",
        "last_evaluation_age_seconds",
        "pending_sectors",
        "uncorrectable_sectors",
        "critical_warning",
        "overall_passed",
    }
    enums = {
        "inactive",
        "active",
        "firing",
        "resolved",
        "ok",
        "healthy",
        "unhealthy",
        "clean",
        "present",
        "unknown",
        "absent",
        "owned",
        "foreign",
        "accepted",
    }
    if depth > 3 or budget[0] < 0 or type(value) is not dict:
        return {}
    result = {}
    for key, item in islice(value.items(), 64):
        if len(result) >= 16 or budget[0] < 0:
            break
        if key not in keys:
            continue
        if key == "wans" and type(item) is dict:
            selected = {
                name: sanitized_observations(
                    item[name], depth + 1, wan_names=wan_names, budget=budget
                )
                for name in wan_names
                if name in item
            }
            if selected:
                result[key] = selected
        elif key in {"ready", "active"}:
            if type(item) is bool:
                result[key] = item
        elif type(item) is bool or item is None:
            result[key] = item
        elif type(item) in (int, float) and finite(item) and abs(item) <= 1e12:
            result[key] = item
        elif type(item) is str and item in enums:
            result[key] = item
        elif type(item) is dict:
            selected = sanitized_observations(
                item, depth + 1, wan_names=wan_names, budget=budget
            )
            if selected:
                result[key] = selected
    return result


class Coordinator:
    """Adapters: health()->v1 report; report(event)->receipt; inspect(site,wan,id).

    run_case(site,wan,id,emit,before_arm=...,before_inject=...) must preserve engine
    cleanup on callback rejection. Adapters must bound health, reports, inspections,
    and engine calls within the declared whole-case budget; deployment must enforce
    a service deadline and independent router recovery timer. emit is deliberately
    nonthrowing. Watchdog never invokes run_case. Local callers must use the SAME
    lock/database for every invocation. Explicit stores implement dirt_ledger.Ledger
    and share one configured authority. Their admission and transactions must have
    bounded deadlines; cloud authoritative storage never requires local files.
    """

    def __init__(self, policy, *, clock, health, report, run_case, inspect, store=None):
        validate_policy(policy)
        self.policy = policy
        self.clock = clock
        self.health = health
        self.report = report
        self.run_case = run_case
        self.inspect = inspect
        self.broken = False
        self.last_clock = None
        self.clock_invalid = False
        self.health_issues = []
        self.owner = None
        self.store = (
            store
            if store is not None
            else SQLiteLedger(policy.db_path, policy.lock_path)
        )
        if store is None:
            # Keep direct db() fixtures and supported local fault injection hooks.
            self.store.connection = lambda: self.db()
            self.transact(lambda tx: tx.activation(policy.activation, initialize=True))

    def db(self):
        """Legacy SQLite fixture access; explicit cloud stores expose no SQL."""
        if not isinstance(self.store, SQLiteLedger):
            raise TypeError("db() is available only for SQLite storage")
        return self.store.db()

    def lock(self):
        # Cloud lock() MUST be nonclaiming; claim_run owns admission atomically.
        return self.store.lock()

    def transact(self, callback):
        try:
            return self.store.transact(callback, owner=self.owner)
        except Exception:
            self.broken = True
            raise

    def now(self):
        try:
            value = self.clock()
        except Exception:
            self.clock_invalid = True
            raise CoordinatorRejected("state_invalid") from None
        if type(value) not in (int, float) or not finite(value) or value < 0:
            self.clock_invalid = True
            raise CoordinatorRejected("state_invalid")
        if self.last_clock is not None and value < self.last_clock:
            self.clock_invalid = True
            raise CoordinatorRejected("state_invalid")
        self.last_clock = value
        self.clock_invalid = False
        return value

    def terminal_time(self):
        """Historical evidence time only; never used by fault authorization gates."""
        try:
            return self.now()
        except HealthRejected:
            if self.last_clock is None:
                raise
            return self.last_clock

    def preflight(self, hostname):
        if hostname != self.policy.authority:
            raise CoordinatorRejected("unsupported")
        self.health_issues = []
        try:
            snapshot = deepcopy(self.health())
        except Exception:
            raise CoordinatorRejected("health_unavailable") from None
        now = self.now()
        checks = snapshot.get("checks", {}) if type(snapshot) is dict else {}
        if type(checks) is not dict:
            checks = {}
        issues = []
        reason = "health_failed"
        envelope = snapshot.get("observed_at") if type(snapshot) is dict else None
        if type(envelope) not in (int, float) or not finite(envelope):
            reason = "observations_unavailable"
        elif not -5 <= now - envelope <= self.policy.health_age:
            reason = "health_stale"
        for key in self.policy.required_checks:
            if not _name(key):
                continue
            item = checks.get(key)
            item = item if type(item) is dict else {}
            details = item.get("details", {})
            details = details if type(details) is dict else {}
            observed = item.get("observed_at")
            valid_time = (
                type(observed) in (int, float)
                and finite(observed)
                and 0 <= observed <= 1e12
            )
            stale = valid_time and not -5 <= now - observed <= self.policy.health_age
            available = (
                details.get("available")
                if type(details.get("available")) is bool
                else bool(item)
            )
            healthy = (
                details.get("healthy")
                if "healthy" in details
                and (
                    details.get("healthy") is None
                    or type(details.get("healthy")) is bool
                )
                else item.get("ok") if type(item.get("ok")) is bool else None
            )
            malformed_metadata = (
                "available" in details and type(details["available"]) is not bool
            ) or (
                "healthy" in details
                and details["healthy"] is not None
                and type(details["healthy"]) is not bool
            )
            if malformed_metadata:
                available = False
                healthy = None
            if not item:
                available = False
                healthy = None
            if (
                item.get("ok") is True
                and available
                and healthy is True
                and valid_time
                and not stale
            ):
                continue
            if malformed_metadata:
                reason = "state_invalid"
            elif not available:
                reason = "health_unavailable"
            elif stale:
                reason = "health_stale"
            elif item.get("ok") is True and healthy is not True:
                reason = "health_failed"
            elif not valid_time or healthy is None:
                reason = "observations_unavailable"
            issues.append(
                dict(
                    check_id=key,
                    available=available,
                    healthy=healthy,
                    observed_at=observed if valid_time else now,
                    observations=sanitized_observations(details, depth=1),
                )
            )
        # Keep only relevant negative evidence, bounded below the report facts cap.
        for issue in issues[:32]:
            if len(json.dumps(issue["observations"])) > 1000:
                issue["observations"] = {}
            candidate = self.health_issues + [issue]
            if len(json.dumps(candidate, separators=(",", ":"))) > 5000:
                continue
            self.health_issues = candidate
        try:
            require_healthy(
                snapshot, self.policy.required_checks, now, self.policy.health_age
            )
        except HealthRejected:
            raise CoordinatorRejected(reason) from None
        # Legacy v1 evidence without these optional fields remains authoritative.
        # Present collector metadata is an additional positive authorization gate.
        for item in checks.values():
            details = item["details"]
            if ("available" in details and type(details["available"]) is not bool) or (
                "healthy" in details
                and details["healthy"] is not None
                and type(details["healthy"]) is not bool
            ):
                raise CoordinatorRejected("state_invalid")
            if "available" in details and details["available"] is not True:
                raise CoordinatorRejected("health_unavailable")
            if "healthy" in details and details["healthy"] is not True:
                raise CoordinatorRejected("health_failed")
        return snapshot

    def event(
        self,
        run,
        phase,
        outcome,
        *,
        case=None,
        site=None,
        facts=None,
        revision=1,
        transition=None,
        inspection=None,
    ):
        event = make_event(
            run_id=run,
            case_id=case,
            site=site,
            phase=phase,
            outcome=outcome,
            observed_at=(
                self.now()
                if phase in ("starting", "case_starting")
                else self.terminal_time()
            ),
            facts=facts
            or {
                "reason_codes": [
                    (
                        "checks_passed"
                        if outcome == "passed"
                        else (
                            "interrupted"
                            if outcome == "interrupted"
                            else (
                                "recovery_unverified"
                                if outcome == "recovery_unverified"
                                else "preflight_failed"
                            )
                        )
                    )
                ]
            },
            revision=revision,
        )
        record = EventRecord(event["event_id"], event)
        if transition is None:
            self.transact(lambda tx: tx.create_event(record))
        else:
            self.transact(
                lambda tx: tx.transition_case_event(transition, record, inspection)
            )
        return event, self.dispatch(event)

    def dispatch(self, event):
        try:
            record = self.transact(lambda tx: tx.get_event(event["event_id"]))
            if record and record.receipt:
                return record.receipt
            receipt = self.report(deepcopy(event))
            if not validate_receipt(receipt, event):
                return None
            self.transact(lambda tx: tx.set_receipt(event["event_id"], receipt))
            return receipt
        except Exception:
            return None

    def scheduled(self, hostname):
        return self._start(hostname)

    def manual(self, hostname, selection=None):
        return self._start(hostname, manual=True, selection=selection)

    def _start(self, hostname, manual=False, selection=None):
        p = self.policy
        if hostname != p.authority:
            return "unsupported"
        if not p.enabled:
            return "disabled"
        declared = [(s.name, w) for s in p.sites for w in (s.standby, s.primary)]
        selected = declared if selection is None else list(selection)
        if (
            not selected
            or any(x not in declared for x in selected)
            or len(set(selected)) != len(selected)
        ):
            return "unsupported"
        selected = [x for x in declared if x in selected]
        with self.lock() as acquired:
            if not acquired:
                return "busy"
            now = self.now()
            if manual and p.manual_window is not None:
                opening, closing = p.manual_window
                if now < opening:
                    return "not_due"
                if now >= closing or now - opening > p.start_grace:
                    return "outside_window"
            elif now < p.activation:
                return "not_due"
            local = datetime.fromtimestamp(now, ZoneInfo(p.timezone))
            slot = now if manual else month_slot(p, local.year, local.month)
            if not manual and (slot is None or slot < p.activation or now < slot):
                return "not_due"
            if not manual and now - slot > p.start_grace:
                return "outside_window"
            rid = uuid.uuid4().hex
            if manual and p.manual_window is not None:
                # A bounded manual intent is one-shot across fresh processes;
                # it never occupies or rewrites a monthly schedule slot.
                identity = [
                    "manual-window-v1",
                    p.authority,
                    float(p.activation),
                    [float(n) for n in p.manual_window],
                    selected,
                ]
                rid = hashlib.sha256(
                    json.dumps(identity, separators=(",", ":")).encode()
                ).hexdigest()[:32]
            admission = self.store.claim_run(
                p.activation, RunRecord(rid, None if manual else slot, "running")
            )
            if admission.status != "claimed":
                return admission.status
            self.owner = admission.owner
            outcome = "skipped"
            summaries = []
            reason = "preflight_failed"
            try:
                self.preflight(hostname)
                start, receipt = self.event(
                    rid,
                    "starting",
                    "pending",
                    facts={
                        "reason_codes": ["manual" if manual else "scheduled"],
                        "schedule": {"due_at": slot},
                        "selection": list(
                            dict.fromkeys(site for site, wan in selected)
                        ),
                        "cases": [
                            dict(
                                name="case-%d" % (index + 1),
                                site=site,
                                connection=wan,
                                outcome="pending",
                            )
                            for index, (site, wan) in enumerate(selected)
                        ],
                    },
                )
                if not self.fresh(start, receipt):
                    raise CoordinatorRejected("announcement_failed")
                outcome = "passed"
                for site, wan in selected:
                    case_out, summary = self.case(
                        rid,
                        site,
                        wan,
                        slot,
                        start,
                        receipt,
                        hostname,
                        window_end=(
                            p.manual_window[1] if manual and p.manual_window else None
                        ),
                    )
                    summaries.append(summary)
                    if self.broken and case_out == "passed":
                        outcome = "failed"
                        reason = "state_invalid"
                        break
                    if case_out != "passed":
                        outcome = case_out
                        reason = summary["reason_codes"][0]
                        break
            except HealthRejected as exc:
                reason = getattr(exc, "reason", "preflight_failed")
                outcome = "skipped"
            except KeyboardInterrupt:
                reason = "interrupted"
                outcome = "interrupted"
            except Exception:
                reason = "state_invalid"
                outcome = "failed"
            self.transact(lambda tx: tx.finish_run(rid, outcome))
            final_facts = {
                "reason_codes": ["checks_passed" if outcome == "passed" else reason],
                "elapsed_seconds": max(0, self.terminal_time() - now),
            }
            if summaries:
                final_facts["cases"] = summaries
            if self.health_issues:
                final_facts["health_issues"] = self.health_issues
            self.event(
                rid,
                "skipped" if outcome == "skipped" else "finished",
                outcome,
                facts=final_facts,
            )
            if not self.broken:
                self.store.release_if_resolved(self.owner)
            return outcome

    def fresh(self, event, receipt):
        return (
            validate_announcement_receipt(receipt, event, now=self.now())
            and -5 <= self.now() - receipt["accepted_at"] <= self.policy.receipt_age
        )

    def case(
        self, rid, site, wan, slot, start, start_receipt, hostname, *, window_end=None
    ):
        p = self.policy
        deadline = slot + p.window
        if window_end is not None:
            deadline = min(deadline, window_end)
        cid = uuid.uuid4().hex
        router_id = uuid.uuid4().hex
        checks = {}
        arm_possible = False
        rejected = False
        rejection_reason = None
        frozen = None
        origin = self.now()

        def budget():
            now = self.now()
            if now < origin or deadline - now < p.case_budget:
                raise CoordinatorRejected("outside_window")

        budget()
        self.preflight(hostname)
        announcement, receipt = self.event(
            rid,
            "case_starting",
            "pending",
            case=cid,
            site=site,
            facts={"connection": wan},
        )
        if not validate_receipt(start_receipt, start) or not self.fresh(
            announcement, receipt
        ):
            raise CoordinatorRejected("announcement_failed")
        intent = CaseRecord(cid, rid, site, wan, router_id, "running", False)
        self.transact(lambda tx: tx.create_case(intent))

        interrupted = False
        buffered = []
        arm_gap = False

        def flush():
            # Nonthrowing, including when invoked from the engine cleanup path.
            while buffered:
                record = buffered.pop(0)
                try:
                    self.persist_stage(cid, record)
                except Exception:
                    self.broken = True

        def emit(stage, data):
            nonlocal checks, interrupted, arm_gap
            # The router is already armed. Establish the I/O barrier before even
            # timestamp collection can fail and cause the engine to emit error.
            if self.store.authoritative and stage == "armed":
                arm_gap = True
            if (
                stage == "error"
                and type(data) is dict
                and data.get("kind") == "KeyboardInterrupt"
            ):
                interrupted = True
            if (
                stage == "result"
                and type(data) is dict
                and type(data.get("checks")) is dict
            ):
                checks = {
                    k: v
                    for k, v in data["checks"].items()
                    if k in CHECKS and type(v) is bool
                }
            try:
                # Raw router and collector facts stay out of public reports. Bounded
                # evidence records carry stage and explicit check verdicts only.
                record = {
                    "stage": (
                        stage
                        if stage
                        in {
                            "baseline",
                            "armed",
                            "injected",
                            "fault_observation",
                            "restored",
                            "recovery_observation",
                            "error",
                            "cleanup",
                            "result",
                        }
                        else "unknown"
                    ),
                    "observed_at": self.now(),
                    "checks": checks.copy() if stage == "result" else {},
                    "run_id": rid,
                    "case_id": cid,
                    "site": site,
                    "wan": wan,
                    "router_run_id": router_id,
                    "interrupted": interrupted,
                    "error_kind": "KeyboardInterrupt" if interrupted else None,
                    "observations": sanitized_observations(
                        data,
                        wan_names=next(
                            (s.standby, s.primary) for s in p.sites if s.name == site
                        ),
                    ),
                }
                if len(json.dumps(record, separators=(",", ":"))) > 8192:
                    record["observations"] = {}
                if self.store.authoritative:
                    # Engine emits these only after inject/restore has returned;
                    # error is deliberately buffered until cleanup or termination.
                    if stage in {"injected", "restored", "cleanup", "result"}:
                        arm_gap = False
                    if len(buffered) >= 256:
                        raise ValueError("buffered stage evidence limit exceeded")
                    buffered.append(record)
                    if not arm_gap:
                        flush()
                else:
                    self.persist_stage(cid, record)
            except Exception:
                self.broken = True

        def guard():
            nonlocal frozen, arm_possible, rejected, rejection_reason
            try:
                if self.broken:
                    raise CoordinatorRejected("state_invalid")
                budget()
                frozen = self.preflight(hostname)
                if not validate_receipt(start_receipt, start) or not self.fresh(
                    announcement, receipt
                ):
                    raise CoordinatorRejected("receipt_expired")
                budget()
            except Exception as exc:
                rejected = True
                rejection_reason = getattr(exc, "reason", "preflight_failed")
                raise CoordinatorRejected(rejection_reason) from None
            arm_possible = True

        def inject_guard():
            nonlocal rejection_reason
            try:
                if self.broken or frozen is None:
                    raise CoordinatorRejected("state_invalid")
                budget()
                try:
                    require_healthy(frozen, p.required_checks, self.now(), p.health_age)
                except HealthRejected:
                    raise CoordinatorRejected("health_stale") from None
                if not validate_receipt(start_receipt, start) or not self.fresh(
                    announcement, receipt
                ):
                    raise CoordinatorRejected("receipt_expired")
            except HealthRejected as exc:
                rejection_reason = getattr(exc, "reason", "preflight_failed")
                raise

        try:
            passed = self.run_case(
                site, wan, router_id, emit, before_arm=guard, before_inject=inject_guard
            )
        except KeyboardInterrupt:
            passed = False
            interrupted = True
        except Exception:
            passed = False
        finally:
            flush()
        try:
            clean, proof = self.inspection(site, wan, router_id)
        except Exception:
            clean = False
            proof = {"valid": False, "status": "unknown"}
        outcome = (
            "interrupted"
            if interrupted
            else (
                "skipped"
                if rejected and not arm_possible
                else (
                    "passed"
                    if passed is True
                    and checks
                    and all(
                        checks.get(k) is True
                        for k in ("fault", "alert", "recovery", "cleanup")
                    )
                    and not self.broken
                    else "failed"
                )
            )
        )
        if not clean and not interrupted:
            outcome = "recovery_unverified"

        transition = CaseTransition(intent, outcome, clean)
        inspection = InspectionRecord(uuid.uuid4().hex, cid, proof)

        facts = {
            "connection": wan,
            "reason_codes": [
                rejection_reason
                or (
                    "state_invalid"
                    if self.broken
                    else (
                        "checks_passed"
                        if outcome == "passed"
                        else (
                            "interrupted"
                            if outcome == "interrupted"
                            else (
                                "recovery_unverified"
                                if outcome == "recovery_unverified"
                                else "fault_failed"
                            )
                        )
                    )
                )
            ],
            "elapsed_seconds": max(0, self.terminal_time() - origin),
            "checks": checks or {"fault": None},
            "restoration": {"restoration": clean},
        }
        if not clean and interrupted:
            facts["reason_codes"] = ["interrupted", "recovery_unverified"]
        if self.clock_invalid:
            facts["reason_codes"] = ["state_invalid"] + (
                ["interrupted"] if interrupted else []
            )
        if rejection_reason and self.health_issues:
            facts["health_issues"] = self.health_issues
        self.event(
            rid,
            "case_finished",
            outcome,
            case=cid,
            site=site,
            facts=facts,
            transition=transition,
            inspection=inspection,
        )
        return outcome, dict(
            name=cid,
            site=site,
            connection=wan,
            outcome=outcome,
            checks=facts["checks"],
            restoration=facts["restoration"],
            reason_codes=facts["reason_codes"],
            elapsed_seconds=facts["elapsed_seconds"],
        )

    def inspection(self, site, wan, router_id):
        allowed = {
            "ipv4": {"clean", "present", "unknown"},
            "ipv6": {"clean", "present", "unknown"},
            "marker": {"absent", "owned", "foreign", "unknown"},
            "timer": {"absent", "active", "unknown"},
            "service": {"inactive", "active", "unknown"},
            "wan_roles": {"healthy", "unhealthy", "unknown"},
            "internet": {"healthy", "unhealthy", "unknown"},
        }
        try:
            value = self.inspect(site, wan, router_id)
        except Exception:
            value = None
        now = self.now()
        clean = restored(value, now)
        proof = {"attempted_at": now, "valid": False}
        if (
            type(value) is dict
            and set(value) == set(allowed) | {"version", "observed_at"}
            and type(value["version"]) is int
            and value["version"] == 1
            and type(value["observed_at"]) in (int, float)
            and finite(value["observed_at"])
            and value["observed_at"] >= 0
            and all(
                type(value[k]) is str and value[k] in statuses
                for k, statuses in allowed.items()
            )
        ):
            proof.update(snapshot=deepcopy(value), valid=True)
        else:
            proof["status"] = "unknown"
        return clean, proof

    def persist_stage(self, cid, record):
        seq = self.transact(lambda tx: tx.append_stage(cid, record))
        if self.store.authoritative:
            return
        path = Path(self.policy.evidence_dir) / cid
        path.mkdir(parents=True, exist_ok=True)
        parent_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
        with open(path / ("%06d.jsonl" % seq), "x") as out:
            out.write(json.dumps(record, sort_keys=True) + "\n")
            out.flush()
            os.fsync(out.fileno())
        fd = os.open(str(path), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def watchdog(self, hostname):
        if hostname != self.policy.authority:
            return "unsupported"
        with self.lock() as acquired:
            if not acquired:
                return "busy"
            admission = self.store.claim_inspection(self.policy.activation)
            if admission.status != "claimed":
                return admission.status
            self.owner = admission.owner
            cases = self.transact(lambda tx: tx.cases(unresolved=True))
            for case in cases:
                cid, rid, site, wan, router_id, outcome = (
                    case.id,
                    case.run,
                    case.site,
                    case.wan,
                    case.router_id,
                    case.outcome,
                )
                known = any(
                    s.name == site and wan in (s.standby, s.primary)
                    for s in self.policy.sites
                )
                try:
                    clean, proof = (
                        self.inspection(site, wan, router_id)
                        if known
                        else (False, {"valid": False, "status": "unknown"})
                    )
                except Exception:
                    clean = False
                    proof = {"valid": False, "status": "unknown"}
                if outcome == "running":
                    outcome = "interrupted"
                    self.event(
                        rid,
                        "interrupted",
                        outcome,
                        case=cid,
                        site=site,
                        transition=CaseTransition(case, outcome, False),
                    )
                    case = CaseRecord(cid, rid, site, wan, router_id, outcome, False)
                inspection = InspectionRecord(uuid.uuid4().hex, cid, proof)
                if clean:
                    self.event(
                        rid,
                        "recovery_update",
                        "interrupted" if outcome == "interrupted" else "failed",
                        case=cid,
                        site=site,
                        facts={"restoration": {"restoration": True}},
                        transition=CaseTransition(case, outcome, True),
                        inspection=inspection,
                    )
                else:
                    self.transact(lambda tx: tx.append_inspection(inspection))
            running = self.transact(lambda tx: tx.runs(running=True))
            for run in running:
                self.transact(lambda tx: tx.finish_run(run.id, "interrupted"))
            terminal_cases = self.transact(lambda tx: tx.cases(terminal=True))
            for case in terminal_cases:
                cid, rid, site, wan, outcome, clean = (
                    case.id,
                    case.run,
                    case.site,
                    case.wan,
                    case.outcome,
                    case.restored,
                )
                existing, rows = self.transact(
                    lambda tx: (
                        tx.get_event(event_id(rid, cid, "case_finished", 1)),
                        tx.stages(cid),
                    )
                )
                if not existing:
                    checks = {}
                    for stage in rows:
                        if stage["stage"] == "result":
                            checks = stage["checks"]
                    self.event(
                        rid,
                        "case_finished",
                        outcome,
                        case=cid,
                        site=site,
                        facts={
                            "connection": wan,
                            "checks": checks or {"fault": None},
                            "restoration": {"restoration": bool(clean)},
                        },
                    )
            # Reconcile terminal intent gaps, including loss before case intent.
            allruns = self.transact(lambda tx: tx.runs(terminal=True))
            for run in allruns:
                phase = "skipped" if run.outcome == "skipped" else "finished"
                existing = self.transact(
                    lambda tx: tx.get_event(event_id(run.id, None, phase, 1))
                )
                if not existing:
                    self.event(run.id, phase, run.outcome)
            self.missed()
            events = self.transact(lambda tx: tx.pending_events())
            for record in events:
                if record.payload["phase"] not in ("starting", "case_starting"):
                    self.dispatch(record.payload)
            if not self.broken:
                self.store.release_if_resolved(self.owner)
            return "inspected"

    def missed(self):
        p = self.policy
        now = self.now()
        start = datetime.fromtimestamp(p.activation, ZoneInfo(p.timezone))
        end = datetime.fromtimestamp(now, ZoneInfo(p.timezone))
        year, month = start.year, start.month
        while (year, month) <= (end.year, end.month):
            slot = month_slot(p, year, month)
            if slot is not None and p.activation <= slot and slot + p.start_grace < now:
                rid = uuid.uuid4().hex

                def record_missed(tx):
                    if tx.find_run(slot):
                        return False
                    tx.create_run(RunRecord(rid, slot, "skipped"))
                    return True

                if self.transact(record_missed):
                    self.event(
                        rid,
                        "skipped",
                        "skipped",
                        facts={
                            "reason_codes": ["outside_window"],
                            "schedule": {"due_at": slot},
                        },
                    )
            year, month = (year + 1, 1) if month == 12 else (year, month + 1)
