import importlib.util
from pathlib import Path
import pytest
from connection_monitoring.dirt_health import HealthRejected

ROOT = Path(__file__).resolve().parents[1]

CONFIG = {
    "version": 1,
    "sites": {
        "third-site": {
            "primary": "uplink-a",
            "wans": {
                "uplink-a": {
                    "interface": "eth0",
                    "uuid": "11111111-1111-4111-8111-111111111111",
                },
                "uplink-b": {
                    "interface": "eth2",
                    "uuid": "22222222-2222-4222-8222-222222222222",
                },
            },
            "management_interface": "br0",
            "state_dir": "/run/sample-drill",
            "timer_prefix": "sample-drill-",
            "router": {
                "host": "192.0.2.1",
                "user": "operator",
                "known_hosts": "/tmp/router-hosts",
                "key_path": "/tmp/router-key",
            },
            "monitor": {
                "host": "192.0.2.7",
                "user": "observer",
                "known_hosts": "/tmp/monitor-hosts",
                "key_path": "/tmp/monitor-key",
            },
            "observer": {
                "collector_unit": "sample-quality.timer",
                "probe_url": "https://example.test/trace",
                "body_marker": "address=",
                "grafana_port": 3000,
                "token_env_file": "/tmp/secrets.env",
                "token_env_name": "VIEWER_TOKEN",
                "delivery_db": "/tmp/delivery.sqlite",
            },
            "alerts": {
                w: {
                    "labels": {
                        "site": "third-site",
                        "wan": w,
                        "service": "connectivity",
                    },
                    "title": "Link missing - " + w,
                    "uid": "link_missing_third-site_" + w,
                }
                for w in ("uplink-a", "uplink-b")
            },
        }
    },
}


def load(name):
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "connection_monitoring" / f"{name}.py"
    )
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.SITES = CONFIG["sites"]
    return m


def test_fault_targets_only_selected_interface_and_owned_chains():
    m = load("wan_fault")
    cmds = m.fault_commands("eth0", "a" * 32)
    assert len(cmds) == 12
    assert all(c[0] in ("iptables", "ip6tables") for c in cmds)
    assert all("eth2" not in c and "br0" not in c for c in cmds)
    assert all("-F" not in c for c in cmds)
    for c in m.restore_commands("eth0", "a" * 32):
        if "-F" in c:
            assert c[c.index("-F") + 1].startswith("DIRT_")


def test_guards_reject_lan_drift_and_unready_backup():
    m = load("wan_fault")
    info = {
        "br0": {"type": "lan"},
        "eth0": {
            "uuid": m.SITES["third-site"]["wans"]["uplink-a"]["uuid"],
            "type": "wan",
            "ready": True,
            "active": True,
        },
        "eth2": {
            "uuid": m.SITES["third-site"]["wans"]["uplink-b"]["uuid"],
            "type": "wan",
            "ready": True,
            "active": False,
        },
    }
    m.check_baseline(info, "uplink-a", "br0", "third-site")
    with pytest.raises(ValueError):
        m.check_baseline(info, "uplink-a", "eth0", "third-site")
    info["eth2"]["ready"] = False
    with pytest.raises(ValueError):
        m.check_baseline(info, "uplink-a", "br0", "third-site")
    info["eth2"]["ready"] = True
    info["eth0"]["uuid"] = "changed"
    with pytest.raises(ValueError):
        m.check_baseline(info, "uplink-a", "br0", "third-site")


@pytest.mark.parametrize("run", ["../bad", "a;reboot", "a" * 31])
def test_invalid_identifiers_rejected(run):
    with pytest.raises(ValueError):
        load("wan_fault").validate("uplink-a", run, 600, "third-site")


def test_unknown_wan_or_unbounded_duration_rejected():
    m = load("wan_fault")
    for wan, ttl in [("both", 600), ("uplink-a", 1), ("uplink-b", 36000)]:
        with pytest.raises(ValueError):
            m.validate(wan, "a" * 32, ttl, "third-site")


def test_failover_requires_real_backup_active_and_lan_internet():
    m = load("dirt")
    s = {
        "uplink-a": {"ready": False, "active": False},
        "uplink-b": {"ready": True, "active": True},
    }
    assert m.fault_observed(s, "uplink-a", True, "uplink-a")
    assert not m.fault_observed(s, "uplink-a", False, "uplink-a")
    s["uplink-b"]["active"] = False
    assert not m.fault_observed(s, "uplink-a", True, "uplink-a")


def test_standby_loss_and_return_do_not_require_switching_to_standby():
    m = load("dirt")
    s = {
        "uplink-a": {"ready": True, "active": True},
        "uplink-b": {"ready": False, "active": False},
    }
    assert m.fault_observed(s, "uplink-b", True, "uplink-a")
    assert not m.recovered(s, True, "uplink-a")
    s["uplink-b"]["ready"] = True
    assert m.recovered(s, True, "uplink-a")


def test_cleanup_cannot_turn_failed_assertions_into_pass():
    m = load("dirt")
    assert not m.verdict(
        {"fault": True, "alert": False, "recovery": True, "cleanup": True}
    )
    assert not m.verdict(
        {"fault": True, "alert": True, "recovery": True, "cleanup": False}
    )
    assert m.verdict({"fault": True, "alert": True, "recovery": True, "cleanup": True})


def test_cleanup_query_failure_is_not_proof_of_recovery(monkeypatch):
    import subprocess

    m = load("wan_fault")
    monkeypatch.setattr(
        m,
        "command",
        lambda args, check=True: subprocess.CompletedProcess(
            args, 4, "", "lock timeout"
        ),
    )
    with pytest.raises(ValueError):
        m.firewall_clean("eth0", "a" * 32)


def test_successful_empty_firewall_listing_proves_cleanup(monkeypatch):
    import subprocess

    m = load("wan_fault")
    monkeypatch.setattr(
        m,
        "command",
        lambda args, check=True: subprocess.CompletedProcess(
            args, 0, "-P INPUT ACCEPT\n-P OUTPUT ACCEPT\n", ""
        ),
    )
    assert m.firewall_clean("eth0", "a" * 32)


def test_router_failure_restores_and_cannot_pass(monkeypatch):
    m = load("dirt")
    events = []
    calls = []

    def fake(site, role, source, args=()):
        if role == "monitor":
            return {
                "internet": True,
                "collector_active": True,
                "alert": {"state": "inactive", "health": "ok"},
            }
        action = args[0]
        calls.append(action)
        if action == "inject":
            raise ValueError("Injection failed")
        return {"baseline": True, "armed": True, "cleanup": True}

    monkeypatch.setattr(m, "ssh", fake)
    assert not m.run(
        "third-site",
        "uplink-b",
        "a" * 32,
        lambda stage, data: events.append((stage, data)),
        poll=0,
    )
    assert calls == ["probe", "arm", "inject", "restore"]
    assert events[-1][1]["checks"]["cleanup"] is True
    assert events[-1][1]["passed"] is False


def test_full_standby_fault_and_recovery_lifecycle(monkeypatch):
    import time

    m = load("dirt")
    events = []
    actions = []
    phase = ["baseline"]
    normal = {
        "uplink-a": {"ready": True, "active": True},
        "uplink-b": {"ready": True, "active": False},
    }
    bad = {
        "uplink-a": {"ready": True, "active": True},
        "uplink-b": {"ready": False, "active": False},
    }

    def fake(site, role, source, args=()):
        if role == "monitor":
            return {
                "internet": True,
                "collector_active": True,
                "alert": {
                    "state": "firing" if phase[0] == "fault" else "inactive",
                    "health": "ok",
                },
                "delivery_history": {
                    "status": "firing" if phase[0] == "fault" else "resolved",
                    "accepted": time.time() + 1,
                },
            }
        action = args[0]
        actions.append(action)
        if action == "inject":
            phase[0] = "fault"
        if action == "restore":
            phase[0] = "recovery"
        return {
            "fault_present": phase[0] == "fault",
            "wans": bad if phase[0] == "fault" else normal,
            "cleanup": True,
        }

    monkeypatch.setattr(m, "ssh", fake)
    assert m.run(
        "third-site",
        "uplink-b",
        "a" * 32,
        lambda stage, data: events.append((stage, data)),
        poll=0,
    )
    assert actions.index("arm") < actions.index("inject") < actions.index("restore")
    assert events[-1][1]["passed"] is True


@pytest.fixture
def router_harness(monkeypatch, tmp_path):
    import subprocess, sys

    m = load("wan_fault")
    log = []
    rules = {"iptables": [], "ip6tables": []}
    deadline = None
    cfg = m.SITES["third-site"]
    info = {
        w["interface"]: {
            "uuid": w["uuid"],
            "type": "wan",
            "ready": True,
            "active": name == "uplink-a",
        }
        for name, w in cfg["wans"].items()
    }
    info["br0"] = {"type": "lan"}
    cfg["state_dir"] = str(tmp_path)
    monkeypatch.setattr(m, "STATE", tmp_path)
    monkeypatch.setattr(m, "network_info", lambda: info)
    monkeypatch.setattr(m, "management_interface", lambda: "br0")
    monkeypatch.setattr(m.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        m, "RECOVERY_SOURCE", Path(m.__file__).read_text(), raising=False
    )

    def cmd(args, check=True):
        nonlocal deadline
        log.append(args)
        stdout = ""
        if args[0] == "systemd-run":
            deadline = int((m.time.monotonic() + 900) * 1000000)
        if args[0] == "busctl":
            import json
            prop = args[-1]
            service = "sample-drill-" + "a" * 32 + ".service"
            argv = ["/usr/bin/python3", str(tmp_path / ("a" * 32 + ".py")), "restore",
                    "--site", "third-site", "--wan", "uplink-b", "--run", "a" * 32, "--ttl", "900"]
            values = {"ActiveState": ("s", "active"), "Unit": ("s", service),
                "TimersMonotonic": ("a(stt)", [["OnActiveUSec", 900000000, deadline]]),
                "ExecStart": ("a(sasbttttuii)", [[argv[0], argv, False, 0, 0, 0, 0, 0, 0, 0]])}
            kind, data = values[prop]
            stdout = json.dumps({"type": kind, "data": data})
        if args[0] in rules:
            binary = args[0]
            parts = args[3:]
            op = parts[0]
            if op == "-S":
                stdout = "\n".join(" ".join(r) for r in rules[binary])
            elif op in ("-N", "-A"):
                rules[binary].append(parts)
            elif op == "-I":
                rules[binary].append(["-A", parts[1], *parts[3:]])
            elif op == "-D":
                rule = ["-A", *parts[1:]]
                if rule in rules[binary]:
                    rules[binary].remove(rule)
            elif op == "-F":
                rules[binary][:] = [
                    r for r in rules[binary] if not (r[0] == "-A" and r[1] == parts[1])
                ]
            elif op == "-X":
                rules[binary][:] = [r for r in rules[binary] if r != ["-N", parts[1]]]
        return subprocess.CompletedProcess(args, 0, stdout, "")

    monkeypatch.setattr(m, "command", cmd)

    def invoke(action):
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "wan_fault",
                action,
                "--site",
                "third-site",
                "--wan",
                "uplink-b",
                "--run",
                "a" * 32,
            ],
        )
        m.main(config=CONFIG)

    return m, log, rules, cmd, invoke


def test_router_arms_independent_recovery_before_drops_and_cleans_only_owned_rules(
    router_harness,
):
    m, log, rules, cmd, invoke = router_harness
    unrelated = ["-A", "OUTPUT", "-o", "eth0", "-j", "ACCEPT"]
    rules["iptables"].append(unrelated)
    invoke("arm")
    invoke("inject")
    timer = next(i for i, c in enumerate(log) if c[0] == "systemd-run")
    drop = next(i for i, c in enumerate(log) if "-N" in c)
    assert timer < drop and any("--property=Restart=on-failure" in c for c in log)
    assert m.firewall_present("eth2", "a" * 32)
    invoke("restore")
    assert rules["iptables"] == [unrelated] and rules["ip6tables"] == []
    assert (
        not (m.STATE / "active.json").exists()
        and not (m.STATE / ("a" * 32 + ".py")).exists()
    )


def test_timer_arm_failure_never_installs_drop_rules(router_harness, monkeypatch):
    m, log, rules, cmd, invoke = router_harness

    def failed(args, check=True):
        if args[0] == "systemd-run":
            raise ValueError("Timer failed")
        return cmd(args, check)

    monkeypatch.setattr(m, "command", failed)
    with pytest.raises(ValueError):
        invoke("arm")
    assert not any("-N" in c for c in log)
    assert not (m.STATE / "active.json").exists()


def test_partial_injection_rolls_back_ipv4_and_ipv6(router_harness, monkeypatch):
    m, log, rules, cmd, invoke = router_harness
    invoke("arm")

    def partial(args, check=True):
        if args[0] == "ip6tables" and "-N" in args:
            raise ValueError("Second family failed")
        return cmd(args, check)

    monkeypatch.setattr(m, "command", partial)
    with pytest.raises(ValueError):
        invoke("inject")
    assert rules["iptables"] == [] and rules["ip6tables"] == []
    assert not (m.STATE / "active.json").exists()


def test_suite_orders_standby_before_primary_and_stops_on_failure(
    monkeypatch, tmp_path
):
    import sys

    m = load("dirt")
    calls = []

    def failed(site, wan, run_id, emit):
        calls.append(wan)
        return False

    monkeypatch.setattr(m, "run", failed)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "dirt",
            "--site",
            "third-site",
            "--suite",
            "--execute",
            "--output",
            str(tmp_path / "evidence.jsonl"),
        ],
    )
    with pytest.raises(SystemExit) as exc:
        m.main(config=CONFIG)
    assert exc.value.code == 1 and calls == ["uplink-b"]


def test_suite_runs_primary_only_after_standby_recovery_passes(monkeypatch, tmp_path):
    import sys

    m = load("dirt")
    calls = []

    def passed(site, wan, run_id, emit):
        calls.append(wan)
        return True

    monkeypatch.setattr(m, "run", passed)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "dirt",
            "--site",
            "third-site",
            "--suite",
            "--execute",
            "--output",
            str(tmp_path / "evidence.jsonl"),
        ],
    )
    with pytest.raises(SystemExit) as exc:
        m.main(config=CONFIG)
    assert exc.value.code == 0 and calls == ["uplink-b", "uplink-a"]


def test_notification_correlation_accepts_real_grafana_without_internal_uid():
    import hashlib, json

    m = load("dirt")
    labels = {
        "site": "third-site",
        "wan": "uplink-b",
        "service": "connectivity",
        "alertname": "Link missing - uplink-b",
    }
    key = hashlib.sha256(
        json.dumps(labels, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert key in m.subject_keys("third-site", "uplink-b")
    assert key not in m.subject_keys("third-site", "uplink-a")
    assert len(m.subject_keys("third-site", "uplink-b")) == 2


def test_rule_read_survives_brief_monitor_restart_without_inventing_state():
    import io, json, urllib.error

    m = load("dirt")
    calls = []
    sleeps = []

    def open_once(req, timeout):
        calls.append((req.full_url, timeout))
        if len(calls) == 1:
            raise urllib.error.URLError("connection refused")
        return io.BytesIO(json.dumps({"data": {"groups": []}}).encode())

    assert m.read_rules("secret", opener=open_once, sleep=sleeps.append) == {
        "data": {"groups": []}
    }
    assert (
        calls == [("http://127.0.0.1:3000/api/prometheus/grafana/api/v1/rules", 5)] * 2
    )
    assert sleeps == [2]


def test_rule_read_retries_are_bounded_and_auth_errors_stop():
    import urllib.error, pytest

    m = load("dirt")
    calls = []
    sleeps = []

    def unavailable(*a, **k):
        calls.append(1)
        raise TimeoutError()

    with pytest.raises(ValueError, match="Grafana observation unavailable"):
        m.read_rules("secret", opener=unavailable, sleep=sleeps.append)
    assert len(calls) == 4 and sleeps == [2, 2, 2]

    def unauthorized(*a, **k):
        raise urllib.error.HTTPError("hidden", 401, "secret", {}, None)

    with pytest.raises(urllib.error.HTTPError):
        m.read_rules(
            "secret",
            opener=unauthorized,
            sleep=lambda _: pytest.fail("must not retry invalid credentials"),
        )


@pytest.mark.parametrize(
    "mutation",
    [
        lambda c: c["sites"]["third-site"].pop("router"),
        lambda c: c["sites"]["third-site"]["wans"]["uplink-b"].update(interface="br0"),
        lambda c: c["sites"]["third-site"]["wans"]["uplink-b"].update(
            uuid=c["sites"]["third-site"]["wans"]["uplink-a"]["uuid"]
        ),
        lambda c: c["sites"]["third-site"].update(primary="absent"),
        lambda c: c["sites"]["third-site"]["observer"].pop("delivery_db"),
    ],
)
def test_invalid_configuration_cannot_reach_ssh(mutation, monkeypatch):
    import copy

    m = load("dirt")
    cfg = copy.deepcopy(CONFIG)
    mutation(cfg)
    monkeypatch.setattr(m, "ssh", lambda *a: pytest.fail("invalid config reached SSH"))
    with pytest.raises(SystemExit):
        m.main(["--site", "third-site", "--suite", "--preflight"], config=cfg)


def test_nonboolean_active_and_management_lan_drift_rejected(router_harness):
    m, _, _, _, _ = router_harness
    info = m.network_info()
    info["eth0"]["active"] = 1
    with pytest.raises(ValueError):
        m.check_baseline(info, "uplink-b", "br0", "third-site")
    info["eth0"]["active"] = True
    info["br0"]["type"] = "wan"
    with pytest.raises(ValueError):
        m.check_baseline(info, "uplink-b", "br0", "third-site")


def test_unknown_firewall_inspection_retains_recovery_resources(
    router_harness, monkeypatch
):
    import subprocess

    m, log, _, cmd, invoke = router_harness
    invoke("arm")
    invoke("inject")

    def failed(args, check=True):
        if args[0] == "ip6tables" and "-S" in args:
            return subprocess.CompletedProcess(args, 1, "", "unknown")
        return cmd(args, check)

    monkeypatch.setattr(m, "command", failed)
    with pytest.raises(ValueError):
        invoke("restore")
    assert (m.STATE / "active.json").exists() and (
        m.STATE / ("a" * 32 + ".py")
    ).exists()
    assert not any(c[:2] == ["systemctl", "stop"] for c in log)


def test_readonly_existing_lock_still_attempts_firewall_recovery(router_harness, monkeypatch):
    import errno

    m, log, rules, cmd, invoke = router_harness
    invoke("arm")
    invoke("inject")
    foreign = ["-A", "OUTPUT", "-o", "eth0", "-j", "ACCEPT"]
    rules["iptables"].append(foreign)
    original_open, original_unlink = Path.open, Path.unlink

    def readonly_open(path, mode="r", *args, **kwargs):
        if path.is_relative_to(m.STATE) and any(flag in mode for flag in "wax+"):
            raise OSError(errno.EROFS, "read-only simulation")
        return original_open(path, mode, *args, **kwargs)

    def readonly_unlink(path, *args, **kwargs):
        if path.is_relative_to(m.STATE):
            raise OSError(errno.EROFS, "read-only simulation")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", readonly_open)
    monkeypatch.setattr(Path, "unlink", readonly_unlink)
    log.clear()
    with pytest.raises(OSError):
        invoke("restore")
    assert rules == {"iptables": [foreign], "ip6tables": []}
    assert (m.STATE / "active.json").exists()
    assert (m.STATE / ("a" * 32 + ".py")).exists()
    assert not any(c[:2] == ["systemctl", "stop"] for c in log)

def test_helper_removal_failure_keeps_timer_for_retry(router_harness, monkeypatch):
    import errno

    m, log, rules, cmd, invoke = router_harness
    invoke("arm")
    invoke("inject")
    original = Path.unlink
    def fail_helper(path, *args, **kwargs):
        if path.suffix == ".py":
            raise OSError(errno.EROFS, "read-only simulation")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "unlink", fail_helper)
    log.clear()
    with pytest.raises(OSError):
        invoke("restore")
    assert rules == {"iptables": [], "ip6tables": []}
    assert (m.STATE / ("a" * 32 + ".py")).exists()
    assert (m.STATE / "active.json").exists()
    assert not any(c[:2] == ["systemctl", "stop"] for c in log)


@pytest.mark.parametrize("property", ["ActiveState", "Unit", "TimersMonotonic", "ExecStart"])
def test_injection_requires_owned_live_timer(router_harness, monkeypatch, property):
    import json, subprocess
    m, log, rules, cmd, invoke = router_harness
    invoke("arm")
    def wrong_timer(args, check=True):
        if args[0] == "busctl" and args[-1] == property:
            return subprocess.CompletedProcess(args, 0, json.dumps({"type": "s", "data": "foreign"}), "")
        return cmd(args, check)
    monkeypatch.setattr(m, "command", wrong_timer)
    with pytest.raises(ValueError):
        invoke("inject")
    assert rules == {"iptables": [], "ip6tables": []}


@pytest.mark.parametrize("change", [{"ttl": 1200}, {"armed_at": 0}, {"armed_at": 999999999999}])
def test_injection_rejects_changed_or_expired_marker(router_harness, change):
    import json
    m, log, rules, cmd, invoke = router_harness
    invoke("arm")
    marker = m.STATE / "active.json"
    marker.write_text(json.dumps(dict(json.loads(marker.read_text()), **change)))
    with pytest.raises(ValueError):
        invoke("inject")
    assert rules == {"iptables": [], "ip6tables": []}


@pytest.fixture
def router_clock(router_harness, monkeypatch):
    from types import SimpleNamespace

    m, _, _, _, _ = router_harness
    clock = [100.0]
    monkeypatch.setattr(m, "time", SimpleNamespace(
        monotonic=lambda: clock[0], time=lambda: clock[0] + 900))
    return clock


def test_arm_records_original_verified_monotonic_deadline(router_harness, router_clock):
    import json

    m, _, _, _, invoke = router_harness
    invoke("arm")
    saved = json.loads((m.STATE / "active.json").read_text())
    assert type(saved["deadline_monotonic_usec"]) is int
    assert saved["deadline_monotonic_usec"] == 1000000000


@pytest.mark.parametrize("drift_usec", [-10000000, 10000000])
def test_inject_rejects_original_deadline_drift_before_firewall_mutation(
    router_harness, router_clock, monkeypatch, drift_usec
):
    import json

    m, log, rules, cmd, invoke = router_harness
    invoke("arm")
    router_clock[0] += 10

    def drifted(args, check=True):
        result = cmd(args, check)
        if args[0] == "busctl" and args[-1] == "TimersMonotonic":
            proof = json.loads(result.stdout)
            proof["data"][0][2] += drift_usec
            result.stdout = json.dumps(proof)
        return result

    monkeypatch.setattr(m, "command", drifted)
    log.clear()
    with pytest.raises(ValueError):
        invoke("inject")
    assert not any(c[0] in rules for c in log)
    assert rules == {"iptables": [], "ip6tables": []}
    assert (m.STATE / "active.json").exists()
    assert (m.STATE / ("a" * 32 + ".py")).exists()


@pytest.mark.parametrize("deadline", [None, True, 1000000000.0, "1000000000"])
def test_inject_requires_typed_original_deadline(router_harness, router_clock, deadline):
    import json

    m, log, rules, _, invoke = router_harness
    invoke("arm")
    marker = m.STATE / "active.json"
    saved = json.loads(marker.read_text())
    saved["deadline_monotonic_usec"] = deadline
    if deadline is None:
        del saved["deadline_monotonic_usec"]
    marker.write_text(json.dumps(saved))
    log.clear()
    with pytest.raises(ValueError):
        invoke("inject")
    assert not any(c[0] in rules for c in log)
    assert rules == {"iptables": [], "ip6tables": []}


def test_inject_keeps_original_deadline_after_time_advances(router_harness, router_clock):
    import json

    m, log, rules, cmd, invoke = router_harness
    invoke("arm")
    original = (m.STATE / "active.json").read_bytes()
    router_clock[0] += 10
    invoke("inject")
    assert m.firewall_present("eth2", "a" * 32)
    assert (m.STATE / "active.json").read_bytes() == original
    proof = cmd(["busctl", "TimersMonotonic"])
    assert json.loads(proof.stdout)["data"][0][2] == 1000000000
    assert sum(c[0] == "systemd-run" for c in log) == 1


def test_duplicate_arm_cannot_extend_deadline(router_harness, router_clock):
    m, log, rules, cmd, invoke = router_harness
    invoke("arm")
    original = (m.STATE / "active.json").read_bytes()
    router_clock[0] += 10
    with pytest.raises(ValueError):
        invoke("arm")
    assert sum(c[0] == "systemd-run" for c in log) == 1
    assert (m.STATE / "active.json").read_bytes() == original
    invoke("inject")
    assert m.firewall_present("eth2", "a" * 32)


def test_timer_stop_failure_retains_recovery_and_never_reports_cleanup(router_harness, monkeypatch):
    import subprocess
    m, log, rules, cmd, invoke = router_harness
    invoke("arm"); invoke("inject")
    original = (m.STATE / ("a"*32+".py")).read_bytes()
    def failed_stop(args, check=True):
        if args[:2] == ['systemctl','stop']:
            return subprocess.CompletedProcess(args,1,'','')
        return cmd(args,check)
    monkeypatch.setattr(m,'command',failed_stop)
    with pytest.raises(ValueError): invoke("restore")
    assert rules == {"iptables":[],"ip6tables":[]}
    assert (m.STATE / ("a"*32+".py")).read_bytes() == original


@pytest.mark.parametrize('mutation', ['missing','inactive','expired','mismatched'])
def test_inject_never_mutates_without_matching_recovery(router_harness, monkeypatch, mutation):
    import json, subprocess
    m, log, rules, cmd, invoke = router_harness
    invoke('arm')
    marker=m.STATE/'active.json'
    if mutation=='missing': marker.unlink()
    elif mutation=='mismatched': marker.write_text(json.dumps(dict(json.loads(marker.read_text()),run='b'*32)))
    else:
        def unavailable(args,check=True):
            if args[0]=='busctl' and args[-1]=='TimersMonotonic':
                return subprocess.CompletedProcess(args,0,json.dumps({'type':'a(stt)','data':[['OnActiveUSec',900000000,0]]}),'')
            if mutation=='inactive' and args[:2]==['systemctl','is-active']: raise ValueError('inactive')
            return cmd(args,check)
        monkeypatch.setattr(m,'command',unavailable)
    with pytest.raises(ValueError): invoke('inject')
    assert rules == {"iptables":[],"ip6tables":[]}


@pytest.mark.parametrize('change',['missing','changed'])
def test_inject_requires_original_standalone_helper(router_harness,change):
    m, log, rules, cmd, invoke = router_harness
    invoke('arm')
    helper=m.STATE/('a'*32+'.py')
    if change=='missing': helper.unlink()
    else: helper.write_text('raise SystemExit(0)\n')
    with pytest.raises(ValueError): invoke('inject')
    assert rules == {"iptables":[],"ip6tables":[]}


def test_saved_recovery_runs_without_package_config_or_zipapp(router_harness, tmp_path):
    import subprocess, sys, json, os

    m, _, _, _, invoke = router_harness
    invoke("arm")
    invoke("inject")
    helper = m.STATE / ("a" * 32 + ".py")
    saved = helper.read_text()
    assert "EMBEDDED_SITE" in saved and "connection_monitoring" not in saved
    assert "router-key" not in saved and "delivery.sqlite" not in saved
    # Execute a separate process from only the saved helper and harmless fake binaries.
    binaries = tmp_path / "bin"
    binaries.mkdir()
    log = tmp_path / "commands"
    for binary in ("iptables", "ip6tables", "systemctl"):
        file = binaries / binary
        file.write_text(
            '#!/bin/sh\nprintf "%s\\n" "'
            + binary
            + ' $*" >> "'
            + str(log)
            + '"\nexit 0\n'
        )
        file.chmod(0o755)
    runtime = tmp_path / "deployed.pyz"
    runtime.write_text("not needed")
    runtime.unlink()
    config = tmp_path / "external.json"
    config.write_text("{}")
    config.unlink()
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            "import os,runpy,sys;os.geteuid=lambda:0;sys.argv=sys.argv[1:];runpy.run_path(sys.argv[0],run_name='__main__')",
            str(helper),
            "restore",
            "--site",
            "third-site",
            "--wan",
            "uplink-b",
            "--run",
            "a" * 32,
        ],
        env={**os.environ, "PATH": str(binaries)},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["cleanup"] is True
    assert not helper.exists() and not (m.STATE / "active.json").exists()
    commands = log.read_text()
    assert (
        "iptables -w 5 -F DIRT_" in commands and "ip6tables -w 5 -F DIRT_" in commands
    )


def test_engine_reads_helper_from_zip_package(tmp_path):
    import zipfile, subprocess, sys

    root = ROOT / "connection_monitoring"
    artifact = tmp_path / "runtime.pyz"
    with zipfile.ZipFile(artifact, "w") as z:
        for name in ("__init__.py", "dirt.py", "wan_fault.py"):
            z.write(root / name, "connection_monitoring/" + name)
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            'import sys;sys.path.insert(0,sys.argv[1]);from connection_monitoring.dirt import helper_source;assert "def restore" in helper_source()',
            str(artifact),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_timer_uses_configured_prefix(router_harness):
    m, log, _, _, invoke = router_harness
    invoke("arm")
    assert any("--unit=sample-drill-" + ("a" * 32) in c for c in log)
    assert any(
        c == ["systemctl", "is-active", "sample-drill-" + ("a" * 32) + ".timer"]
        for c in log
    )


def test_monitor_default_ssh_identity_and_router_explicit_identity(monkeypatch):
    import copy, subprocess

    m = load("dirt")
    m.SITES = copy.deepcopy(CONFIG["sites"])
    m.SITES["third-site"]["monitor"]["key_path"] = None
    calls = []

    def invoke(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "{}", "")

    monkeypatch.setattr(m.subprocess, "run", invoke)
    m.ssh("third-site", "monitor", "pass")
    m.ssh("third-site", "router", "pass")
    assert "-i" not in calls[0] and "IdentitiesOnly=yes" not in calls[0]
    assert "-i" in calls[1] and "IdentitiesOnly=yes" in calls[1]
    from connection_monitoring.wan_fault import validate_config

    assert validate_config({"version": 1, "sites": m.SITES})


def test_generated_router_source_probes_and_embeds_only_target_fault_config(
    monkeypatch,
):
    import sys

    m = load("dirt")
    source = m.router_source("third-site")
    namespace = {"__name__": "saved_router"}
    exec(compile(source, "router-source", "exec"), namespace)
    cfg = CONFIG["sites"]["third-site"]
    info = {
        w["interface"]: {
            "uuid": w["uuid"],
            "type": "wan",
            "ready": True,
            "active": name == cfg["primary"],
        }
        for name, w in cfg["wans"].items()
    }
    info["br0"] = {"type": "lan"}
    namespace["network_info"] = lambda: info
    namespace["management_interface"] = lambda: "br0"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "fault",
            "probe",
            "--site",
            "third-site",
            "--wan",
            "uplink-b",
            "--run",
            "a" * 32,
        ],
    )
    namespace["main"]()
    assert namespace["SITES"]["third-site"]["primary"] == "uplink-a"
    assert "router-key" not in source and "delivery.sqlite" not in source
    assert namespace["RECOVERY_SOURCE"] == m.helper_source()


@pytest.mark.parametrize("preflight", [True, False])
def test_operator_paths_send_executable_configured_router_source(
    monkeypatch, preflight
):
    import sys

    m = load("dirt")
    seen = []

    def fake(site, role, source, args=()):
        if role == "monitor":
            return {
                "internet": True,
                "collector_active": True,
                "alert": {"state": "inactive", "health": "ok"},
            }
        if args[0] != "probe":
            raise ValueError("Stop before real mutation")
        namespace = {"__name__": "router_helper"}
        exec(compile(source, "sent-router", "exec"), namespace)
        cfg = CONFIG["sites"][site]
        info = {
            w["interface"]: {
                "uuid": w["uuid"],
                "type": "wan",
                "ready": True,
                "active": name == cfg["primary"],
            }
            for name, w in cfg["wans"].items()
        }
        info["br0"] = {"type": "lan"}
        namespace["network_info"] = lambda: info
        namespace["management_interface"] = lambda: "br0"
        monkeypatch.setattr(sys, "argv", ["router", *args])
        namespace["main"]()
        seen.append(namespace["SITES"])
        return {"baseline": True}

    monkeypatch.setattr(m, "ssh", fake)
    if preflight:
        m.main(
            ["--site", "third-site", "--wan", "uplink-b", "--preflight"], config=CONFIG
        )
    else:
        assert not m.run("third-site", "uplink-b", "a" * 32, lambda *a: None, poll=0)
    assert seen and all(list(s) == ["third-site"] for s in seen)


@pytest.mark.parametrize(
    "role,missing", [("router", False), ("router", True), ("monitor", True)]
)
def test_router_requires_explicit_key_and_monitor_requires_key_declaration_before_ssh(
    monkeypatch, role, missing
):
    import copy

    m = load("dirt")
    cfg = copy.deepcopy(CONFIG)
    if missing:
        cfg["sites"]["third-site"][role].pop("key_path")
    else:
        cfg["sites"]["third-site"][role]["key_path"] = None
    monkeypatch.setattr(
        m, "ssh", lambda *args: pytest.fail("incomplete SSH config reached SSH")
    )
    with pytest.raises(SystemExit):
        m.main(["--site", "third-site", "--wan", "uplink-b", "--preflight"], config=cfg)


@pytest.mark.parametrize("value", [None, 0, ""])
def test_recovery_requires_exact_false_standby_active(value):
    m = load("dirt")
    states = {
        "uplink-a": {"ready": True, "active": True},
        "uplink-b": {"ready": True, "active": value},
    }
    assert not m.recovered(states, True, "uplink-a")
    states["uplink-b"].pop("active")
    assert not m.recovered(states, True, "uplink-a")
    states["uplink-b"]["active"] = False
    assert m.recovered(states, True, "uplink-a")


@pytest.mark.parametrize(
    "section,key,path",
    [
        ("observer", "token_env_file", "relative/secrets"),
        ("observer", "delivery_db", "/tmp/../delivery.sqlite"),
        ("observer", "delivery_db", "/tmp/bad\x00.sqlite"),
        ("observer", "token_env_file", "/tmp/bad\n.env"),
        (None, "state_dir", "/"),
        (None, "state_dir", "/run/../tmp/drill"),
        (None, "state_dir", "/run/drill\x00"),
    ],
)
def test_unsafe_observer_and_state_paths_rejected_before_ssh(
    monkeypatch, section, key, path
):
    import copy

    m = load("dirt")
    cfg = copy.deepcopy(CONFIG)
    target = cfg["sites"]["third-site"]
    if section:
        target = target[section]
    target[key] = path
    monkeypatch.setattr(m, "ssh", lambda *args: pytest.fail("unsafe path reached SSH"))
    with pytest.raises(SystemExit):
        m.main(["--site", "third-site", "--wan", "uplink-b", "--preflight"], config=cfg)


@pytest.mark.parametrize("rules", [[], [{"name": "Target"}, {"name": "Target"}]])
def test_alert_selection_rejects_missing_or_ambiguous_title(rules):
    m = load("dirt")
    with pytest.raises(ValueError, match="exactly one"):
        m.select_alert_rule({"data": {"groups": [{"rules": rules}]}}, "Target")


def test_alert_selection_preserves_only_matching_rule_state():
    m = load("dirt")
    rule = {
        "name": "Target",
        "state": "firing",
        "health": "ok",
        "lastEvaluation": "now",
        "duration": 300,
        "other": "omitted",
    }
    selected = m.select_alert_rule(
        {"data": {"groups": [{"rules": [{"name": "Different"}, rule]}]}}, "Target"
    )
    assert selected == {
        k: rule[k] for k in ("name", "state", "health", "lastEvaluation", "duration")
    }
    assert "def select_alert_rule" in m.OBSERVER


@pytest.mark.parametrize("failure", [HealthRejected("health unavailable"), ValueError("health unavailable"), RuntimeError("observer unavailable"), KeyboardInterrupt()])
def test_before_arm_failure_emits_failed_result_without_any_mutation(monkeypatch, failure):
    m = load("dirt")
    actions, events = [], []

    def ssh(site, role, source, args=()):
        if role == "monitor":
            return {"collector_active": True, "internet": True,
                    "alert": {"state": "inactive", "health": "ok"}}
        actions.append(args[0])
        return {"baseline": True}

    def guard():
        assert actions == ["probe"]
        assert events[-1][0] == "baseline"
        raise failure

    monkeypatch.setattr(m, "ssh", ssh)
    assert not m.run("third-site", "uplink-b", "a" * 32,
                     lambda stage, data: events.append((stage, data)), before_arm=guard)
    assert actions == ["probe"]
    assert events[-1] == ("result", {"passed": False, "checks": {
        "fault": False, "alert": False, "recovery": False, "cleanup": False}})
    assert events[-2][1]["kind"] == type(failure).__name__


def test_before_arm_runs_after_baseline_and_before_arm_and_inject(monkeypatch):
    m = load("dirt")
    order = []

    def ssh(site, role, source, args=()):
        if role == "monitor":
            order.append("observe")
            return {"collector_active": True, "internet": True,
                    "alert": {"state": "inactive", "health": "ok"}}
        order.append(args[0])
        if args[0] == "inject":
            raise ValueError("stop observation fixture")
        return {"cleanup": True}

    monkeypatch.setattr(m, "ssh", ssh)
    assert not m.run("third-site", "uplink-b", "a" * 32, lambda *args: None,
                     before_arm=lambda: order.append("guard"))
    assert order == ["probe", "observe", "guard", "arm", "inject", "restore"]


def test_unhealthy_baseline_does_not_call_before_arm(monkeypatch):
    m = load("dirt")
    monkeypatch.setattr(m, "ssh", lambda *args: {})
    assert not m.run("third-site", "uplink-b", "a" * 32, lambda *args: None,
                     before_arm=lambda: pytest.fail("guard preceded baseline acceptance"))


def test_before_inject_rejection_restores_armed_timer_without_inject(monkeypatch):
    from connection_monitoring import dirt
    actions=[]
    monkeypatch.setattr(dirt,'SITES',{'third-site':{'primary':'uplink-a'}})
    monkeypatch.setattr(dirt,'router_source',lambda site:'source')
    def ssh(site,kind,source,args):
        if kind=='monitor':return {'collector_active':True,'internet':True,'alert':{'state':'inactive','health':'ok'}}
        actions.append(args[0]);return {'cleanup':True}
    monkeypatch.setattr(dirt,'ssh',ssh)
    def reject():raise HealthRejected('expired frozen health')
    assert dirt.run('third-site','uplink-b','a'*32,lambda *args:None,before_inject=reject) is False
    assert actions==['probe','arm','restore']
