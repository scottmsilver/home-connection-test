"""Generic pinned health-report contract; no deployment-specific check predicates."""
import re
from .heartbeat import IDENTIFIER, bounded_number

FIELDS={'version','site','observed_at','uptime_seconds','root_free_percent','check_revision','checks'}
LEAF={'available','healthy','observed_at','source_observed_at','source_expires_at'}
CHECK_ID=re.compile(r'[a-z0-9][a-z0-9_-]{0,63}\.[a-z0-9][a-z0-9_-]{0,31}\.[a-z0-9][a-z0-9_-]{0,63}\Z')
REVISION=re.compile(r'[0-9a-f]{64}\Z')


def validate_health_snapshot(raw,expected_site=None,expected_checks=None,expected_revision=None):
    if type(raw) is not dict or set(raw)!=FIELDS or type(raw['version']) is not int or raw['version']!=2:
        raise ValueError('invalid health report fields')
    site=raw['site']
    if type(site) is not str or not IDENTIFIER.fullmatch(site) or expected_site is not None and site!=expected_site:
        raise ValueError('invalid health report site')
    revision=raw['check_revision']
    if type(revision) is not str or not REVISION.fullmatch(revision) or expected_revision is not None and revision!=expected_revision:
        raise ValueError('health revision mismatch')
    for key,maximum in [('observed_at',1e12),('uptime_seconds',1e12),('root_free_percent',100)]:
        if raw[key] is None and key!='observed_at':continue
        if not bounded_number(raw[key],maximum):raise ValueError('invalid health metric')
    checks=raw['checks']
    if type(checks) is not dict or not 1<=len(checks)<=64 or expected_checks is not None and set(checks)!=set(expected_checks):
        raise ValueError('health check set mismatch')
    for key,item in checks.items():
        if type(key) is not str or not CHECK_ID.fullmatch(key) or not key.startswith(site+'.'):
            raise ValueError('invalid health check identity')
        if type(item) is not dict or set(item)!=LEAF or type(item['available']) is not bool:
            raise ValueError('invalid health check')
        if item['available']:
            if type(item['healthy']) is not bool or item['observed_at'] is None:raise ValueError('invalid health verdict')
        elif item['healthy'] is not None:raise ValueError('invalid unknown verdict')
        for field in ('observed_at','source_observed_at','source_expires_at'):
            if item[field] is not None and not bounded_number(item[field],1e12):raise ValueError('invalid health time')
        source,expiry=item['source_observed_at'],item['source_expires_at']
        if (source is None)!=(expiry is None):raise ValueError('incomplete health source')
        if source is not None and (expiry<=source or item['observed_at'] is None or source>item['observed_at']+5):
            raise ValueError('invalid health source')
    return dict(raw,checks={k:dict(v) for k,v in checks.items()})
