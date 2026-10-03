"""Whole-estate health gates use invented check identities only."""
import copy
import importlib

import pytest

IDS = ("alpha.router", "alpha.monitor", "beta.router", "beta.monitor", "maintenance")


def healthy():
    return {"version": 1, "observed_at": 1000, "checks": {
        key: {"ok": True, "observed_at": 1000, "details": {"ready": [True, 2, "observed", None]}}
        for key in IDS
    }}


def gate(report, required=IDS, **kwargs):
    module = importlib.import_module("connection_monitoring.dirt_health")
    return module.require_healthy(report, required, now=1000, max_age=120, **kwargs)


def test_complete_healthy_report_is_accepted_without_modification():
    report = healthy()
    original = copy.deepcopy(report)
    gate(report)
    assert report == original


@pytest.mark.parametrize("field", ["version", "observed_at", "checks"])
def test_missing_report_fields_rejected(field):
    report = healthy()
    del report[field]
    with pytest.raises(ValueError):
        gate(report)


@pytest.mark.parametrize("value", [True, False, 0, 2, 1.0, "1", None])
def test_version_requires_exact_integer_one(value):
    report = healthy()
    report["version"] = value
    with pytest.raises(ValueError):
        gate(report)


@pytest.mark.parametrize("value", [False, None, 0, 1, "true", [], {}])
def test_degraded_peer_or_nonboolean_health_blocks_whole_gate(value):
    report = healthy()
    report["checks"]["beta.router"]["ok"] = value
    with pytest.raises(ValueError):
        gate(report)


@pytest.mark.parametrize("field", ["ok", "observed_at", "details"])
def test_missing_check_fields_rejected(field):
    report = healthy()
    del report["checks"]["alpha.router"][field]
    with pytest.raises(ValueError):
        gate(report)


@pytest.mark.parametrize("per_check", [False, True])
@pytest.mark.parametrize("value", [True, False, float("nan"), float("inf"), -float("inf"), "1000", None, 879.9, 1005.1])
def test_invalid_stale_or_future_timestamps_rejected(per_check, value):
    report = healthy()
    target = report["checks"]["beta.monitor"] if per_check else report
    target["observed_at"] = value
    with pytest.raises(ValueError):
        gate(report)


def test_age_and_future_boundaries_are_inclusive():
    report = healthy()
    report["observed_at"] = 880
    report["checks"]["alpha.router"]["observed_at"] = 1005
    gate(report)


@pytest.mark.parametrize("required", [(), ("alpha.router", "alpha.router"), ("bad\nsecret",), "alpha.router", None])
def test_required_checks_are_explicit_nonempty_unique_safe_ids(required):
    with pytest.raises(ValueError):
        gate(healthy(), required)


@pytest.mark.parametrize("extra", [False, True])
def test_exact_check_coverage_required(extra):
    report = healthy()
    if extra:
        report["checks"]["unknown"] = report["checks"]["alpha.router"]
    else:
        del report["checks"]["maintenance"]
    with pytest.raises(ValueError):
        gate(report)


@pytest.mark.parametrize("details", [None, [], "secret", {"value": object()}, {"value": float("nan")}, {"value": "secret" * 1000}, {"value": list(range(65))}, {"value": [[[[[[[0]]]]]]]}])
def test_malformed_or_oversized_evidence_has_sanitized_errors(details):
    report = healthy()
    report["checks"]["alpha.router"]["details"] = details
    module = importlib.import_module("connection_monitoring.dirt_health")
    with pytest.raises(module.HealthRejected) as exc:
        gate(report)
    assert "secret" not in str(exc.value)
    assert len(str(exc.value)) <= 256


def test_hostile_extra_check_id_does_not_leak():
    report = healthy()
    report["checks"]["secret\ncredential"] = {}
    with pytest.raises(ValueError) as exc:
        gate(report)
    assert "secret" not in str(exc.value)


@pytest.mark.parametrize("name,value", [("now", True), ("now", float("nan")), ("max_age", -1), ("max_age", True), ("max_future_skew", -1)])
def test_invalid_time_policy_is_rejected(name, value):
    module = importlib.import_module("connection_monitoring.dirt_health")
    args = dict(now=1000, max_age=120)
    args[name] = value
    with pytest.raises(module.HealthRejected):
        module.require_healthy(healthy(), IDS, **args)


@pytest.mark.parametrize("target", ["report", "check"])
def test_extra_schema_fields_rejected(target):
    report = healthy()
    (report if target == "report" else report["checks"]["alpha.router"])["unexpected"] = "secret"
    with pytest.raises(ValueError):
        gate(report)


@pytest.mark.parametrize("target,value", [("report", []), ("checks", []), ("check", []), ("check", None)])
def test_wrong_container_types_rejected(target, value):
    report = healthy()
    if target == "report":
        report = value
    elif target == "checks":
        report["checks"] = value
    else:
        report["checks"]["alpha.router"] = value
    with pytest.raises(ValueError):
        gate(report)


def test_cyclic_evidence_and_overflow_numeric_evidence_rejected():
    for value in (10 ** 1000,):
        report = healthy()
        report["checks"]["alpha.router"]["details"] = {"value": value}
        with pytest.raises(ValueError):
            gate(report)
    report = healthy()
    details = report["checks"]["alpha.router"]["details"]
    details["cycle"] = details
    with pytest.raises(ValueError):
        gate(report)


@pytest.mark.parametrize("details", [
    {str(i): "a" * 1024 for i in range(17)},
    {str(i): list(range(8)) for i in range(32)},
    {str(i): True for i in range(65)},
    {"a" * 1025: True},
])
def test_evidence_byte_node_container_and_key_bounds(details):
    report = healthy()
    report["checks"]["alpha.router"]["details"] = details
    with pytest.raises(ValueError):
        gate(report)


def test_aggregate_report_size_is_bounded():
    ids = tuple("check" + str(i) for i in range(100))
    report = {"version": 1, "observed_at": 1000, "checks": {
        key: {"ok": True, "observed_at": 1000,
              "details": {str(i): "a" * 1024 for i in range(12)}} for key in ids}}
    with pytest.raises(ValueError, match="Oversized health report"):
        gate(report, ids)


def test_valid_safe_identifier_can_appear_in_error_but_facts_cannot():
    report = healthy()
    report["checks"]["beta.router"].update(ok=False, details={"secret": "credential"})
    with pytest.raises(ValueError) as exc:
        gate(report)
    assert "beta.router" in str(exc.value)
    assert "secret" not in str(exc.value) and "credential" not in str(exc.value)


def test_future_skew_policy_cannot_weaken_five_second_protocol_limit():
    with pytest.raises(ValueError, match="Invalid health time policy"):
        gate(healthy(), max_future_skew=3600)


def test_future_skew_policy_can_be_stricter():
    report = healthy()
    report["observed_at"] = 1001
    with pytest.raises(ValueError, match="future"):
        gate(report, max_future_skew=0)
