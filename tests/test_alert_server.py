import sys
from pathlib import Path
import pytest
from connection_monitoring import alert_server as server

@pytest.mark.parametrize('payload', [None, [], {}, {'alerts': []}, {'alerts': [{}]}, {'alerts': [{'status':'wrong'}]}, {'alerts': [{'status':'firing','labels': []}]}, {'alerts':[{'status':'firing'}]*21}])
def test_rejects_invalid_payload(payload):
    with pytest.raises(ValueError): server.validate_payload(payload)

def test_validates_all_before_delivery():
    assert server.validate_payload({'alerts':[{'status':'firing','labels': {}, 'annotations': {}, 'values': {}}]})[0]['status'] == 'firing'

import http.client
import json
import threading

@pytest.fixture
def endpoint():
    delivered = []
    instance = server.make_server('private', delivered.append, ('127.0.0.1', 0))
    worker = threading.Thread(target=instance.serve_forever, daemon=True); worker.start()
    yield instance.server_address, delivered
    instance.shutdown(); worker.join(); instance.server_close()

def post(address, body, token='private', headers=None):
    client = http.client.HTTPConnection(*address, timeout=2)
    client.request('POST', '/alerts', body, {'Authorization': 'Bearer ' + token, **(headers or {})})
    response = client.getresponse(); code = response.status; response.read(); client.close()
    return code

def test_http_authenticated_and_all_or_nothing(endpoint):
    address, delivered = endpoint
    good = {'status':'firing'}
    assert post(address, json.dumps({'alerts':[good]}), 'wrong') == 401
    assert post(address, '{bad') == 400
    assert post(address, json.dumps({'alerts':[good, {'status':'bad'}]})) == 400
    assert delivered == []
    assert post(address, json.dumps({'alerts':[good]})) == 200
    assert delivered == [dict(good, labels={}, annotations={}, values={})]

def test_http_limits_and_health(endpoint):
    address, delivered = endpoint
    assert post(address, b'x' * (server.MAX_BODY + 1)) == 413
    client = http.client.HTTPConnection(*address, timeout=2)
    client.request('GET', '/healthz'); assert client.getresponse().status == 200; client.close()
    assert delivered == []

def test_http_delivery_failure_returns_503():
    def fail(alert): raise RuntimeError('private upstream credential')
    instance = server.make_server('private', fail, ('127.0.0.1',0))
    worker = threading.Thread(target=instance.handle_request); worker.start()
    try:
        assert post(instance.server_address, json.dumps({'alerts':[{'status':'firing'}]})) == 503
    finally:
        worker.join(); instance.server_close()

def test_cannot_bind_public_interface():
    with pytest.raises(ValueError): server.make_server('private', lambda a: None, ('0.0.0.0',8092))

def test_runtime_requires_nonempty_secrets(monkeypatch, tmp_path):
    from tests.notifier_helpers import notifier_config
    config = notifier_config(tmp_path)
    for name in config['secrets'].values():
        monkeypatch.setenv(name, '')
    with pytest.raises(ValueError, match='required'):
        server.runtime_delivery(config)

def test_normalizes_no_data_nulls():
    alert = server.validate_payload({'alerts':[{'status':'firing', 'labels':None,'annotations':None,'values':{'A':None,'B':2}}]})[0]
    assert alert['labels'] == alert['annotations'] == {}
    assert alert['values'] == {'A':None, 'B':2}
    assert server.validate_payload({'alerts':[{'status':'firing','values':None}]})[0]['values'] == {}

def test_queue_accepts_batch_and_health_during_slow_delivery(tmp_path):
    from connection_monitoring.alert_queue import AlertQueue
    gate = threading.Event(); entered = threading.Event()
    queue = AlertQueue(tmp_path / 'queue', capacity=2)
    def deliver(alert): entered.set(); gate.wait(3)
    queue.start(lambda: deliver)
    instance = server.make_server('private', queue, ('127.0.0.1',0))
    worker = threading.Thread(target=instance.serve_forever,daemon=True); worker.start()
    try:
        assert post(instance.server_address, json.dumps({'alerts':[{'status':'firing'}]})) == 200
        assert entered.wait(1)
        assert post(instance.server_address, json.dumps({'alerts':[{'status':'firing'}]*2})) == 503
        assert queue.pending() == 1
        client = http.client.HTTPConnection(*instance.server_address, timeout=1)
        client.request('GET','/healthz'); assert client.getresponse().status == 200; client.close()
    finally:
        gate.set(); instance.shutdown(); worker.join(); instance.server_close(); queue.close()

def test_runtime_loop_exits_for_dead_worker():
    class Queue:
        def healthy(self): return False
    class Listener:
        timeout = None
        def handle_request(self): raise AssertionError('must not accept')
    with pytest.raises(SystemExit, match='notifier worker unavailable'):
        server.serve_runtime(Listener(), Queue())

def test_null_reading_survives_server_queue_model_boundary():
    from connection_monitoring.alert_queue import selected_alert
    from connection_monitoring.alert_summary import facts_for_model
    alert=server.validate_payload({'alerts':[{'status':'firing','values':{'A':None}}]})[0]
    facts=facts_for_model(selected_alert(alert))
    assert facts['numeric_evidence']==[{'id':'value:A','name':'A','value':None,'kind':'unknown_reference'}]
