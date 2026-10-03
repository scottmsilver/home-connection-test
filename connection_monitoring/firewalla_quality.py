#!/usr/bin/env python3
"""Convert sanitized Firewalla WAN-quality history to Influx line protocol."""

import argparse
import json
import sys
from decimal import Decimal, InvalidOperation


ALLOWED_ENVELOPE_KEYS = frozenset(("code", "data"))
ALLOWED_RECORD_KEYS = frozenset(("stat",))
ALLOWED_STAT_KEYS = frozenset(("min", "max", "median", "mean", "lossrate"))
MAX_TIMESTAMP_NS = 9_223_372_036_854_775_807


class InvalidInput(ValueError):
    """Input cannot be converted safely."""


class ConflictingInput(InvalidInput):
    """Input contains ambiguous point identity and must fail as a whole."""


def parse_args(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", action="store_true")
    parser.add_argument("--site", required=True)
    parser.add_argument("--router", required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--wan", action="append", required=True, metavar="UUID=NAME")
    return parser.parse_args(argv)


def parse_wan_map(values):
    result = {}
    names = set()
    for value in values:
        uuid, separator, name = value.partition("=")
        if not separator or not uuid or not name or uuid in result or name in names:
            raise InvalidInput("invalid WAN mapping")
        result[uuid] = name
        names.add(name)
    return result


def reject_duplicate_members(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise InvalidInput("duplicate JSON member")
        result[key] = value
    return result


def escape_tag(value):
    return str(value).replace("\\", "\\\\").replace(",", "\\,").replace("=", "\\=").replace(" ", "\\ ")


def as_decimal(value):
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        raise InvalidInput("metric is not numeric")
    decimal = Decimal(value)
    if not decimal.is_finite():
        raise InvalidInput("metric is not finite")
    return decimal


def format_decimal(value):
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def timestamp_ns(value):
    if not isinstance(value, str):
        raise InvalidInput("timestamp is not a string")
    try:
        seconds = Decimal(value)
    except InvalidOperation as error:
        raise InvalidInput("invalid timestamp") from error
    nanoseconds = seconds * Decimal(1_000_000_000)
    if not seconds.is_finite() or nanoseconds != nanoseconds.to_integral_value():
        raise InvalidInput("invalid timestamp")
    result = int(nanoseconds)
    if result < 0 or result > MAX_TIMESTAMP_NS:
        raise InvalidInput("invalid timestamp")
    return result


def parse_key(value, target, names):
    prefix = f"metric:monitor:raw:ping:{target}:"
    if not isinstance(value, str) or not value.startswith(prefix):
        raise InvalidInput("unexpected quality key")
    uuid = value[len(prefix) :]
    if not uuid or uuid not in names:
        raise InvalidInput("unexpected quality key")
    return uuid


def convert_point(site, router, target, wan, timestamp, record):
    if not isinstance(record, dict) or set(record) != ALLOWED_RECORD_KEYS:
        raise InvalidInput("invalid quality record")
    stat = record["stat"]
    if (
        not isinstance(stat, dict)
        or not set(stat).issubset(ALLOWED_STAT_KEYS)
        or "lossrate" not in stat
    ):
        raise InvalidInput("invalid quality statistics")

    numeric = {name: as_decimal(value) for name, value in stat.items()}
    lossrate = numeric["lossrate"]
    if lossrate < 0 or lossrate > 1:
        raise InvalidInput("loss rate out of range")
    for name in ("min", "max", "median", "mean"):
        if name in numeric and numeric[name] < 0:
            raise InvalidInput("latency out of range")

    fields = [f"packet_loss_percent={format_decimal(lossrate * 100)}"]
    if "mean" in numeric:
        fields.append(f"latency_ms={format_decimal(numeric['mean'])}")
    tags = (
        f"site={escape_tag(site)},wan={escape_tag(wan)},"
        f"router={escape_tag(router)},source=firewalla,target={escape_tag(target)}"
    )
    return f"wan_quality,{tags} {','.join(fields)} {timestamp}"


def convert(envelope, site, router, target, names):
    if (
        not isinstance(envelope, dict)
        or set(envelope) != ALLOWED_ENVELOPE_KEYS
        or isinstance(envelope.get("code"), bool)
        or envelope.get("code") != 200
        or not isinstance(envelope.get("data"), dict)
    ):
        raise InvalidInput("invalid API envelope")

    output = []
    identities = set()
    for quality_key, history in envelope["data"].items():
        uuid = parse_key(quality_key, target, names)
        if not isinstance(history, dict):
            raise InvalidInput("invalid quality history")
        for timestamp_key, record in history.items():
            point_timestamp = timestamp_ns(timestamp_key)
            identity = (names[uuid], point_timestamp)
            if identity in identities:
                raise ConflictingInput("duplicate quality point")
            line = convert_point(
                site, router, target, names[uuid], point_timestamp, record
            )
            identities.add(identity)
            output.append((point_timestamp, names[uuid], line))

    output.sort(key=lambda item: (item[0], item[1]))
    return [item[2] for item in output]


def convert_state(envelope, site, router, names):
    if not isinstance(envelope, dict) or set(envelope) != {'time', 'data'}:
        raise InvalidInput('invalid state envelope')
    collected = envelope['time']
    data = envelope['data']
    if type(collected) is not int or not isinstance(data, dict) or set(data) != set(names):
        raise InvalidInput('incomplete native state')
    timestamp = timestamp_ns(str(collected))
    output = []
    for uuid, state in sorted(data.items()):
        if not isinstance(state, dict) or set(state) != {'ready', 'active'} or any(type(v) is not bool for v in state.values()):
            raise InvalidInput('invalid native state')
        tags = f'site={escape_tag(site)},wan={escape_tag(names[uuid])},router={escape_tag(router)},source=firewalla'
        output.append(f'wan_state,{tags} ready={int(state["ready"])}i,active={int(state["active"])}i {timestamp}')
    return output


def main(argv=None):
    try:
        args = parse_args(argv)
        names = parse_wan_map(args.wan)
        envelope = json.loads(
            sys.stdin.read(),
            parse_float=Decimal,
            parse_constant=lambda _value: (_ for _ in ()).throw(InvalidInput("invalid numeric constant")),
            object_pairs_hook=reject_duplicate_members,
        )
        lines = (convert_state(envelope, args.site, args.router, names) if args.state
                 else convert(envelope, args.site, args.router, args.target, names))
    except (InvalidInput, json.JSONDecodeError) as error:
        print(f"invalid quality input: {error}", file=sys.stderr)
        return 65

    if not lines:
        return 65
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
