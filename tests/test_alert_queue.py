import json
import sys
import threading
import time
from pathlib import Path
import pytest
from connection_monitoring.alert_queue import AlertQueue, QueueError

ALERT = {'status':'firing','fingerprint':'abc','labels':{'alertname':'WAN','password':'secret'},'annotations':{'summary':'missing','secret':'secret'},'values':{'A':2},'extra':'secret'}

def wait_for(predicate):
    end = time.monotonic() + 3
    while not predicate():
        assert time.monotonic() < end
        time.sleep(.01)

def test_persisted_queue_survives_restart_and_selects_fields(tmp_path):
    path = tmp_path / 'queue.sqlite'
    queue = AlertQueue(path, capacity=2)
    queue.enqueue_batch([ALERT]); queue.close()
    assert b'secret' not in path.read_bytes()
    second = AlertQueue(path, capacity=2)
    received = []
    second.start(lambda: received.append)
    wait_for(lambda: len(received) == 1 and second.pending() == 0)
    second.close()
    assert received[0]['labels'] == {'alertname':'WAN'}
    assert received[0]['annotations'] == {'summary':'missing'}

def test_full_batch_is_atomic(tmp_path):
    queue = AlertQueue(tmp_path / 'queue', capacity=2)
    queue.enqueue_batch([ALERT])
    with pytest.raises(QueueError): queue.enqueue_batch([ALERT, ALERT])
    assert queue.pending() == 1
    queue.close()

def test_worker_retries_failure_and_factory_runs_on_worker(tmp_path):
    queue = AlertQueue(tmp_path / 'queue', retry_delay=.02)
    calls = []; ids = []
    def factory():
        ids.append(threading.get_ident())
        def deliver(alert):
            calls.append(alert)
            if len(calls) == 1: raise RuntimeError('secret')
        return deliver
    queue.start(factory); queue.enqueue_batch([ALERT])
    wait_for(lambda: len(calls) == 2 and queue.pending() == 0)
    assert ids != [threading.get_ident()]
    queue.close()

def test_startup_factory_failure_is_sanitized(tmp_path):
    queue = AlertQueue(tmp_path / 'queue')
    def fail(): raise RuntimeError('secret')
    with pytest.raises(QueueError, match='worker startup failed') as error: queue.start(fail)
    assert 'secret' not in str(error.value)
    queue.close()

def test_enqueue_is_fast_while_worker_delivery_blocks(tmp_path):
    queue = AlertQueue(tmp_path / 'queue'); blocked = threading.Event(); entered = threading.Event()
    def deliver(alert): entered.set(); blocked.wait(2)
    queue.start(lambda: deliver); queue.enqueue_batch([ALERT]); assert entered.wait(1)
    begin = time.monotonic(); queue.enqueue_batch([ALERT]); assert time.monotonic()-begin < .2
    blocked.set(); queue.close()

def test_selector_discards_non_numeric_and_url_credentials():
    from connection_monitoring.alert_queue import selected_alert
    selected = selected_alert(dict(ALERT, values={'A':None, 'B':'secret', 'C':True, 'D':2}, generatorURL='https://grafana.example/alert/view?token=secret#secret'))
    assert selected['values'] == {'A':None, 'D':2}
    assert selected['generatorURL'] == 'https://grafana.example/alert/view'
    assert 'generatorURL' not in selected_alert(dict(ALERT,generatorURL='https://user:secret@grafana.example/'))

def test_worker_database_failure_is_silent_and_unhealthy(tmp_path, monkeypatch):
    errors = []
    monkeypatch.setattr(threading, 'excepthook', errors.append)
    queue = AlertQueue(tmp_path / 'queue'); queue.start(lambda: lambda alert: None)
    with queue.lock, queue.db:
        queue.db.execute('DROP TABLE jobs')
    queue.wake.set()
    wait_for(lambda: not queue.thread.is_alive())
    assert errors == []
    assert not queue.healthy()
    queue.close()

def test_preserves_panel_url_for_safe_local_renderer():
    from connection_monitoring.alert_queue import selected_alert
    assert selected_alert(dict(ALERT, panelURL='https://grafana.example/d/sample-dashboard/graphs?viewPanel=1&secret=discard'))['panelURL'] == 'https://grafana.example/d/sample-dashboard/graphs?viewPanel=1'

def test_enqueue_rejects_dead_worker(tmp_path):
    queue = AlertQueue(tmp_path / 'queue')
    queue.start(lambda: lambda alert: None)
    queue.stop.set(); queue.wake.set(); queue.thread.join()
    with pytest.raises(QueueError, match='worker unavailable'): queue.enqueue_batch([ALERT])
    queue.close()

def test_queue_preserves_real_grafana_panel_url_without_slug():
    from connection_monitoring.alert_queue import selected_alert
    url = 'https://grafana.example/d/sample-dashboard?viewPanel=1'
    assert selected_alert(dict(ALERT,panelURL=url))['panelURL'] == url

def test_selected_rule_context_survives_without_other_annotations():
    import json
    from connection_monitoring.alert_queue import selected_alert
    from connection_monitoring.alert_summary import facts_for_model
    raw=json.dumps({'kind':'disk_capacity','measurement':'root usage','unit':'percent','threshold':85,'measurement_ref':'Alarm','extra':'secret'})
    selected=selected_alert(dict(ALERT,annotations={'estate_context':raw,'password':'secret'}))
    assert selected['annotations']['estate_context']==raw
    assert 'password' not in selected['annotations']
    rule=facts_for_model(selected)['rule']
    assert rule['threshold']==85 and 'extra' not in rule
    assert facts_for_model(dict(selected,annotations={'estate_context':'x'*4097}))['rule']=={}

@pytest.mark.parametrize('panel',['PRIVATE_TOKEN','0','-1','1%0Asecret','1%26secret','1234567890'])
def test_queue_discards_non_panel_references(panel):
    from connection_monitoring.alert_queue import selected_alert
    assert 'panelURL' not in selected_alert(dict(ALERT,panelURL='https://grafana.example/d/sample-dashboard?viewPanel='+panel))

def test_queue_preserves_panel_prefix_for_browser_links():
    from connection_monitoring.alert_queue import selected_alert
    assert selected_alert(dict(ALERT,panelURL='https://grafana.example/d/sample-dashboard?viewPanel=panel-2'))['panelURL']=='https://grafana.example/d/sample-dashboard?viewPanel=panel-2'
