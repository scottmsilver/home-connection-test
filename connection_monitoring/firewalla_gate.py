#!/usr/bin/env python3

import argparse
import json
import math
import os
import sys
import subprocess
import time
import uuid
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

FIREAPI_URL = "http://127.0.0.1:8834/v1/encipher/simple"
_UUID_CHARS = set("0123456789abcdef-")


class Allowlist:
    """The exact commands this key may run, derived from deployed data.

    The UUIDs used to be literals in this file, which meant a second site
    needed a second copy of a security-sensitive script. They are data now.
    Everything about this class fails closed: an absent, unreadable, or
    malformed file yields no permitted command at all, because a gate that
    falls back to a default is a gate that opens when the file is corrupted.
    """

    def __init__(self, wan_uuids, quality_target):
        self.wan_uuids = tuple(wan_uuids)
        self.commands = {f"run-speedtest {u}": u for u in wan_uuids}
        self.quality_keys = frozenset(f"metric:monitor:raw:ping:{quality_target}:{u}" for u in wan_uuids)


EMPTY_ALLOWLIST = Allowlist([], "")


def load_allowlist(path=None):
    try:
        with open(path, "r") as handle:
            data = json.load(handle)
    except Exception:
        # Deliberately broad, not lazy: this is a forced-command SSH gate, and
        # every way a corrupt or hostile allowlist file can fail to parse
        # (bad encoding, deeply nested JSON raising RecursionError, whatever
        # the next weird one turns out to be) must yield "no permitted
        # command," not a traceback that dumps this script's and the
        # allowlist's paths to whoever holds the key. Do not narrow this back
        # to a tuple of exception types.
        return EMPTY_ALLOWLIST

    if not isinstance(data, dict):
        return EMPTY_ALLOWLIST
    wans = data.get("wans")
    target = data.get("quality_target")
    if not isinstance(wans, list) or not wans:
        return EMPTY_ALLOWLIST
    if not isinstance(target, str) or not target:
        return EMPTY_ALLOWLIST
    for uuid_value in wans:
        # Not a UUID parse: the point is that nothing reaches a redis key
        # pattern or a shell-adjacent string except lowercase hex and dashes.
        if not isinstance(uuid_value, str) or not uuid_value or not set(uuid_value) <= _UUID_CHARS:
            return EMPTY_ALLOWLIST
    return Allowlist(wans, target)


QUALITY_EXPORT_COMMAND = "export-network-quality"
QUALITY_STAT_FIELDS = ("min", "max", "median", "mean", "lossrate")
EXPORT_ARGV = [
    "/usr/bin/redis-cli",
    "--raw",
    "zrange",
    "internet_speedtest_results",
    "0",
    "-1",
]


class CommandRejected(Exception):
    pass


class CommandFailure(Exception):
    def __init__(self, message, exit_code=70):
        super().__init__(message)
        self.exit_code = exit_code


def trigger_speedtest(wan_uuid, opener=None, request_id_factory=None):
    if opener is None:
        opener = urlopen
    if request_id_factory is None:
        request_id_factory = lambda: str(uuid.uuid4())

    query = urlencode(
        {
            "command": "cmd",
            "item": "runInternetSpeedtest",
            "id": request_id_factory(),
        }
    )
    body = json.dumps({"wanUUID": wan_uuid, "vendor": "ookla"}, separators=(",", ":")).encode("utf-8")
    request = Request(
        f"{FIREAPI_URL}?{query}",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with opener(request, timeout=120) as response:
            raw_response = response.read()
    except (HTTPError, URLError, OSError, TypeError, HTTPException):
        raise CommandFailure("FireApi request failed", 69) from None

    try:
        envelope = json.loads(raw_response)
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
        raise CommandFailure("invalid FireApi response") from None

    if not isinstance(envelope, dict) or "code" not in envelope:
        raise CommandFailure("invalid FireApi response")
    if envelope["code"] != 200:
        raise CommandFailure("FireApi returned an error")

    try:
        result = envelope["data"]["result"]
    except (KeyError, TypeError):
        raise CommandFailure("invalid FireApi response") from None

    if not isinstance(result, dict):
        raise CommandFailure("invalid FireApi response")
    if not (result.get("success") is True and result.get("manual") is True and result.get("uuid") == wan_uuid):
        raise CommandFailure("speed test was not accepted")


def _is_number(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _is_numeric_timestamp(timestamp):
    if not isinstance(timestamp, str):
        return False
    try:
        return math.isfinite(float(timestamp))
    except (OverflowError, ValueError):
        return False


def sanitize_network_quality(data, allowed_keys):
    sanitized = {}
    for key, history in data.items():
        if not isinstance(key, str) or key not in allowed_keys:
            continue
        if not isinstance(history, dict):
            continue

        sanitized_history = {}
        for timestamp, record in history.items():
            if not _is_numeric_timestamp(timestamp) or not isinstance(record, dict):
                continue
            stat = record.get("stat")
            if not isinstance(stat, dict):
                continue
            sanitized_stat = {
                field: stat[field] for field in QUALITY_STAT_FIELDS if field in stat and _is_number(stat[field])
            }
            if sanitized_stat:
                sanitized_history[timestamp] = {"stat": sanitized_stat}
        if sanitized_history:
            sanitized[key] = sanitized_history
    return sanitized


def export_network_quality(allowed_keys, opener=None):
    if opener is None:
        opener = urlopen

    query = urlencode({"command": "get", "item": "networkMonitorData", "target": "0.0.0.0"})
    request = Request(f"{FIREAPI_URL}?{query}", method="GET")
    try:
        with opener(request, timeout=10) as response:
            raw_response = response.read()
    except (HTTPError, URLError, OSError, TypeError, HTTPException):
        raise CommandFailure("FireApi request failed", 69) from None

    try:
        envelope = json.loads(raw_response)
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
        raise CommandFailure("invalid FireApi response") from None

    if not (
        isinstance(envelope, dict)
        and isinstance(envelope.get("code"), int)
        and not isinstance(envelope["code"], bool)
        and envelope["code"] == 200
        and isinstance(envelope.get("data"), dict)
    ):
        raise CommandFailure("invalid FireApi response")

    print(
        json.dumps(
            {"code": 200, "data": sanitize_network_quality(envelope["data"], allowed_keys)},
            separators=(",", ":"),
        )
    )


def export_network_state(wan_uuids, now=time.time):
    try:
        result = subprocess.run(
            ['/usr/bin/redis-cli', '--raw', 'hgetall', 'sys:network:info'],
            capture_output=True, text=True, timeout=5, check=True,
        )
        rows = result.stdout.splitlines()
        if len(rows) % 2:
            raise ValueError('invalid hash')
        native = {}
        for value in rows[1::2]:
            try:
                state = json.loads(value)
            except ValueError:
                continue
            if not isinstance(state, dict) or state.get('uuid') not in wan_uuids:
                continue
            if state['uuid'] in native:
                raise ValueError('duplicate WAN identity')
            native[state['uuid']] = state
        selected = {}
        for wan in wan_uuids:
            state = native[wan]
            if state.get('type') != 'wan' or any(type(state.get(k)) is not bool for k in ('ready', 'active')):
                raise ValueError('invalid state')
            selected[wan] = {k: state[k] for k in ('ready', 'active')}
        collected = int(now())
        if not selected or collected < 0:
            raise ValueError('invalid collection')
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError, AttributeError):
        raise CommandFailure('Native WAN state unavailable', 69) from None
    print(json.dumps({'time': collected, 'data': selected}, separators=(',', ':')))


def dispatch(original_command, opener=None, execv=None, request_id_factory=None, allowlist_path=None):
    if execv is None:
        execv = os.execv

    if original_command == "":
        # No allowlist needed: the read-only redis export takes no argument and
        # exposes nothing site-specific, so a bad allowlist file must not take
        # the speed importer down with it.
        execv(EXPORT_ARGV[0], EXPORT_ARGV)
        return

    allowlist = load_allowlist(allowlist_path)

    if original_command == 'export-network-state':
        if not allowlist.wan_uuids:
            raise CommandRejected('rejected command')
        export_network_state(allowlist.wan_uuids)
        return

    if original_command == QUALITY_EXPORT_COMMAND:
        if not allowlist.quality_keys:
            raise CommandRejected("rejected command")
        export_network_quality(allowlist.quality_keys, opener=opener)
        return

    wan_uuid = allowlist.commands.get(original_command)
    if wan_uuid is None:
        raise CommandRejected("rejected command")
    trigger_speedtest(wan_uuid, opener=opener, request_id_factory=request_id_factory)


class _FixedErrorParser(argparse.ArgumentParser):
    def error(self, unused_message):
        raise CommandRejected("rejected command")


def main(argv=None):
    try:
        parser = _FixedErrorParser(add_help=False)
        parser.add_argument("--allowlist", required=True)
        args = parser.parse_args(argv)
        dispatch(os.environ.get("SSH_ORIGINAL_COMMAND", ""), allowlist_path=args.allowlist)
    except CommandRejected as error:
        print(str(error), file=sys.stderr)
        return 64
    except CommandFailure as error:
        print(str(error), file=sys.stderr)
        return error.exit_code
    except Exception:
        # Deliberately broad, not lazy: this is a forced-command SSH gate,
        # and its whole contract is that a key holder gets exactly one of a
        # few fixed effects and learns nothing else. An uncaught exception
        # here — a missing execv target, a RecursionError from a
        # pathologically nested FireApi response, whatever the next weird
        # one turns out to be — must not print a traceback: that leaks this
        # script's path, line numbers, and local values to whoever holds the
        # key. Do not narrow this back to a tuple of exception types, and do
        # not print the exception itself.
        print("internal error", file=sys.stderr)
        return 71
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
