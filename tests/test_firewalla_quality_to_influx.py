import json
import subprocess
import sys
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "connection_monitoring/firewalla_quality.py"
FIBER = "11111111-1111-4111-8111-111111111111"
BACKUP = "22222222-2222-4222-8222-222222222222"
TARGET = "198.51.100.1"


def key(uuid=FIBER, target=TARGET):
    return f"metric:monitor:raw:ping:{target}:{uuid}"


def envelope(data):
    return {"code": 200, "data": data}


def run_converter(payload, *extra_args):
    raw = payload if isinstance(payload, str) else json.dumps(payload)
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--site",
            "demo site,west=1",
            "--router",
            "router one,primary=1",
            "--target",
            TARGET,
            "--wan",
            f"{FIBER}=fiber west",
            "--wan",
            f"{BACKUP}=backup,backup=1",
            *extra_args,
        ],
        input=raw + "\n",
        text=True,
        capture_output=True,
        check=False,
    )


def test_two_wans_emit_deterministically_with_complete_escaped_tags_and_ns_time():
    payload = envelope(
        {
            key(BACKUP): {
                "1786092078": {"stat": {"lossrate": 0.125, "mean": 42}}
            },
            key(FIBER): {
                "1786092077.294": {
                    "stat": {
                        "min": 1,
                        "max": 5,
                        "median": 2,
                        "mean": 2.5,
                        "lossrate": 0.005,
                    }
                }
            },
        }
    )

    result = run_converter(payload)

    assert result.returncode == 0
    assert result.stderr == ""
    assert result.stdout.splitlines() == [
        "wan_quality,site=demo\\ site\\,west\\=1,wan=fiber\\ west,router=router\\ one\\,primary\\=1,source=firewalla,target=198.51.100.1 packet_loss_percent=0.5,latency_ms=2.5 1786092077294000000",
        "wan_quality,site=demo\\ site\\,west\\=1,wan=backup\\,backup\\=1,router=router\\ one\\,primary\\=1,source=firewalla,target=198.51.100.1 packet_loss_percent=12.5,latency_ms=42 1786092078000000000",
    ]


def test_full_loss_omits_missing_latency_instead_of_inventing_it():
    result = run_converter(
        envelope({key(): {"1786092079": {"stat": {"lossrate": 1}}}})
    )

    assert result.returncode == 0
    assert "packet_loss_percent=100" in result.stdout
    assert "latency_ms" not in result.stdout


@pytest.mark.parametrize(
    "payload",
    [
        {"code": 500, "data": {}},
        {"code": True, "data": {}},
        {"code": 200, "data": []},
        {"code": 200, "data": {}, "unknown": 1},
        "not-json",
    ],
)
def test_malformed_or_unsuccessful_envelope_fails_without_output(payload):
    result = run_converter(payload)

    assert result.returncode != 0
    assert result.stdout == ""


@pytest.mark.parametrize(
    "bad_key",
    [
        lambda: key(uuid="unknown"),
        lambda: key(target="203.0.113.1"),
        lambda: f"metric:monitor:raw:dns:{TARGET}:{FIBER}",
    ],
)
def test_unknown_uuid_target_or_metric_is_not_emitted(bad_key):
    result = run_converter(
        envelope({bad_key(): {"1786092077": {"stat": {"lossrate": 0}}}})
    )

    assert result.returncode != 0
    assert result.stdout == ""


@pytest.mark.parametrize(
    "invalid_entry",
    [
        {key(uuid="unknown"): {"1786092078": {"stat": {"lossrate": 0}}}},
        {key(target="203.0.113.1"): {"1786092078": {"stat": {"lossrate": 0}}}},
        {key(): {"bad-timestamp": {"stat": {"lossrate": 0}}}},
        {key(): {"1786092078": {"stat": {"lossrate": "invalid"}}}},
    ],
)
def test_any_invalid_entry_rejects_the_entire_mixed_batch(invalid_entry):
    data = {key(): {"1786092077": {"stat": {"lossrate": 0, "mean": 2}}}}
    for quality_key, history in invalid_entry.items():
        data.setdefault(quality_key, {}).update(history)

    result = run_converter(envelope(data))

    assert result.returncode != 0
    assert result.stdout == ""
    assert "invalid quality input" in result.stderr


@pytest.mark.parametrize(
    "timestamp,stat",
    [
        ("not-an-epoch", {"lossrate": 0}),
        ("-1", {"lossrate": 0}),
        ("1786092077.0000000001", {"lossrate": 0}),
        ("1786092077", {"lossrate": True}),
        ("1786092077", {"lossrate": -0.1}),
        ("1786092077", {"lossrate": 1.1}),
        ("1786092077", {"lossrate": 0, "mean": True}),
        ("1786092077", {"lossrate": 0, "mean": -0.1}),
        ("1786092077", {"mean": 1}),
        ("1786092077", {"lossrate": 0, "unknown": 1}),
    ],
)
def test_invalid_point_values_fail_without_output(timestamp, stat):
    result = run_converter(envelope({key(): {timestamp: {"stat": stat}}}))

    assert result.returncode != 0
    assert result.stdout == ""


@pytest.mark.parametrize(
    "extra_args",
    [
        ("--wan", f"{FIBER}=other"),
        ("--wan", "third=fiber west"),
        ("--wan", "invalid"),
    ],
)
def test_duplicate_or_conflicting_wan_mappings_fail_closed(extra_args):
    result = run_converter(envelope({key(): {"1786092077": {"stat": {"lossrate": 0}}}}), *extra_args)

    assert result.returncode != 0
    assert result.stdout == ""


def test_duplicate_json_members_fail_closed():
    raw = (
        '{"code":200,"data":{"'
        + key()
        + '":{"1786092077":{"stat":{"lossrate":0}},'
        + '"1786092077":{"stat":{"lossrate":1}}}}}'
    )

    result = run_converter(raw)

    assert result.returncode != 0
    assert result.stdout == ""


def test_distinct_timestamp_spellings_that_collide_in_ns_fail_closed():
    result = run_converter(
        envelope(
            {
                key(): {
                    "1786092077": {"stat": {"lossrate": 0}},
                    "1786092077.0": {"stat": {"lossrate": 0}},
                }
            }
        )
    )

    assert result.returncode != 0
    assert result.stdout == ""


def test_no_valid_points_is_nonzero():
    result = run_converter(envelope({}))

    assert result.returncode != 0
    assert result.stdout == ""


def test_native_state_conversion_has_current_time_and_no_invented_loss():
    result=run_converter({'time':1000,'data':{FIBER:{'ready':True,'active':True},BACKUP:{'ready':False,'active':False}}},'--state')
    assert result.returncode == 0, result.stderr
    assert 'ready=1i,active=1i 1000000000000' in result.stdout
    assert 'ready=0i,active=0i 1000000000000' in result.stdout
    assert all(line.startswith('wan_state,') for line in result.stdout.splitlines())
    assert 'packet_loss' not in result.stdout


@pytest.mark.parametrize('payload', [{'time':1000,'data':{}},{'time':1000,'data':{FIBER:{'ready':0,'active':True}}},{'time':1000,'data':{FIBER:{'ready':False,'active':False,'secret':'no'}}},{'time':-1,'data':{FIBER:{'ready':False,'active':False}}}])
def test_native_state_conversion_fails_closed(payload):
    result=run_converter(payload,'--state')
    assert result.returncode == 65
    assert result.stdout == ''
