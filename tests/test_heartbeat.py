import copy
import json
from pathlib import Path
import pytest
from connection_monitoring.heartbeat import validate_snapshot, commit_body


def snapshot():
    return {'version': 1, 'site': 'example-site', 'observed_at': 1791288000,
            'uptime_seconds': 120.5, 'root_free_percent': 47.2,
            'services': dict.fromkeys(('host_services', 'monitoring_container', 'influxdb', 'grafana'), 'healthy')}


def test_valid_snapshot_and_unknown_metrics_are_copied():
    value = snapshot()
    value['uptime_seconds'] = value['root_free_percent'] = None
    clean = validate_snapshot(value, 'example-site')
    assert clean == value and clean is not value and clean['services'] is not value['services']


@pytest.mark.parametrize('key,value', [('version', True), ('version', 2), ('site', '../other'),
    ('observed_at', True), ('observed_at', float('nan')), ('uptime_seconds', -1),
    ('root_free_percent', 101), ('root_free_percent', float('inf')), ('received_at', 123)])
def test_invalid_fields_rejected(key, value):
    raw = snapshot(); raw[key] = value
    with pytest.raises(ValueError): validate_snapshot(raw)


def test_missing_or_injected_service_rejected_and_site_is_bound():
    for services in ({'grafana': 'healthy'}, dict(snapshot()['services'], arbitrary='healthy'),
                     dict(snapshot()['services'], grafana='all-green')):
        raw = snapshot(); raw['services'] = services
        with pytest.raises(ValueError): validate_snapshot(raw, expected_services=snapshot()['services'])
    with pytest.raises(ValueError): validate_snapshot(snapshot(), 'other-site')


def test_firestore_write_has_one_atomic_server_timestamp_transform():
    body = commit_body('example-project', 'siteHeartbeats', snapshot())
    assert len(body['writes']) == 1
    write = body['writes'][0]
    assert write['update']['name'] == 'projects/example-project/databases/(default)/documents/siteHeartbeats/example-site'
    assert write['updateTransforms'] == [{'fieldPath': 'received_at', 'setToServerValue': 'REQUEST_TIME'}]
    assert 'received_at' not in write['update']['fields']
    assert write['update']['fields']['services']['mapValue']['fields']['grafana'] == {'stringValue':'healthy'}


def test_shared_golden_fixture_contract():
    fixture = json.loads((Path(__file__).parent / 'fixtures/heartbeat-v1.json').read_text())
    assert validate_snapshot(fixture['valid_snapshot']) == fixture['valid_snapshot']
    for case in fixture['invalid_snapshots']:
        with pytest.raises(ValueError): validate_snapshot(case['snapshot'])
