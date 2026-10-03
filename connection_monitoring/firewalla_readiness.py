#!/usr/bin/env python3

import argparse
import json
import math
import os
import re
import subprocess
import sys
import time


EXPECTED_KEY_RE = re.compile(
    r"metric:monitor:raw:ping:[^:]+:[0-9a-f]{8}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)
STAT_FIELDS = frozenset(("min", "max", "median", "mean", "lossrate"))
DEFAULT_DEADLINE_SECONDS = 415
DEFAULT_POLL_INTERVAL = 5
EXPORT_TIMEOUT_SECONDS = 10


def _is_number(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _valid_expected_keys(expected_keys):
    return (
        isinstance(expected_keys, list)
        and bool(expected_keys)
        and all(isinstance(key, str) for key in expected_keys)
        and len(expected_keys) == len(set(expected_keys))
        and all(EXPECTED_KEY_RE.fullmatch(key) for key in expected_keys)
    )


def readiness_cutoff(reference_epoch, policy_changed, freshness_window=600):
    if not _is_number(reference_epoch):
        raise ValueError("reference epoch must be finite")
    if not isinstance(policy_changed, bool):
        raise ValueError("policy changed flag must be boolean")
    if not _is_number(freshness_window) or freshness_window < 0:
        raise ValueError("freshness window must be nonnegative")
    return reference_epoch + 1 if policy_changed else reference_epoch - freshness_window


def export_is_ready(raw, expected_keys, freshness_cutoff):
    if not _valid_expected_keys(expected_keys) or not _is_number(freshness_cutoff):
        return False
    try:
        envelope = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return False
    if not (
        isinstance(envelope, dict)
        and envelope.get("code") == 200
        and isinstance(envelope.get("data"), dict)
    ):
        return False

    data = envelope["data"]
    for key in expected_keys:
        history = data.get(key)
        if not isinstance(history, dict) or not history:
            return False
        has_fresh_record = False
        for timestamp, record in history.items():
            if not isinstance(timestamp, str) or not isinstance(record, dict):
                return False
            try:
                parsed_timestamp = float(timestamp)
            except (OverflowError, ValueError):
                return False
            stat = record.get("stat")
            if not (
                math.isfinite(parsed_timestamp)
                and set(record) == {"stat"}
                and isinstance(stat, dict)
                and bool(stat)
                and set(stat) <= STAT_FIELDS
                and all(_is_number(value) for value in stat.values())
            ):
                return False
            has_fresh_record = has_fresh_record or parsed_timestamp >= freshness_cutoff
        if not has_fresh_record:
            return False
    return True


def wait_for_quality(
    expected_keys,
    freshness_cutoff,
    *,
    gate_path,
    runner=None,
    clock=None,
    sleeper=None,
    deadline_seconds=DEFAULT_DEADLINE_SECONDS,
    poll_interval=DEFAULT_POLL_INTERVAL,
):
    if runner is None:
        runner = subprocess.run
    if clock is None:
        clock = time.monotonic
    if sleeper is None:
        sleeper = time.sleep
    if not _is_number(deadline_seconds) or deadline_seconds <= 0:
        raise ValueError("deadline must be positive")
    if not _is_number(poll_interval) or poll_interval < 0:
        raise ValueError("poll interval must be nonnegative")

    environment = os.environ.copy()
    environment.update({"SSH_ORIGINAL_COMMAND": "export-network-quality"})
    deadline = clock() + deadline_seconds
    while True:
        remaining = deadline - clock()
        if remaining <= 0:
            return False
        try:
            result = runner(
                [str(gate_path)],
                capture_output=True,
                check=False,
                env=environment,
                text=True,
                timeout=min(EXPORT_TIMEOUT_SECONDS, remaining),
            )
        except (OSError, subprocess.SubprocessError):
            result = None

        if clock() >= deadline:
            return False
        if (
            result is not None
            and result.returncode == 0
            and export_is_ready(result.stdout, expected_keys, freshness_cutoff)
        ):
            return True

        remaining = deadline - clock()
        if remaining <= 0:
            return False
        sleeper(min(poll_interval, remaining))


def _parse_expected_keys(value):
    if not value.startswith("json:"):
        raise ValueError
    try:
        expected_keys = json.loads(value[len("json:"): ])
    except json.JSONDecodeError:
        raise ValueError from None
    if not _valid_expected_keys(expected_keys):
        raise ValueError
    return expected_keys


class _FixedErrorParser(argparse.ArgumentParser):
    def error(self, unused_message):
        raise ValueError


def main(argv=None):
    parser = _FixedErrorParser(add_help=False)
    parser.add_argument("--gate", required=True)
    parser.add_argument("--freshness-cutoff", required=True)
    parser.add_argument("--expected-keys", required=True)
    try:
        args = parser.parse_args(argv)
        freshness_cutoff = float(args.freshness_cutoff)
        expected_keys = _parse_expected_keys(args.expected_keys)
        if not math.isfinite(freshness_cutoff):
            raise ValueError
    except (TypeError, ValueError):
        print("invalid network-quality readiness inputs", file=sys.stderr)
        return 65

    if wait_for_quality(expected_keys, freshness_cutoff, gate_path=args.gate):
        return 0
    print("fresh network-quality history unavailable", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
