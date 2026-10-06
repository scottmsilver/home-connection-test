"""Versioned, bounded heartbeat snapshots independent of deployment identity."""
import math
import re

FIELDS = {'version', 'site', 'observed_at', 'uptime_seconds', 'root_free_percent', 'services'}
STATES = {'healthy', 'unhealthy', 'unknown'}
IDENTIFIER = re.compile(r'[a-z0-9][a-z0-9_-]{0,63}\Z')
PROJECT = re.compile(r'[a-z][a-z0-9-]{4,61}[a-z0-9]\Z')
COLLECTION = re.compile(r'[A-Za-z][A-Za-z0-9_-]{0,63}\Z')


def bounded_number(value, maximum):
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= maximum


def validate_snapshot(raw, expected_site=None, expected_services=None):
    if type(raw) is not dict or set(raw) != FIELDS:
        raise ValueError('invalid heartbeat fields')
    if type(raw['version']) is not int or raw['version'] != 1:
        raise ValueError('unsupported heartbeat version')
    if type(raw['site']) is not str or not IDENTIFIER.fullmatch(raw['site']):
        raise ValueError('invalid site identifier')
    if expected_site is not None and raw['site'] != expected_site:
        raise ValueError('site identity mismatch')
    for field, maximum in [('observed_at', 1e12), ('uptime_seconds', 1e12), ('root_free_percent', 100)]:
        if raw[field] is None and field != 'observed_at': continue
        if not bounded_number(raw[field], maximum): raise ValueError('invalid heartbeat metric')
    services = raw['services']
    if type(services) is not dict or not 1 <= len(services) <= 8:
        raise ValueError('invalid service map')
    if any(type(k) is not str or not IDENTIFIER.fullmatch(k) or type(v) is not str or v not in STATES for k, v in services.items()):
        raise ValueError('invalid service state')
    if expected_services is not None and set(services) != set(expected_services):
        raise ValueError('service contract mismatch')
    return dict(raw, services=dict(services))


def firestore_value(value):
    if value is None: return {'nullValue': None}
    if type(value) is str: return {'stringValue': value}
    if type(value) is int: return {'integerValue': str(value)}
    if type(value) is float: return {'doubleValue': value}
    if type(value) is dict: return {'mapValue': {'fields': {k: firestore_value(v) for k, v in value.items()}}}
    raise ValueError('unsupported Firestore value')


def commit_body(project, collection, raw):
    if type(project) is not str or not PROJECT.fullmatch(project): raise ValueError('invalid project identifier')
    if type(collection) is not str or not COLLECTION.fullmatch(collection): raise ValueError('invalid collection identifier')
    snapshot = validate_snapshot(raw)
    name = 'projects/{}/databases/(default)/documents/{}/{}'.format(project, collection, snapshot['site'])
    return {'writes': [{'update': {'name': name, 'fields': {k: firestore_value(v) for k, v in snapshot.items()}},
                        'updateTransforms': [{'fieldPath': 'received_at', 'setToServerValue': 'REQUEST_TIME'}]}]}
