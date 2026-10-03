#!/usr/bin/env python3
"""DIRT Internet recovery suite. Preview by default; faults require --execute."""

import argparse, hashlib, inspect, json, os, secrets, shlex, signal, subprocess, sys, time
import urllib.request, urllib.error
from pathlib import Path

SITES = {}


def helper_source():
    import importlib.resources

    if hasattr(importlib.resources, "files"):
        return (
            importlib.resources.files("connection_monitoring")
            .joinpath("wan_fault.py")
            .read_text(encoding="utf-8")
        )
    return importlib.resources.read_text(
        "connection_monitoring", "wan_fault.py", encoding="utf-8"
    )


def router_source(site):
    config = {
        site: {
            key: SITES[site][key]
            for key in (
                "primary",
                "wans",
                "management_interface",
                "state_dir",
                "timer_prefix",
            )
        }
    }
    source = helper_source()
    return (
        "EMBEDDED_SITE = "
        + repr(config)
        + "\nRECOVERY_SOURCE = "
        + repr(source)
        + "\n"
        + source
    )


def fault_observed(states, wan, internet, primary):
    backup = next((name for name in states if name != wan), None)
    if not backup or not internet:
        return False
    return (
        states[wan].get("ready") is False
        and states[wan].get("active") is False
        and states[backup].get("ready") is True
        and states[backup].get("active") is True
    )


def recovered(states, internet, primary):
    return (
        internet
        and len(states) == 2
        and primary in states
        and all(v.get("ready") is True for v in states.values())
        and states[primary].get("active") is True
        and all(v.get("active") is False for k, v in states.items() if k != primary)
    )


def verdict(checks):
    return all(checks.get(k) is True for k in ("fault", "alert", "recovery", "cleanup"))


def ssh(site, role, source, args=()):
    cfg = SITES[site][role]
    cmd = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=8",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "UserKnownHostsFile=" + cfg["known_hosts"],
    ]
    if cfg["key_path"] is not None:
        cmd += ["-i", os.path.expanduser(cfg["key_path"]), "-o", "IdentitiesOnly=yes"]
    cmd.append(cfg["user"] + "@" + cfg["host"])
    remote = ["/usr/bin/python3", "-", *args]
    if role == "router":
        remote = ["sudo", "--preserve-env=SSH_CONNECTION", *remote]
    r = subprocess.run(
        [*cmd, shlex.join(remote)],
        input=source,
        text=True,
        capture_output=True,
        timeout=45,
    )
    if r.returncode:
        raise ValueError(role + " operation failed")
    try:
        return json.loads(r.stdout)
    except ValueError:
        raise ValueError("Invalid " + role + " response") from None


def subject_keys(site, wan):
    alert = SITES[site]["alerts"][wan]
    labels = dict(alert["labels"])
    labels["alertname"] = alert["title"]
    keys = []
    for include_uid in (False, True):
        if include_uid:
            labels["__alert_rule_uid__"] = alert["uid"]
        keys.append(
            hashlib.sha256(
                json.dumps(labels, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
        )
    return keys


def read_rules(token, opener=None, sleep=time.sleep, port=3000):
    opener = opener or urllib.request.urlopen
    req = urllib.request.Request(
        "http://127.0.0.1:" + str(port) + "/api/prometheus/grafana/api/v1/rules",
        headers={"Authorization": "Bearer " + token},
    )
    for attempt in range(4):
        try:
            with opener(req, timeout=5) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code not in (429, 500, 502, 503, 504):
                raise
        except (urllib.error.URLError, OSError):
            pass
        if attempt < 3:
            sleep(2)
    raise ValueError("Grafana observation unavailable")


def select_alert_rule(data, title):
    matches = [
        rule
        for group in data["data"]["groups"]
        for rule in group["rules"]
        if rule.get("name") == title
    ]
    if len(matches) != 1:
        raise ValueError("Expected exactly one configured alert rule")
    return {
        key: matches[0].get(key)
        for key in ("name", "state", "health", "lastEvaluation", "duration")
    }


OBSERVER = (
    "import json,hashlib\n"
    + inspect.getsource(subject_keys)
    + "\nimport time,urllib.request,urllib.error\n"
    + inspect.getsource(read_rules)
    + "\n"
    + inspect.getsource(select_alert_rule)
    + """\nimport json,sys,socket,sqlite3,hashlib,urllib.request
import urllib.request,urllib.error
from pathlib import Path
site,wan=sys.argv[1:3]
cfg=SITES[site];observer=cfg['observer']
import subprocess
result={'internet':False,'collector_active':subprocess.run(['systemctl','is-active',observer['collector_unit']],capture_output=True).returncode==0}
try:
 with urllib.request.urlopen(observer['probe_url'],timeout=8) as r:
  result['internet']=r.status==200 and observer['body_marker'].encode() in r.read(8192)
except Exception:pass
key=None
for line in Path(observer['token_env_file']).read_text().splitlines():
 if line.startswith(observer['token_env_name']+'='):key=line.split('=',1)[1].strip().strip(chr(34)+chr(39))
if not key:raise ValueError('Observer token unavailable')
data=read_rules(key,port=observer['grafana_port'])
result['alert']=select_alert_rule(data,cfg['alerts'][wan]['title'])
db=sqlite3.connect('file:'+observer['delivery_db']+'?mode=ro',uri=True)
keys=subject_keys(site,wan)
row=db.execute('SELECT status,accepted FROM history WHERE subject IN (?,?) ORDER BY accepted DESC LIMIT 1',keys).fetchone()
result['delivery_history']={'status':row[0],'accepted':row[1]} if row else None
print(json.dumps(result))
"""
)


def run(site, wan, run_id, emit, poll=15):
    source = router_source(site)

    def router(action):
        return ssh(
            site,
            "router",
            source,
            (action, "--site", site, "--wan", wan, "--run", run_id, "--ttl", "900"),
        )

    def observe():
        return ssh(
            site,
            "monitor",
            "SITES = " + repr({site: SITES[site]}) + "\n" + OBSERVER,
            (site, wan),
        )

    failed = False
    checks = {"fault": False, "alert": False, "recovery": False, "cleanup": False}
    armed = False
    started = time.time()
    primary = SITES[site]["primary"]
    try:
        baseline = router("probe")
        ob = observe()
        emit("baseline", {"router": baseline, "observation": ob})
        if (
            not ob.get("collector_active")
            or not ob.get("internet")
            or ob.get("alert", {}).get("state") != "inactive"
            or ob.get("alert", {}).get("health") != "ok"
        ):
            raise ValueError("Unhealthy baseline or target alert already active")
        # The local timer is armed and verified before any packet block is added.
        armed = True
        emit("armed", router("arm"))
        emit("injected", router("inject"))
        deadline = time.monotonic() + 780
        while time.monotonic() < deadline:
            state = router("status")
            ob = observe()
            emit("fault_observation", {"router": state, "observation": ob})
            if not state.get("fault_present"):
                raise ValueError("Fault disappeared before test completion")
            healthy = fault_observed(state["wans"], wan, ob.get("internet"), primary)
            if checks["fault"] and not ob.get("internet"):
                raise ValueError("LAN Internet failed after failover")
            checks["fault"] = checks["fault"] or healthy
            delivery = ob.get("delivery_history") or {}
            checks["alert"] = checks["alert"] or (
                ob.get("alert", {}).get("state") == "firing"
                and delivery.get("status") == "firing"
                and delivery.get("accepted", 0) >= started
            )
            if healthy and checks["alert"]:
                break
            time.sleep(poll)
        emit("restored", router("restore"))
        checks["cleanup"] = True
        deadline = time.monotonic() + 900
        while time.monotonic() < deadline:
            state = router("status")
            ob = observe()
            emit("recovery_observation", {"router": state, "observation": ob})
            delivery = ob.get("delivery_history") or {}
            recovery = (
                recovered(state["wans"], ob.get("internet"), primary)
                and ob.get("alert", {}).get("state") == "inactive"
                and delivery.get("status") == "resolved"
                and delivery.get("accepted", 0) >= started
            )
            if recovery:
                checks["recovery"] = True
                break
            time.sleep(poll)
    except (Exception, KeyboardInterrupt) as exc:
        failed = True
        emit(
            "error",
            {
                "kind": type(exc).__name__,
                "reason": (
                    str(exc)
                    if isinstance(exc, ValueError)
                    else "Run interrupted or observation failed"
                ),
            },
        )
    finally:
        if armed:
            try:
                checks["cleanup"] = router("restore").get("cleanup") is True
                emit("cleanup", {"complete": checks["cleanup"]})
            except Exception:
                checks["cleanup"] = False
                emit(
                    "cleanup",
                    {
                        "complete": False,
                        "independent_recovery_state": "unverified; inspect router timer and owned rules",
                    },
                )
        emit("result", {"passed": not failed and verdict(checks), "checks": checks})
    return not failed and verdict(checks)


def main(argv=None, config=None):
    global SITES
    from connection_monitoring.wan_fault import validate_config
    from connection_monitoring.config import load_config

    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config")
    p.add_argument("--site", required=True)
    target = p.add_mutually_exclusive_group(required=True)
    target.add_argument("--wan")
    target.add_argument("--suite", action="store_true")
    p.add_argument("--execute", action="store_true")
    p.add_argument("--preflight", action="store_true")
    p.add_argument("--output", type=Path)
    a = p.parse_args(argv)
    try:
        SITES = validate_config(
            config if config is not None else load_config(a.config)
        )["sites"]
    except (ValueError, TypeError) as exc:
        p.error(str(exc))
    if a.site not in SITES:
        p.error("Unknown site")
    if a.execute and a.preflight:
        p.error("--preflight and --execute are mutually exclusive")
    if a.wan and a.wan not in SITES[a.site]["wans"]:
        p.error("Choose exactly one declared WAN")
    primary = SITES[a.site]["primary"]
    cases = (
        [next(w for w in SITES[a.site]["wans"] if w != primary), primary]
        if a.suite
        else [a.wan]
    )
    plan = {
        "site": a.site,
        "cases": cases,
        "primary": primary,
        "fault": "runtime IPv4/IPv6 single-WAN packet block",
        "independent_recovery_seconds": 900,
        "scope": "LAN probe traffic, router state and matching real alert delivery; graph/model wording not asserted",
        "order": "Each case must fully recover before the next begins; stop on failure",
    }
    print(json.dumps(plan), flush=True)
    if not a.execute and not a.preflight:
        return
    if a.preflight:
        source = router_source(a.site)
        for wan in cases:
            run_id = secrets.token_hex(16)
            print(
                json.dumps(
                    ssh(
                        a.site,
                        "router",
                        source,
                        ("probe", "--site", a.site, "--wan", wan, "--run", run_id),
                    )
                )
            )
            print(
                json.dumps(
                    ssh(
                        a.site,
                        "monitor",
                        "SITES = " + repr({a.site: SITES[a.site]}) + "\n" + OBSERVER,
                        (a.site, wan),
                    )
                )
            )
        return
    if not a.output:
        p.error("--execute requires --output for evidence")
    fd = os.open(a.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    passed = False
    with os.fdopen(fd, "w") as out:
        signal.signal(
            signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt())
        )
        for wan in cases:
            run_id = secrets.token_hex(16)

            def emit(stage, data):
                record = {
                    "time": time.time(),
                    "run": run_id,
                    "site": a.site,
                    "wan": wan,
                    "stage": stage,
                    "data": data,
                }
                line = json.dumps(record)
                out.write(line + "\n")
                out.flush()
                os.fsync(out.fileno())
                print(line, flush=True)

            passed = run(a.site, wan, run_id, emit)
            if not passed:
                break
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
