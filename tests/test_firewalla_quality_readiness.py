import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

HELPER = Path(__file__).resolve().parents[1] / "connection_monitoring/firewalla_readiness.py"
FIBER = "11111111-1111-4111-8111-111111111111"
BACKUP = "22222222-2222-4222-8222-222222222222"
TARGET = "198.51.100.1"
KEYS = [
    f"metric:monitor:raw:ping:{TARGET}:{FIBER}",
    f"metric:monitor:raw:ping:{TARGET}:{BACKUP}",
]


@pytest.fixture
def readiness():
    spec = importlib.util.spec_from_file_location("firewalla_quality_readiness", HELPER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def payload(timestamps=(1000, 1000), *, keys=KEYS):
    return json.dumps(
        {
            "code": 200,
            "data": {
                key: {str(timestamp): {"stat": {"mean": 12.5, "lossrate": 0, "median": 12}}}
                for key, timestamp in zip(keys, timestamps)
            },
        },
        separators=(",", ":"),
    )


def test_requires_both_exact_uuid_and_target_histories(readiness):
    assert readiness.export_is_ready(payload(), KEYS, 1000)
    assert not readiness.export_is_ready(payload(keys=KEYS[:1]), KEYS, 1000)

    wrong_target = [key.replace(TARGET, "203.0.113.1") for key in KEYS]
    assert not readiness.export_is_ready(payload(keys=wrong_target), KEYS, 1000)

    wrong_uuid = [KEYS[0], KEYS[1].replace(BACKUP, "unknown-uuid")]
    assert not readiness.export_is_ready(payload(keys=wrong_uuid), KEYS, 1000)


@pytest.mark.parametrize(
    "raw",
    [
        "not-json SECRET_RESPONSE",
        "[]",
        '{"code":500,"data":{}}',
        '{"code":200,"data":[]}',
        '{"code":200,"data":{"unexpected":"hostile"}}',
        json.dumps({"code": 200, "data": {KEYS[0]: {"NaN": {"stat": {"mean": 1}}}}}),
        json.dumps({"code": 200, "data": {KEYS[0]: {"1000": {"stat": {"mean": True}}}}}),
    ],
)
def test_malformed_or_hostile_envelopes_fail_closed(readiness, raw):
    assert readiness.export_is_ready(raw, KEYS, 0) is False


def test_one_stale_key_keeps_the_export_unready(readiness):
    assert not readiness.export_is_ready(payload((1000, 999)), KEYS, 1000)


def test_mutation_boundary_rejects_pre_policy_race_then_accepts_new_samples(readiness):
    responses = [payload((1000, 1000)), payload((1001, 1001))]
    calls = []

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout=responses.pop(0))

    now = [0.0]

    assert readiness.wait_for_quality(
        KEYS,
        readiness.readiness_cutoff(1000, policy_changed=True),
        gate_path="/demo/gate",
        runner=runner,
        clock=lambda: now[0],
        sleeper=lambda seconds: now.__setitem__(0, now[0] + seconds),
        deadline_seconds=20,
        poll_interval=1,
    )
    assert len(calls) == 2


def test_noop_cutoff_accepts_existing_samples_from_normal_ten_minute_window(readiness):
    assert readiness.readiness_cutoff(2000, policy_changed=False) == 1400
    assert readiness.readiness_cutoff(2000, policy_changed=True) == 2001
    assert readiness.export_is_ready(payload((1400, 1400)), KEYS, 1400)


def test_command_failures_retry_with_exact_sanitized_gate_invocation(readiness):
    results = [
        SimpleNamespace(returncode=69, stdout="SECRET_RESPONSE"),
        SimpleNamespace(returncode=0, stdout=payload()),
    ]
    calls = []
    now = [0.0]

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        return results.pop(0)

    assert readiness.wait_for_quality(
        KEYS,
        1000,
        gate_path="/demo/gate",
        runner=runner,
        clock=lambda: now[0],
        sleeper=lambda seconds: now.__setitem__(0, now[0] + seconds),
        deadline_seconds=20,
        poll_interval=1,
    )
    assert len(calls) == 2
    for argv, kwargs in calls:
        assert argv == ["/demo/gate"]
        assert kwargs["env"]["SSH_ORIGINAL_COMMAND"] == "export-network-quality"
        assert kwargs["capture_output"] is True
        assert kwargs["text"] is True
        assert kwargs["check"] is False
        assert 0 < kwargs["timeout"] <= 10


def test_strict_deadline_rejects_even_a_valid_late_response(readiness):
    now = [0.0]
    timeouts = []

    def runner(unused_argv, **kwargs):
        timeouts.append(kwargs["timeout"])
        now[0] = 10.0
        return SimpleNamespace(returncode=0, stdout=payload())

    assert not readiness.wait_for_quality(
        KEYS,
        1000,
        gate_path="/demo/gate",
        runner=runner,
        clock=lambda: now[0],
        sleeper=lambda unused_seconds: None,
        deadline_seconds=10,
    )
    assert timeouts == [10]


@pytest.mark.parametrize(
    ("cutoff", "keys"),
    [("SECRET_CUTOFF", "SECRET_KEYS"), ("1000", 'json:[{"SECRET":"VALUE"}]')],
)
def test_cli_input_errors_are_fixed_and_do_not_echo_values(readiness, capsys, cutoff, keys):
    assert (
        readiness.main(
            [
                "--freshness-cutoff",
                cutoff,
                "--expected-keys",
                keys,
            ]
        )
        == 65
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "invalid network-quality readiness inputs\n"


def test_cli_requires_explicit_gate_before_effects(readiness, monkeypatch, capsys):
    monkeypatch.setattr(readiness.subprocess, 'run', lambda *args, **kwargs: pytest.fail('unexpected process'))
    assert readiness.main(['--freshness-cutoff', '1000', '--expected-keys', 'json:' + json.dumps(KEYS)]) == 65
    assert capsys.readouterr().err == 'invalid network-quality readiness inputs\n'
