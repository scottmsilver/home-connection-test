"""Version 1 positive health evidence validation, with no external effects.

Details are JSON objects: maximum depth 6, 256 nodes, 64 entries per
container, 1024 characters per string and 16 KiB encoded per check.
Reports contain at most 256 checks and encode to at most 1 MiB.
"""
import json
import math
import re


class HealthRejected(ValueError):
    """Required positive health evidence is unavailable or invalid."""


_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z", re.ASCII)


def _reject(reason, check_id=None):
    # Callers supply only fixed reasons and already validated identifiers.
    raise HealthRejected(reason + (": " + check_id if check_id else ""))


def _finite(value):
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except (OverflowError, ValueError):
        return False


def _safe_id(value):
    return type(value) is str and _ID.fullmatch(value) is not None


def _facts(value, depth, budget):
    budget[0] -= 1
    if budget[0] < 0 or depth > 6:
        return False
    kind = type(value)
    if value is None or kind is bool:
        return True
    if kind in (int, float):
        return _finite(value)
    if kind is str:
        return len(value) <= 1024
    if kind is list:
        return len(value) <= 64 and all(_facts(v, depth + 1, budget) for v in value)
    if kind is dict:
        return len(value) <= 64 and all(
            type(k) is str and len(k) <= 1024
            and _facts(v, depth + 1, budget) for k, v in value.items()
        )
    return False


def _encoded_size(value):
    try:
        return len(json.dumps(value, ensure_ascii=True, allow_nan=False,
                              separators=(",", ":")).encode("ascii"))
    except (ValueError, TypeError, OverflowError, RecursionError):
        _reject("Malformed health evidence")


def require_healthy(report, required_checks, now, max_age, max_future_skew=5):
    """Reject unless exactly the declared checks have fresh positive evidence.

    Check IDs are explicit, unique safe ASCII strings. Age limits are inclusive;
    timestamps and policy values must be finite numbers, never booleans.
    Future skew may be tightened but cannot exceed the five-second protocol limit.
    Raises HealthRejected with bounded diagnostics that never contain details.
    """
    if (not _finite(now) or now < 0 or not _finite(max_age) or max_age < 0
            or not _finite(max_future_skew) or not 0 <= max_future_skew <= 5):
        _reject("Invalid health time policy")
    if type(required_checks) not in (list, tuple, set, frozenset):
        _reject("Invalid required health checks")
    if not 1 <= len(required_checks) <= 256:
        _reject("Invalid required health checks")
    if not all(_safe_id(key) for key in required_checks):
        _reject("Invalid required health checks")
    required = set(required_checks)
    if len(required) != len(required_checks):
        _reject("Duplicate required health checks")
    if type(report) is not dict or set(report) != {"version", "observed_at", "checks"}:
        _reject("Malformed health report")
    if type(report["version"]) is not int or report["version"] != 1:
        _reject("Unsupported health version")

    def fresh(value, check_id=None):
        if not _finite(value) or value < 0:
            _reject("Invalid health timestamp", check_id)
        if now - value > max_age or value - now > max_future_skew:
            _reject("Health evidence expired or future", check_id)

    fresh(report["observed_at"])
    checks = report["checks"]
    if type(checks) is not dict or len(checks) > 256:
        _reject("Malformed health checks")
    if not all(_safe_id(key) for key in checks):
        _reject("Invalid health check identity")
    if set(checks) != required:
        _reject("Health check coverage mismatch")
    for key, check in checks.items():
        if type(check) is not dict or set(check) != {"ok", "observed_at", "details"}:
            _reject("Malformed health check", key)
        if check["ok"] is not True:
            _reject("Health check not healthy", key)
        fresh(check["observed_at"], key)
        details = check["details"]
        if type(details) is not dict or not _facts(details, 0, [256]):
            _reject("Malformed or oversized health details", key)
        if _encoded_size(details) > 16384:
            _reject("Oversized health details", key)
    if _encoded_size(report) > 1048576:
        _reject("Oversized health report")
