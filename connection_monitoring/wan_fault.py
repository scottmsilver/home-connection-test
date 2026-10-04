#!/usr/bin/env python3
"""Router-local, runtime-only single-WAN fault with independent restoration."""

import argparse, fcntl, ipaddress, json, os, re, shlex, subprocess, sys, time
from pathlib import Path

SITES = {}
STATE = None


def safe_absolute_path(value):
    return (
        isinstance(value, str)
        and value.startswith("/")
        and value != "/"
        and not any(ord(char) < 32 or ord(char) == 127 for char in value)
        and ".." not in value.split("/")
    )


def validate_config(config):
    if (
        not isinstance(config, dict)
        or type(config.get("version")) is not int
        or config["version"] != 1
        or not isinstance(config.get("sites"), dict)
        or not config["sites"]
    ):
        raise ValueError("DIRT requires version 1 and sites")
    for site, cfg in config["sites"].items():
        if (
            not isinstance(site, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]+", site)
            or not isinstance(cfg, dict)
        ):
            raise ValueError("Invalid site")
        for key in ("primary", "management_interface", "state_dir", "timer_prefix"):
            if not isinstance(cfg.get(key), str) or not cfg[key]:
                raise ValueError("Missing fault configuration: " + key)
        if not safe_absolute_path(cfg["state_dir"]) or not re.fullmatch(
            r"[A-Za-z0-9_-]+", cfg["timer_prefix"]
        ):
            raise ValueError("Invalid recovery paths")
        wans = cfg.get("wans")
        if not isinstance(wans, dict) or len(wans) != 2 or cfg["primary"] not in wans:
            raise ValueError("Exactly two WANs with declared primary required")
        interfaces = []
        uuids = []
        for name, w in wans.items():
            if (
                not isinstance(name, str)
                or not re.fullmatch(r"[A-Za-z0-9_-]+", name)
                or not isinstance(w, dict)
            ):
                raise ValueError("Invalid WAN")
            interface = w.get("interface")
            uid = w.get("uuid")
            if (
                not isinstance(interface, str)
                or not re.fullmatch(r"[A-Za-z0-9_.:-]+", interface)
                or interface == cfg["management_interface"]
            ):
                raise ValueError("Invalid WAN interface")
            import uuid

            try:
                uuid.UUID(uid)
            except (ValueError, TypeError, AttributeError):
                raise ValueError("Invalid WAN UUID")
            interfaces.append(interface)
            uuids.append(uid)
        if len(set(interfaces)) != 2 or len(set(uuids)) != 2:
            raise ValueError("WAN identities must be unique")
        for role in ("router", "monitor"):
            target = cfg.get(role)
            if not isinstance(target, dict):
                raise ValueError("Missing SSH target")
            for key in ("host", "user", "known_hosts", "key_path"):
                if (
                    role == "monitor"
                    and key == "key_path"
                    and key in target
                    and target[key] is None
                ):
                    continue
                value = target.get(key)
                if (
                    not isinstance(value, str)
                    or not value
                    or any(c in value for c in ("\n", "\r", "\x00"))
                ):
                    raise ValueError("Missing SSH configuration: " + key)
            if target["host"].startswith("-") or not re.fullmatch(
                r"[A-Za-z0-9_.-]+", target["user"]
            ):
                raise ValueError("Invalid SSH target")
            if not all(
                os.path.isabs(os.path.expanduser(target[k]))
                for k in ("known_hosts", "key_path")
                if target[k] is not None
            ):
                raise ValueError("SSH paths must be absolute")
        ob = cfg.get("observer")
        if not isinstance(ob, dict):
            raise ValueError("Missing observer")
        for key in (
            "collector_unit",
            "probe_url",
            "body_marker",
            "token_env_file",
            "token_env_name",
            "delivery_db",
        ):
            if not isinstance(ob.get(key), str) or not ob[key]:
                raise ValueError("Missing observer configuration: " + key)
        if (
            type(ob.get("grafana_port")) is not int
            or not 1 <= ob["grafana_port"] <= 65535
        ):
            raise ValueError("Invalid Grafana port")
        if not ob["probe_url"].startswith("https://") or not re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_]*", ob["token_env_name"]
        ):
            raise ValueError("Invalid observer configuration")
        if not all(
            safe_absolute_path(ob[key]) for key in ("token_env_file", "delivery_db")
        ):
            raise ValueError("Observer paths must be safe absolute paths")
        alerts = cfg.get("alerts")
        if not isinstance(alerts, dict) or set(alerts) != set(wans):
            raise ValueError("Missing WAN alert contracts")
        for name, alert in alerts.items():
            if (
                not isinstance(alert, dict)
                or not isinstance(alert.get("labels"), dict)
                or not alert["labels"]
                or not all(
                    isinstance(k, str) and isinstance(v, str) and v
                    for k, v in alert["labels"].items()
                )
            ):
                raise ValueError("Invalid alert labels")
            if not all(
                isinstance(alert.get(k), str) and alert[k] for k in ("title", "uid")
            ):
                raise ValueError("Missing alert title or UID")
    return config


def command(args, check=True):
    r = subprocess.run(args, capture_output=True, text=True, timeout=15)
    if check and r.returncode:
        raise ValueError("Command failed: " + args[0])
    return r


def validate(wan, run, ttl, site=None):
    if (
        site not in SITES
        or wan not in SITES[site]["wans"]
        or not re.fullmatch("[a-f0-9]{32}", run)
        or not 60 <= ttl <= 1200
    ):
        raise ValueError("Invalid single-WAN drill parameters")


def chain(run):
    return "DIRT_" + run[:20]


def jumps(interface, name, operation):
    return [
        [operation, "INPUT", "-i", interface, "-j", name],
        [operation, "OUTPUT", "-o", interface, "-j", name],
        [operation, "FORWARD", "-i", interface, "-j", name],
        [operation, "FORWARD", "-o", interface, "-j", name],
    ]


def fault_commands(interface, run):
    name = chain(run)
    result = []
    for binary in ("iptables", "ip6tables"):
        result += [
            [binary, "-w", "5", "-N", name],
            [binary, "-w", "5", "-A", name, "-j", "DROP"],
        ]
        result += [
            [binary, "-w", "5", *jump[:2], "1", *jump[2:]]
            for jump in jumps(interface, name, "-I")
        ]
    return result


def restore_commands(interface, run):
    name = chain(run)
    result = []
    for binary in ("iptables", "ip6tables"):
        result += [[binary, "-w", "5", *jump] for jump in jumps(interface, name, "-D")]
        result += [[binary, "-w", "5", "-F", name], [binary, "-w", "5", "-X", name]]
    return result


def firewall_rules(binary):
    r = command([binary, "-w", "5", "-S"], False)
    if r.returncode:
        raise ValueError("Firewall inspection failed; recovery remains required")
    return [shlex.split(line) for line in r.stdout.splitlines() if line.strip()]


def firewall_present(interface, run):
    name = chain(run)
    for binary in ("iptables", "ip6tables"):
        rules = firewall_rules(binary)
        if ["-N", name] not in rules or ["-A", name, "-j", "DROP"] not in rules:
            return False
        if not all(["-A", *j[1:]] in rules for j in jumps(interface, name, "-C")):
            return False
    return True


def firewall_clean(interface, run):
    name = chain(run)
    for binary in ("iptables", "ip6tables"):
        if any(name in rule for rule in firewall_rules(binary)):
            return False
    return True


def network_info():
    rows = command(
        ["redis-cli", "--raw", "hgetall", "sys:network:info"]
    ).stdout.splitlines()
    result = {}
    for key, value in zip(rows[::2], rows[1::2]):
        try:
            data = json.loads(value)
        except ValueError:
            continue
        if isinstance(data, dict):
            result[key] = data
    return result


def check_baseline(info, wan, management_interface, site=None):
    cfg = SITES[site]
    wans = cfg["wans"]
    if (
        management_interface != cfg["management_interface"]
        or info.get(management_interface, {}).get("type") != "lan"
    ):
        raise ValueError("Management route must use declared LAN")
    for name, declared in wans.items():
        live = info.get(declared["interface"], {})
        if (
            live.get("uuid") != declared["uuid"]
            or live.get("type") != "wan"
            or live.get("ready") is not True
        ):
            raise ValueError("WAN identity drift or unhealthy connection")
        if type(live.get("active")) is not bool or live["active"] != (
            name == cfg["primary"]
        ):
            raise ValueError("Unexpected primary/standby roles")


def management_interface():
    peer = os.environ.get("SSH_CONNECTION", "").split()
    if not peer:
        raise ValueError("SSH management peer unavailable")
    ipaddress.ip_address(peer[0])
    routes = json.loads(command(["ip", "-j", "route", "get", peer[0]]).stdout)
    return routes[0].get("dev")


def snapshot(site):
    info = network_info()
    return {
        name: {
            key: info.get(w["interface"], {}).get(key)
            for key in ("uuid", "type", "ready", "active")
        }
        for name, w in SITES[site]["wans"].items()
    }


def recovery_argv(site, wan, run, ttl):
    return ["/usr/bin/python3", str(STATE / (run + ".py")), "restore",
            "--site", site, "--wan", wan, "--run", run, "--ttl", str(ttl)]


def recovery_source(site):
    source = globals().get("RECOVERY_SOURCE")
    if not source:
        raise ValueError("Recovery helper source unavailable")
    embedded = {site: {k: SITES[site][k] for k in
        ("primary", "wans", "management_interface", "state_dir", "timer_prefix")}}
    return "EMBEDDED_SITE = " + repr(embedded) + "\n" + source


def timer_property(unit, interface, name):
    # D-Bus JSON preserves integer microseconds and the actual ExecStart argv;
    # human-readable systemctl output is not a stable timer proof.
    path = "/org/freedesktop/systemd1/unit/" + "".join(
        c if c.isascii() and c.isalnum() else "_" + format(ord(c), "02x") for c in unit)
    raw = command(["busctl", "--json=short", "get-property", "org.freedesktop.systemd1",
                   path, "org.freedesktop.systemd1." + interface, name]).stdout
    if len(raw) > 65536:
        raise ValueError("Recovery timer proof too large")
    value = json.loads(raw)
    return value["data"]


def verify_timer(site, wan, run, ttl):
    timer = SITES[site]["timer_prefix"] + run + ".timer"
    service = SITES[site]["timer_prefix"] + run + ".service"
    try:
        helper = STATE / (run + ".py")
        expected = recovery_source(site)
        if not helper.is_file() or helper.stat().st_size != len(expected.encode()) or helper.read_text() != expected:
            raise ValueError()
        if (timer_property(timer, "Unit", "ActiveState") != "active" or
                timer_property(timer, "Timer", "Unit") != service):
            raise ValueError()
        timers = timer_property(timer, "Timer", "TimersMonotonic")
        if (type(timers) is not list or len(timers) != 1 or len(timers[0]) != 3 or
                timers[0][0] != "OnActiveUSec" or type(timers[0][1]) is not int or
                timers[0][1] != ttl * 1000000 or type(timers[0][2]) is not int or
                not time.monotonic() * 1000000 < timers[0][2] <= (time.monotonic() + ttl + 2) * 1000000):
            raise ValueError()
        commands = timer_property(service, "Service", "ExecStart")
        if (type(commands) is not list or len(commands) != 1 or len(commands[0]) != 10 or
                commands[0][:3] != ["/usr/bin/python3", recovery_argv(site, wan, run, ttl), False]):
            raise ValueError()
    except (KeyError, IndexError, TypeError, ValueError, OSError):
        raise ValueError("Owned recovery timer could not be verified") from None


def restore(site, wan, run):
    interface = SITES[site]["wans"][wan]["interface"]
    for args in restore_commands(interface, run):
        command(args, False)
    clean = firewall_clean(interface, run)
    if clean:
        marker = STATE / "active.json"
        helper = STATE / (run + ".py")
        saved_helper = helper.read_bytes() if helper.exists() else None
        saved_marker = None
        if marker.exists() and json.loads(marker.read_text()).get("run") == run:
            saved_marker = marker.read_bytes()
        try:
            if saved_marker is not None:
                marker.unlink()
            if saved_helper is not None:
                helper.unlink()
            stopped = command(
                ["systemctl", "stop", SITES[site]["timer_prefix"] + run + ".timer"], False
            )
            if stopped.returncode:
                raise ValueError("Recovery timer cancellation uncertain")
        except BaseException:
            # Never re-arm or extend the timer. Preserve its original standalone
            # recovery input if cancellation failed, without replacing any file.
            for path, data in ((helper, saved_helper), (marker, saved_marker)):
                if data is not None:
                    try:
                        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                        with os.fdopen(fd, "wb") as stream:
                            stream.write(data)
                            stream.flush()
                            os.fsync(stream.fileno())
                    except FileExistsError:
                        pass
            raise
    if not clean:
        raise ValueError("Owned firewall rules could not all be removed")
    return {"cleanup": True}


def main(config=None):
    global SITES, STATE
    if config is not None:
        SITES = validate_config(config)["sites"]
    elif globals().get("EMBEDDED_SITE"):
        SITES = EMBEDDED_SITE

    p = argparse.ArgumentParser()
    p.add_argument("action", choices=("probe", "arm", "inject", "status", "restore"))
    p.add_argument("--site", choices=SITES, required=True)
    p.add_argument("--wan", required=True)
    p.add_argument("--run", required=True)
    p.add_argument("--ttl", type=int, default=900)
    a = p.parse_args()
    validate(a.wan, a.run, a.ttl, a.site)
    STATE = Path(SITES[a.site]["state_dir"])
    if a.action == "probe":
        check_baseline(network_info(), a.wan, management_interface(), a.site)
        print(json.dumps({"baseline": True, "wans": snapshot(a.site)}))
        return
    if os.geteuid() != 0:
        raise ValueError("Router-local root required")
    STATE.mkdir(mode=0o700, exist_ok=True)
    # flock works on a read-only descriptor on the router's local filesystem.
    # Recovery must not need a writable mount merely to lock existing state.
    lock_path = STATE / "lock"
    try:
        lock = lock_path.open("r" if a.action == "restore" else "a")
    except FileNotFoundError:
        lock = lock_path.open("a")
    with lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        interface = SITES[a.site]["wans"][a.wan]["interface"]
        marker = STATE / "active.json"
        if a.action == "restore":
            result = restore(a.site, a.wan, a.run)
        elif a.action == "status":
            result = {
                "fault_present": firewall_present(interface, a.run),
                "cleanup": firewall_clean(interface, a.run),
                "wans": snapshot(a.site),
            }
        elif a.action == "arm":
            check_baseline(network_info(), a.wan, management_interface(), a.site)
            if marker.exists():
                raise ValueError("Another DIRT run needs cleanup")
            for binary in ("iptables", "ip6tables"):
                if any(chain(a.run) in rule for rule in firewall_rules(binary)):
                    raise ValueError("Owned chain collision")
            helper = STATE / (a.run + ".py")
            saved = recovery_source(a.site)
            fd = os.open(str(helper), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as out:
                out.write(saved)
                out.flush()
                os.fsync(out.fileno())
            command(
                [
                    "systemd-run",
                    "--unit=" + SITES[a.site]["timer_prefix"] + a.run,
                    "--on-active=" + str(a.ttl) + "s",
                    "--timer-property=AccuracySec=1s",
                    "--property=TimeoutStartSec=120s",
                    "--property=Restart=on-failure",
                    "--property=RestartSec=5s",
                    "/usr/bin/python3",
                    str(helper),
                    "restore",
                    "--site",
                    a.site,
                    "--wan",
                    a.wan,
                    "--run",
                    a.run,
                    "--ttl",
                    str(a.ttl),
                ]
            )
            command(
                [
                    "systemctl",
                    "is-active",
                    SITES[a.site]["timer_prefix"] + a.run + ".timer",
                ]
            )
            verify_timer(a.site, a.wan, a.run, a.ttl)
            marker.write_text(
                json.dumps(
                    {
                        "run": a.run,
                        "site": a.site,
                        "wan": a.wan,
                        "armed_at": time.time(),
                        "ttl": a.ttl,
                    }
                )
            )
            marker.chmod(0o600)
            result = {"armed": True, "ttl": a.ttl}
        else:
            if not marker.exists():
                raise ValueError("No armed recovery")
            saved = json.loads(marker.read_text())
            if (saved["run"], saved["site"], saved["wan"]) != (
                a.run,
                a.site,
                a.wan,
            ) or saved.get("ttl") != a.ttl or not 0 <= time.time() - saved["armed_at"] <= 30:
                raise ValueError("Recovery arm expired or mismatched")
            command(
                [
                    "systemctl",
                    "is-active",
                    SITES[a.site]["timer_prefix"] + a.run + ".timer",
                ]
            )
            verify_timer(a.site, a.wan, a.run, a.ttl)
            check_baseline(network_info(), a.wan, management_interface(), a.site)
            try:
                for args in fault_commands(interface, a.run):
                    command(args)
                if not firewall_present(interface, a.run):
                    raise ValueError("Fault not established")
            except BaseException:
                restore(a.site, a.wan, a.run)
                raise
            result = {"injected": True, "interface": interface}
        print(json.dumps(result))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(
            json.dumps(
                {
                    "error": (
                        str(exc) if isinstance(exc, ValueError) else type(exc).__name__
                    )
                }
            )
        )
        sys.exit(1)
