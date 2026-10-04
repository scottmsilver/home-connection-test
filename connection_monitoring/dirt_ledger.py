"""Transactional DIRT records and the supported local SQLite implementation.

Cloud adapters implement Ledger, not a SQL proxy. Transaction callbacks may be
replayed: they contain only typed database operations, with identifiers, clocks,
SSH, inspection and reporting resolved outside the callback. Cloud adapters must
stage writes until all reads complete when required by their database.

claim_run is ONE admission transaction: validate/initialize activation, reject
an owner or unresolved case or duplicate slot, then commit ownership, the slot
and run intent together. A cloud constructor must not initialize activation.
claim_inspection is a separate admission boundary: obtain positive external
execution termination proof before transactionally replacing the EXACT observed
owner/revision. No timeout or missing heartbeat grants ownership. Every later
transaction verifies the acquired owner and configuration revision before writes.
release_if_resolved must retain ownership while a run/case is unresolved.
"""

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, ContextManager, Optional, Protocol, Tuple, TypeVar
import fcntl
import json
import sqlite3

T = TypeVar("T")
MAX_STAGES = 256
MAX_STAGE_BYTES = 8192
MAX_RECORD_BYTES = 32768


@dataclass(frozen=True)
class RunRecord:
    id: str
    slot: Optional[float]
    outcome: str


@dataclass(frozen=True)
class CaseRecord:
    id: str
    run: str
    site: str
    wan: str
    router_id: str
    outcome: str
    restored: bool


@dataclass(frozen=True)
class EventRecord:
    id: str
    payload: dict
    receipt: Optional[dict] = None


@dataclass(frozen=True)
class InspectionRecord:
    id: str
    case_id: str
    payload: dict


@dataclass(frozen=True)
class CaseTransition:
    expected: CaseRecord
    outcome: str
    restored: bool


@dataclass(frozen=True)
class Owner:
    authority: str
    activation: float
    revision: str
    run_id: str
    execution: str
    owner_id: str
    generation: int
    kind: str  # runner or inspector


@dataclass(frozen=True)
class Admission:
    status: str
    owner: Optional[Owner] = None


class Transaction(Protocol):
    """Typed snapshot operations; return detached records, never live documents.

    Creates accept identical deterministic retries and reject changed identities
    or facts. Terminal outcomes, event facts and accepted receipts are immutable;
    recovery can only change restored from false to true. CaseTransition compares
    the entire expected prior record before applying the permitted state change.
    """

    def activation(self, expected: float, *, initialize: bool = False) -> None: ...
    def get_run(self, run_id: str) -> Optional[RunRecord]: ...
    def find_run(self, slot: float) -> Optional[RunRecord]: ...
    def runs(
        self, *, terminal: bool = False, running: bool = False
    ) -> Tuple[RunRecord, ...]: ...
    def create_run(self, run: RunRecord) -> None: ...
    def finish_run(self, run_id: str, outcome: str) -> None: ...
    def get_case(self, case_id: str) -> Optional[CaseRecord]: ...
    def cases(
        self, *, unresolved: bool = False, terminal: bool = False
    ) -> Tuple[CaseRecord, ...]: ...
    def create_case(self, case: CaseRecord) -> None: ...
    def update_case(self, transition: CaseTransition) -> None: ...
    def append_stage(self, case_id: str, payload: dict) -> int: ...
    def stages(self, case_id: str) -> Tuple[dict, ...]: ...
    def get_event(self, event_id: str) -> Optional[EventRecord]: ...
    def create_event(self, event: EventRecord) -> None: ...
    def pending_events(self) -> Tuple[EventRecord, ...]: ...
    def set_receipt(self, event_id: str, receipt: dict) -> None: ...
    def append_inspection(self, inspection: InspectionRecord) -> None: ...
    def inspections(self, case_id: str) -> Tuple[InspectionRecord, ...]: ...
    def transition_case_event(
        self,
        transition: CaseTransition,
        event: EventRecord,
        inspection: Optional[InspectionRecord] = None,
    ) -> None: ...


class Ledger(Protocol):
    # True: durable backend is authoritative, do not require local evidence files,
    # and defer armed observations past the engine's arm-to-inject critical gap.
    authoritative: bool

    def lock(self) -> ContextManager[bool]:
        """Local flock, or a nonclaiming context for a cloud adapter."""
        ...

    def transact(
        self, callback: Callable[[Transaction], T], *, owner: Optional[Owner] = None
    ) -> T:
        """Atomic, bounded, database-only callback; rollback on any exception.

        A cloud adapter verifies the owner on every call. It may retry callbacks
        after transaction conflicts, but must resolve an ambiguous commit from
        deterministic records before replaying writes. If it cannot resolve the
        result, raise: Coordinator stops new cases and retains ownership.
        """
        ...

    def claim_run(self, activation: float, run: RunRecord) -> Admission:
        """Called under lock after calendar and selection checks.

        Return claimed, busy, recovery_unverified, or already_completed. Reject
        activation mismatch with ValueError. A cloud claimed result carries an
        Owner bound to trusted runtime/configuration identity, never caller-
        selected authority or execution. slot=None denotes a manual run.
        """
        ...

    def claim_inspection(self, activation: float) -> Admission:
        """Claim reconciliation; replacing any owner needs terminal proof.

        Collect external execution/task proof BEFORE the database transaction,
        then compare the exact observed owner/revision in it. Unknown, live or
        contradictory proof returns busy without any mutation. The same rule
        applies when the prior owner was an inspector. No proof is inferred by
        Coordinator from timestamps or health.
        """
        ...

    def release_if_resolved(self, owner: Optional[Owner]) -> None:
        """Fence release and retain ownership for running runs/unresolved cases."""
        ...


def encoded(payload, limit=MAX_RECORD_BYTES):
    value = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if len(value.encode("utf-8")) > limit:
        raise ValueError("record evidence limit exceeded")
    return value


class SQLiteTransaction:
    def __init__(self, db):
        self.db = db

    def activation(self, expected, *, initialize=False):
        row = self.db.execute(
            "SELECT value FROM config WHERE key=?", ("activation",)
        ).fetchone()
        if row is None:
            if not initialize:
                raise ValueError("activation missing from durable state")
            self.db.execute(
                "INSERT INTO config VALUES (?,?)", ("activation", json.dumps(expected))
            )
        elif json.loads(row[0]) != expected:
            raise ValueError("activation differs from durable state")

    def get_run(self, run_id):
        row = self.db.execute(
            "SELECT id,slot,outcome FROM runs WHERE id=?", (run_id,)
        ).fetchone()
        return RunRecord(*row) if row else None

    def find_run(self, slot):
        row = self.db.execute(
            "SELECT id,slot,outcome FROM runs WHERE slot=?", (slot,)
        ).fetchone()
        return RunRecord(*row) if row else None

    def runs(self, *, terminal=False, running=False):
        return tuple(
            RunRecord(*row)
            for row in self.db.execute(
                "SELECT id,slot,outcome FROM runs WHERE (?=0 OR outcome!='running') AND (?=0 OR outcome='running')",
                (terminal, running),
            )
        )

    def create_run(self, run):
        existing = self.get_run(run.id)
        if existing:
            if existing != run:
                raise ValueError("run intent is immutable")
            return
        self.db.execute(
            "INSERT INTO runs VALUES (?,?,?)", (run.id, run.slot, run.outcome)
        )

    def finish_run(self, run_id, outcome):
        run = self.get_run(run_id)
        if run is None:
            raise ValueError("run state missing")
        if run.outcome != "running" and run.outcome != outcome:
            raise ValueError("terminal run outcome is immutable")
        self.db.execute("UPDATE runs SET outcome=? WHERE id=?", (outcome, run_id))

    def get_case(self, case_id):
        row = self.db.execute(
            "SELECT id,run,site,wan,router_id,outcome,restored FROM cases WHERE id=?",
            (case_id,),
        ).fetchone()
        return CaseRecord(*row[:-1], bool(row[-1])) if row else None

    def cases(self, *, unresolved=False, terminal=False):
        return tuple(
            CaseRecord(*row[:-1], bool(row[-1]))
            for row in self.db.execute(
                "SELECT id,run,site,wan,router_id,outcome,restored FROM cases WHERE (?=0 OR restored=0) AND (?=0 OR outcome!='running')",
                (unresolved, terminal),
            )
        )

    def create_case(self, case):
        existing = self.get_case(case.id)
        if existing:
            if existing != case:
                raise ValueError("case intent is immutable")
            return
        self.db.execute(
            "INSERT INTO cases VALUES (?,?,?,?,?,?,?)",
            (
                case.id,
                case.run,
                case.site,
                case.wan,
                case.router_id,
                case.outcome,
                int(case.restored),
            ),
        )

    def update_case(self, transition):
        prior = self.get_case(transition.expected.id)
        if prior != transition.expected:
            raise ValueError("case state changed")
        if prior.outcome != "running" and prior.outcome != transition.outcome:
            raise ValueError("terminal case outcome is immutable")
        if prior.restored and not transition.restored:
            raise ValueError("restoration proof is immutable")
        self.db.execute(
            "UPDATE cases SET outcome=?,restored=? WHERE id=?",
            (transition.outcome, int(transition.restored), prior.id),
        )

    def append_stage(self, case_id, payload):
        value = encoded(payload, MAX_STAGE_BYTES)
        seq = self.db.execute(
            "SELECT count(*) FROM stages WHERE case_id=?", (case_id,)
        ).fetchone()[0]
        if seq >= MAX_STAGES:
            raise ValueError("stage evidence limit exceeded")
        self.db.execute("INSERT INTO stages VALUES (?,?,?)", (case_id, seq, value))
        return seq

    def stages(self, case_id):
        return tuple(
            json.loads(row[0])
            for row in self.db.execute(
                "SELECT payload FROM stages WHERE case_id=? ORDER BY sequence",
                (case_id,),
            )
        )

    def get_event(self, event_id):
        row = self.db.execute(
            "SELECT id,payload,receipt FROM events WHERE id=?", (event_id,)
        ).fetchone()
        return (
            EventRecord(
                row[0], json.loads(row[1]), json.loads(row[2]) if row[2] else None
            )
            if row
            else None
        )

    def create_event(self, event):
        payload = encoded(event.payload)
        if event.receipt is not None:
            raise ValueError("create event before receipt")
        existing = self.get_event(event.id)
        if existing:
            if existing.payload != event.payload:
                raise ValueError("event facts are immutable")
            return
        self.db.execute("INSERT INTO events VALUES (?,?,NULL)", (event.id, payload))

    def pending_events(self):
        return tuple(
            EventRecord(row[0], json.loads(row[1]))
            for row in self.db.execute(
                "SELECT id,payload FROM events WHERE receipt IS NULL"
            )
        )

    def set_receipt(self, event_id, receipt):
        event = self.get_event(event_id)
        if event is None:
            raise ValueError("event state missing")
        if event.receipt is not None and event.receipt != receipt:
            raise ValueError("delivery receipt is immutable")
        self.db.execute(
            "UPDATE events SET receipt=? WHERE id=?", (encoded(receipt), event_id)
        )

    def append_inspection(self, inspection):
        payload = encoded(inspection.payload)
        prior = self.db.execute(
            "SELECT case_id,payload FROM inspections WHERE id=?", (inspection.id,)
        ).fetchone()
        if prior:
            if (
                prior[0] != inspection.case_id
                or json.loads(prior[1]) != inspection.payload
            ):
                raise ValueError("inspection evidence is immutable")
            return
        self.db.execute(
            "INSERT INTO inspections VALUES (?,?,?)",
            (inspection.id, inspection.case_id, payload),
        )

    def inspections(self, case_id):
        return tuple(
            InspectionRecord(row[0], row[1], json.loads(row[2]))
            for row in self.db.execute(
                "SELECT id,case_id,payload FROM inspections WHERE case_id=?", (case_id,)
            )
        )

    def transition_case_event(self, transition, event, inspection=None):
        self.update_case(transition)
        if inspection is not None:
            self.append_inspection(inspection)
        self.create_event(event)


class SQLiteLedger:
    authoritative = False

    def __init__(self, db_path, lock_path, *, connection=None):
        self.db_path = db_path
        self.lock_path = lock_path
        self.connection = connection or self.db
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.executescript(
                """CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY,value TEXT);
            CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY,slot REAL UNIQUE,outcome TEXT);
            CREATE TABLE IF NOT EXISTS cases (id TEXT PRIMARY KEY,run TEXT,site TEXT,wan TEXT,router_id TEXT,outcome TEXT,restored INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY,payload TEXT NOT NULL,receipt TEXT);
            CREATE TABLE IF NOT EXISTS inspections (id TEXT PRIMARY KEY,case_id TEXT,payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS stages (case_id TEXT,sequence INTEGER,payload TEXT,PRIMARY KEY(case_id,sequence));"""
            )

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.db_path, timeout=1)
        try:
            db.execute("PRAGMA synchronous=FULL")
            with db:
                yield db
        finally:
            db.close()

    @contextmanager
    def lock(self):
        Path(self.lock_path).parent.mkdir(parents=True, exist_ok=True)
        with open(self.lock_path, "a") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
            try:
                yield True
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def transact(self, callback, *, owner=None):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            return callback(SQLiteTransaction(db))

    def claim_run(self, activation, run):
        def claim(tx):
            tx.activation(activation, initialize=True)
            if tx.cases(unresolved=True):
                return Admission("recovery_unverified")
            if run.slot is not None and tx.find_run(run.slot):
                return Admission("already_completed")
            tx.create_run(run)
            return Admission("claimed")

        return self.transact(claim)

    def claim_inspection(self, activation):
        # The caller holds flock: process exit is the local terminal proof.
        self.transact(lambda tx: tx.activation(activation, initialize=True))
        return Admission("claimed")

    def release_if_resolved(self, owner):
        # Process-scoped local ownership is released by flock's context manager.
        pass
