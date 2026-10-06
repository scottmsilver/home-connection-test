import copy
import pytest
from connection_monitoring.health_report import validate_health_snapshot
from connection_monitoring.heartbeat import commit_body


REVISION='a'*64
def report():
    return {'version':2,'site':'example-site','observed_at':1000,'uptime_seconds':20,'root_free_percent':42,
        'check_revision':REVISION,'checks':{'example-site.monitor.telemetry':{'available':True,'healthy':False,
        'observed_at':1000,'source_observed_at':500,'source_expires_at':1100}}}


def test_v2_preserves_facts_and_source_times_with_exact_config_binding():
    raw=report();clean=validate_health_snapshot(raw,'example-site',list(raw['checks']),REVISION)
    assert clean==raw and clean['checks'] is not raw['checks']
    assert clean['checks']['example-site.monitor.telemetry']['source_observed_at']==500


@pytest.mark.parametrize('change',[lambda r:r.update(received_at=1000),lambda r:r.update(check_revision='wrong'),
    lambda r:r.update(version=True),lambda r:r.update(checks={}),
    lambda r:r['checks']['example-site.monitor.telemetry'].update(available='yes'),
    lambda r:r['checks']['example-site.monitor.telemetry'].update(available=False),
    lambda r:r['checks']['example-site.monitor.telemetry'].update(observed_at=None),
    lambda r:r['checks']['example-site.monitor.telemetry'].update(extra='secret'),
    lambda r:r['checks']['example-site.monitor.telemetry'].update(source_expires_at=None),
    lambda r:r['checks']['example-site.monitor.telemetry'].update(source_expires_at=499),
    lambda r:r['checks']['example-site.monitor.telemetry'].update(source_observed_at=True)])
def test_v2_malformed_leaf_and_envelope_rejected(change):
    raw=report();change(raw)
    with pytest.raises(ValueError):validate_health_snapshot(raw)


def test_unknown_measurements_are_nullable_but_cannot_pass():
    raw=report();raw['checks']['example-site.monitor.telemetry']={
        'available':False,'healthy':None,'observed_at':None,'source_observed_at':None,'source_expires_at':None}
    assert validate_health_snapshot(raw)==raw


def test_pin_and_exact_ids_and_site_cannot_be_substituted():
    raw=report()
    for args in [('other-site',list(raw['checks']),REVISION),('example-site',[],REVISION),('example-site',list(raw['checks']),'b'*64)]:
        with pytest.raises(ValueError):validate_health_snapshot(raw,*args)
    raw['checks']['other-site.monitor.telemetry']=raw['checks'].pop('example-site.monitor.telemetry')
    with pytest.raises(ValueError):validate_health_snapshot(raw)


def test_v2_single_firestore_write_types_booleans_and_nulls_without_receipt_spoof():
    body=commit_body('example-project','siteHeartbeats',report())
    fields=body['writes'][0]['update']['fields']
    assert fields['version']=={'integerValue':'2'}
    leaf=fields['checks']['mapValue']['fields']['example-site.monitor.telemetry']['mapValue']['fields']
    assert leaf['available']=={'booleanValue':True} and leaf['healthy']=={'booleanValue':False}
    assert body['writes'][0]['updateTransforms']==[{'fieldPath':'received_at','setToServerValue':'REQUEST_TIME'}]


def test_shared_typed_fixtures_preserve_all_supplied_values():
    import json
    from pathlib import Path
    from connection_monitoring.heartbeat import firestore_value
    fixture=json.loads((Path(__file__).parent/'fixtures/health-report-v2.json').read_text())
    for case in fixture['cases']:
        document=case['document']
        fields={key:firestore_value(value) for key,value in document.items() if key!='received_at'}
        assert fields=={key:value for key,value in case['firestore_document']['fields'].items() if key!='received_at'}
    assert {case['name'] for case in fixture['cases']}=={'fresh','pin_mismatch','stale_measurement','expired_source','future_measurement','partial','unavailable'}
