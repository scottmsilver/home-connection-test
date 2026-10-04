"""Provider-independent ledger contracts; no cloud SDK or SQL-emulating fake."""

import importlib
from copy import deepcopy
from contextlib import nullcontext
from dataclasses import replace

import pytest


def ledger_module():
    try:
        return importlib.import_module("connection_monitoring.dirt_ledger")
    except ModuleNotFoundError:
        pytest.fail("typed transactional DIRT ledger is missing")


@pytest.fixture
def ledger(tmp_path):
    return lambda: ledger_module().SQLiteLedger(
        str(tmp_path / "state"), str(tmp_path / "lock")
    )


def test_transaction_rollback_including_activation_and_run(ledger):
    ledger = ledger()
    m = ledger_module()

    def fail(tx):
        tx.activation(100, initialize=True)
        tx.create_run(m.RunRecord("run", 200, "running"))
        raise RuntimeError("abort")

    with pytest.raises(RuntimeError, match="abort"):
        ledger.transact(fail)
    assert ledger.transact(lambda tx: tx.get_run("run")) is None
    ledger.transact(lambda tx: tx.activation(101, initialize=True))


def test_atomic_claim_checks_activation_unresolved_and_slot(ledger):
    ledger = ledger()
    m = ledger_module()
    run = m.RunRecord("run", 200, "running")
    assert ledger.claim_run(100, run).status == "claimed"
    with pytest.raises(ValueError, match="activation"):
        ledger.claim_run(101, replace(run, id="other", slot=201))
    assert ledger.transact(lambda tx: tx.get_run("other")) is None
    assert (
        ledger.claim_run(100, replace(run, id="duplicate")).status
        == "already_completed"
    )
    assert ledger.transact(lambda tx: tx.get_run("duplicate")) is None
    ledger.transact(
        lambda tx: tx.create_case(
            m.CaseRecord("case", "run", "island", "backup", "router", "running", False)
        )
    )
    assert (
        ledger.claim_run(100, replace(run, id="next", slot=201)).status
        == "recovery_unverified"
    )
    assert ledger.transact(lambda tx: tx.get_run("next")) is None


def test_case_transition_event_and_inspection_commit_together(ledger):
    ledger = ledger()
    m = ledger_module()
    case = m.CaseRecord("case", "run", "island", "backup", "router", "running", False)
    ledger.transact(lambda tx: tx.create_case(case))
    event = m.EventRecord("event", {"phase": "case_finished"})
    transition = m.CaseTransition(case, "failed", True)
    proof = m.InspectionRecord("inspection", "case", {"valid": True})

    def fail(tx):
        tx.transition_case_event(transition, event, proof)
        raise RuntimeError("abort")

    with pytest.raises(RuntimeError):
        ledger.transact(fail)
    assert ledger.transact(lambda tx: tx.get_case("case")) == case
    assert ledger.transact(lambda tx: tx.get_event("event")) is None
    ledger.transact(lambda tx: tx.transition_case_event(transition, event, proof))
    assert ledger.transact(lambda tx: tx.get_case("case")).outcome == "failed"
    assert ledger.transact(lambda tx: tx.get_event("event")) == event
    assert ledger.transact(lambda tx: tx.inspections("case")) == (proof,)
    with pytest.raises(ValueError, match="state"):
        ledger.transact(lambda tx: tx.update_case(transition))


def test_terminal_outcomes_identity_events_and_receipts_are_immutable(ledger):
    ledger = ledger()
    m = ledger_module()
    run = m.RunRecord("run", 200, "failed")
    case = m.CaseRecord("case", "run", "island", "backup", "router", "failed", False)
    event = m.EventRecord("event", {"outcome": "failed"})
    ledger.transact(
        lambda tx: (tx.create_run(run), tx.create_case(case), tx.create_event(event))
    )
    with pytest.raises(ValueError, match="immutable"):
        ledger.transact(lambda tx: tx.finish_run("run", "passed"))
    with pytest.raises(ValueError, match="immutable"):
        ledger.transact(
            lambda tx: tx.update_case(m.CaseTransition(case, "passed", True))
        )
    with pytest.raises(ValueError, match="immutable"):
        ledger.transact(
            lambda tx: tx.create_event(replace(event, payload={"outcome": "passed"}))
        )
    ledger.transact(lambda tx: tx.create_event(event))  # deterministic retry
    ledger.transact(lambda tx: tx.set_receipt("event", {"accepted": 1}))
    with pytest.raises(ValueError, match="immutable"):
        ledger.transact(lambda tx: tx.set_receipt("event", {"accepted": 2}))
    assert ledger.transact(lambda tx: tx.pending_events()) == ()
    ledger.transact(lambda tx: tx.update_case(m.CaseTransition(case, "failed", True)))
    assert ledger.transact(lambda tx: tx.get_case("case")).outcome == "failed"


def test_stages_are_ordered_bounded_and_survive_fresh_store(ledger):
    ledger = ledger()
    m = ledger_module()
    assert ledger.transact(lambda tx: tx.append_stage("case", {"stage": "armed"})) == 0
    with pytest.raises(ValueError, match="limit"):
        ledger.transact(lambda tx: tx.append_stage("case", {"raw": "x" * 8192}))
    for i in range(1, 256):
        ledger.transact(lambda tx: tx.append_stage("case", {"sequence": i}))
    with pytest.raises(ValueError, match="limit"):
        ledger.transact(lambda tx: tx.append_stage("case", {}))
    fresh = m.SQLiteLedger(ledger.db_path, ledger.lock_path)
    stages = fresh.transact(lambda tx: tx.stages("case"))
    assert (
        len(stages) == 256
        and stages[0] == {"stage": "armed"}
        and stages[-1] == {"sequence": 255}
    )
    with fresh.db() as db:
        assert db.execute("PRAGMA synchronous").fetchone()[0] == 2


def test_local_flock_and_existing_schema_preserved(ledger):
    ledger = ledger()
    m = ledger_module()
    second = m.SQLiteLedger(ledger.db_path, ledger.lock_path)
    with ledger.lock() as first:
        with second.lock() as acquired:
            assert first and not acquired
    with ledger.db() as db:
        assert {row[1] for row in db.execute("PRAGMA table_info(cases)")} == {
            "id",
            "run",
            "site",
            "wan",
            "router_id",
            "outcome",
            "restored",
        }


class MemoryTransaction:
    """Typed document operations on a transactional snapshot, deliberately no SQL."""

    def __init__(self, state):
        self.state = state

    def activation(self, expected, *, initialize=False):
        if self.state["activation"] is None and initialize:
            self.state["activation"] = expected
        if self.state["activation"] != expected:
            raise ValueError("activation differs from durable state")

    def get_run(self, run_id):
        return self.state["runs"].get(run_id)

    def find_run(self, slot):
        return next((r for r in self.runs() if r.slot == slot), None)

    def runs(self, *, terminal=False, running=False):
        return tuple(
            r
            for r in self.state["runs"].values()
            if (not terminal or r.outcome != "running")
            and (not running or r.outcome == "running")
        )

    def create_run(self, run):
        if run.slot is not None and self.find_run(run.slot) not in (None, run):
            raise ValueError("slot already claimed")
        self._create("runs", run)

    def finish_run(self, run_id, outcome):
        prior = self.get_run(run_id)
        if prior.outcome not in ("running", outcome):
            raise ValueError("terminal outcome immutable")
        self.state["runs"][run_id] = replace(prior, outcome=outcome)

    def get_case(self, case_id):
        return self.state["cases"].get(case_id)

    def cases(self, *, unresolved=False, terminal=False):
        return tuple(
            c
            for c in self.state["cases"].values()
            if (not unresolved or not c.restored)
            and (not terminal or c.outcome != "running")
        )

    def create_case(self, case):
        self._create("cases", case)

    def update_case(self, transition):
        prior = self.get_case(transition.expected.id)
        if prior != transition.expected:
            raise ValueError("case state changed")
        if prior.outcome not in ("running", transition.outcome) or (
            prior.restored and not transition.restored
        ):
            raise ValueError("terminal outcome immutable")
        self.state["cases"][prior.id] = replace(
            prior, outcome=transition.outcome, restored=transition.restored
        )

    def append_stage(self, case_id, payload):
        m = ledger_module()
        m.encoded(payload, m.MAX_STAGE_BYTES)
        stages = self.state["stages"].setdefault(case_id, [])
        if len(stages) >= m.MAX_STAGES:
            raise ValueError("stage evidence limit exceeded")
        stages.append(deepcopy(payload))
        return len(stages) - 1

    def stages(self, case_id):
        return tuple(self.state["stages"].get(case_id, ()))

    def get_event(self, event_id):
        return self.state["events"].get(event_id)

    def create_event(self, event):
        existing = self.get_event(event.id)
        if existing is not None:
            if existing.payload != event.payload:
                raise ValueError("event immutable")
            return
        self._create("events", event)

    def pending_events(self):
        return tuple(e for e in self.state["events"].values() if e.receipt is None)

    def set_receipt(self, event_id, receipt):
        event = self.get_event(event_id)
        if event.receipt not in (None, receipt):
            raise ValueError("receipt immutable")
        self.state["events"][event_id] = replace(event, receipt=deepcopy(receipt))

    def append_inspection(self, inspection):
        self._create("inspections", inspection)

    def inspections(self, case_id):
        return tuple(
            i for i in self.state["inspections"].values() if i.case_id == case_id
        )

    def transition_case_event(self, transition, event, inspection=None):
        self.update_case(transition)
        if inspection is not None:
            self.append_inspection(inspection)
        self.create_event(event)

    def _create(self, collection, record):
        prior = self.state[collection].get(record.id)
        if prior is not None and prior != record:
            raise ValueError("record immutable")
        self.state[collection][record.id] = deepcopy(record)


class MemoryLedger:
    """Firestore-like fencing/atomic commits with injected ambiguity and retry."""

    authoritative = True

    def __init__(self):
        self.state = dict(
            activation=None,
            owner=None,
            runs={},
            cases={},
            events={},
            inspections={},
            stages={},
        )
        self.calls = []
        self.fail = None
        self.retry = False
        self.terminal_proof = False
        self.on_call = lambda: None
        self.generation = 0

    def lock(self):
        return nullcontext(True)

    def _transaction(self, callback):
        self.on_call()
        self.calls.append("transaction")
        if self.retry:
            callback(MemoryTransaction(deepcopy(self.state)))
        snapshot = deepcopy(self.state)
        value = callback(MemoryTransaction(snapshot))
        if self.fail is not None:
            self.fail(snapshot)
        self.state = snapshot
        return deepcopy(value)

    def transact(self, callback, *, owner=None):
        def fenced(tx):
            if owner is None or owner != tx.state["owner"]:
                raise ValueError("owner fence rejected")
            return callback(tx)

        return self._transaction(fenced)

    def _owner(self, activation, run_id, kind):
        self.generation += 1
        return ledger_module().Owner(
            "invented-authority",
            activation,
            "revision-a",
            run_id,
            "executions/invented",
            "owner-%d" % self.generation,
            self.generation,
            kind,
        )

    def claim_run(self, activation, run):
        m = ledger_module()
        owner = self._owner(activation, run.id, "runner")

        def claim(tx):
            if tx.state["activation"] is not None:
                tx.activation(activation)
            if tx.state["owner"] is not None:
                return m.Admission("busy")
            if tx.cases(unresolved=True):
                return m.Admission("recovery_unverified")
            if run.slot is not None and tx.find_run(run.slot):
                return m.Admission("already_completed")
            tx.activation(activation, initialize=True)
            tx.state["owner"] = owner
            tx.create_run(run)
            return m.Admission("claimed", owner)

        return self._transaction(claim)

    def claim_inspection(self, activation):
        m = ledger_module()
        observed = deepcopy(self.state["owner"])
        if observed is not None and not self.terminal_proof:
            return m.Admission("busy")
        owner = self._owner(
            activation, observed.run_id if observed else "inspection", "inspector"
        )

        def claim(tx):
            if tx.state["owner"] != observed:
                return m.Admission("busy")
            tx.activation(activation, initialize=True)
            tx.state["owner"] = owner
            return m.Admission("claimed", owner)

        return self._transaction(claim)

    def release_if_resolved(self, owner):
        def release(tx):
            if not tx.cases(unresolved=True) and not tx.runs(running=True):
                tx.state["owner"] = None

        self.transact(release, owner=owner)


def test_document_admission_is_all_or_nothing_and_fences_old_owner():
    m = ledger_module()
    ledger = MemoryLedger()
    run = m.RunRecord("run", 200, "running")
    ledger.fail = lambda state: (_ for _ in ()).throw(OSError("commit failed"))
    with pytest.raises(OSError):
        ledger.claim_run(100, run)
    assert (
        ledger.state["activation"] is None
        and ledger.state["owner"] is None
        and not ledger.state["runs"]
    )
    ledger.fail = None
    owner = ledger.claim_run(100, run).owner
    assert ledger.claim_run(100, replace(run, id="second")).status == "busy"
    assert ledger.claim_inspection(100).status == "busy"
    ledger.terminal_proof = True
    inspector = ledger.claim_inspection(100).owner
    assert inspector != owner
    with pytest.raises(ValueError, match="fence"):
        ledger.transact(lambda tx: tx.finish_run("run", "passed"), owner=owner)
    assert ledger.state["runs"]["run"].outcome == "running"
