"""Human-readable notification times; stored identities remain unchanged."""
from datetime import datetime, timezone
import re
from zoneinfo import ZoneInfo

DEFAULT_TIMEZONE = 'America/Los_Angeles'
ISO = re.compile(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})')


def instant(value):
    try:
        if isinstance(value, str) and ISO.fullmatch(value):
            return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
        if type(value) in (int, float):
            return datetime.fromtimestamp(value, timezone.utc).timestamp()
    except (ValueError, OverflowError, OSError):
        pass
    return None


def local_time(value, zone=DEFAULT_TIMEZONE):
    stamp = instant(value)
    if stamp is None: return None
    try:
        when = datetime.fromtimestamp(stamp, ZoneInfo(zone))
        return f'{when:%b} {when.day}, {when.year}, {when.hour % 12 or 12}:{when:%M %p %Z}'
    except (ValueError, OverflowError, OSError):
        return None


def localize(value, zone=DEFAULT_TIMEZONE, key=''):
    if isinstance(value, dict): return {k: localize(v, zone, k) for k,v in value.items()}
    if isinstance(value, list): return [localize(v, zone, key) for v in value]
    if isinstance(value, str):
        value = ISO.sub(lambda m: local_time(m[0], zone) or 'time unavailable', value)
        return re.sub(r'(?:(?:\d{1,2}:\d{2}(?::\d{2})?\s*(?:AM|PM)?\s*)?\b(?:UTC|GMT)\b)', 'local time unavailable', value, flags=re.IGNORECASE)
    if key in ('accepted_at','observed_at','now','started_at','ended_at','first_accepted_at','due_at','starts_at','ends_at','not_before','valid_until','source_observed_at','source_expires_at') and value is not None:
        return local_time(value, zone)
    return value


def duration(seconds):
    if seconds is None: return None
    minutes = int(seconds // 60)
    if minutes < 1: return 'less than a minute'
    days, minutes = divmod(minutes, 1440)
    hours, minutes = divmod(minutes, 60)
    parts = [f'{n} {unit}{"s" if n != 1 else ""}' for n,unit in ((days,'day'),(hours,'hour'),(minutes,'minute')) if n]
    return ', '.join(parts)
