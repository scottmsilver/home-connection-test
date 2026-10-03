import sys
from pathlib import Path
import pytest
from connection_monitoring import alert_delivery as delivery

ALERT = {'status': 'firing', 'fingerprint': 'abc', 'startsAt': '2026-01-01', 'endsAt': '', 'labels': {'alertname': 'WAN'}, 'annotations': {'summary': 'Missing samples', '__dashboardUid__': 'sample-dashboard', '__panelId__': '1'}}

class Telegram:
    def __init__(self): self.texts = []; self.photos = []; self.fail_photo = False
    def send_message(self, text): self.texts.append(text); return 42
    def send_photo(self, data, reply):
        if self.fail_photo: raise delivery.DeliveryError('photo failed')
        self.photos.append((data, reply))

def test_photo_retry_is_durable_without_repeated_text(tmp_path):
    telegram = Telegram(); telegram.fail_photo = True
    path = tmp_path / 'progress.sqlite'
    service = delivery.DeliveryService(path, telegram, lambda a: b'png', clock=lambda: 100)
    with pytest.raises(delivery.DeliveryError): service(ALERT)
    telegram.fail_photo = False
    delivery.DeliveryService(path, telegram, lambda a: b'png', clock=lambda: 101)(ALERT)
    assert len(telegram.texts) == 1
    assert telegram.photos == [(b'png', 42)]

def test_dedup_allows_four_hour_repeat(tmp_path):
    telegram = Telegram(); now = [100]
    service = delivery.DeliveryService(tmp_path / 'db', telegram, lambda a: None, clock=lambda: now[0])
    service(ALERT); service(ALERT)
    assert len(telegram.texts) == 1
    now[0] += 14400; service(ALERT)
    assert len(telegram.texts) == 2

def test_render_error_does_not_block_text(tmp_path):
    telegram = Telegram()
    def fail(a): raise RuntimeError('secret')
    service = delivery.DeliveryService(tmp_path / 'db', telegram, fail)
    with pytest.raises(delivery.DeliveryError, match='graph rendering failed'):
        service(ALERT)
    service.render = lambda a: b'png'
    service(ALERT)
    assert len(telegram.texts) == 1
    assert telegram.photos == [(b'png', 42)]

def test_text_failure_is_retryable(tmp_path):
    telegram = Telegram()
    def fail(text): raise RuntimeError('secret')
    telegram.send_message = fail
    with pytest.raises(delivery.DeliveryError, match='text delivery failed'):
        delivery.DeliveryService(tmp_path / 'db', telegram)(ALERT)

@pytest.mark.parametrize('uid,panel', [('other','1'), ('sample-dashboard','6'), ('sample-dashboard','1&url=evil')])
def test_renderer_rejects_arbitrary_targets(uid, panel):
    renderer = configured_renderer('token')
    assert renderer({'annotations': {'__dashboardUid__': uid, '__panelId__': panel}}) is None

import io
import json
from urllib import request

class Opener:
    def __init__(self, body): self.body = body; self.requests = []
    def open(self, req, timeout):
        self.requests.append((req, timeout)); return io.BytesIO(self.body)

def test_telegram_wire_protocol_uses_html_and_photo_reply():
    opener = Opener(b'{"ok":true,"result":{"message_id":77}}')
    telegram = delivery.TelegramTransport('fake-token', 'chat', opener)
    assert telegram.send_message('<b>Hello</b>') == 77
    req, timeout = opener.requests[0]
    assert req.full_url == 'https://api.telegram.org/botfake-token/sendMessage'
    assert timeout == 10
    assert json.loads(req.data)['parse_mode'] == 'HTML'
    telegram.send_photo(b'PNG', 77)
    req, _ = opener.requests[1]
    assert b'"message_id": 77' in req.data
    assert b'PNG' in req.data
    assert req.full_url.endswith('/sendPhoto')

@pytest.mark.parametrize('body', [b'not png', b'\x89PNG\r\n\x1a\n' + b'x' * (5 * 1024 * 1024)])
def test_render_image_validation(body):
    opener = Opener(body)
    with pytest.raises(delivery.DeliveryError): configured_renderer('viewer', opener)(ALERT)

def test_render_fixed_local_url_and_timeout():
    opener = Opener(b'\x89PNG\r\n\x1a\n\x00\x00\x00\x0dIHDR\x00\x00\x03\xe8\x00\x00\x01\xf4rest')
    assert configured_renderer('viewer', opener)(ALERT) == opener.body
    req, timeout = opener.requests[0]
    assert req.full_url.startswith('http://127.0.0.1:3000/render/d-solo/sample-dashboard/sample-dashboard?')
    assert 'panelId=1' in req.full_url
    assert timeout == 35
    assert req.get_header('Authorization') == 'Bearer viewer'

def test_redirects_are_never_followed():
    assert delivery.NoRedirect().redirect_request(None,None,302,'',{},'https://evil.example') is None

def test_transport_suppresses_secrets():
    class Failed:
        def open(self, req, timeout): raise RuntimeError(req.full_url)
    with pytest.raises(delivery.DeliveryError) as error:
        delivery.TelegramTransport('secret', 'chat', Failed()).send_message('hello')
    assert 'secret' not in str(error.value)
    assert error.value.__suppress_context__

def test_sqlite_keeps_only_identity_and_progress(tmp_path):
    telegram = Telegram()
    path = tmp_path / 'db'
    delivery.DeliveryService(path, telegram)(ALERT)
    assert b'Missing samples' not in path.read_bytes()
    assert b'WAN' not in path.read_bytes()

def test_missing_fingerprint_does_not_merge_different_alerts(tmp_path):
    telegram = Telegram(); service = delivery.DeliveryService(tmp_path / 'db', telegram)
    first = dict(ALERT); first.pop('fingerprint')
    second = dict(first, labels={'alertname':'CPU'})
    service(first); service(second)
    assert len(telegram.texts) == 2

def test_pending_photo_retry_after_window_keeps_text(tmp_path):
    telegram = Telegram(); telegram.fail_photo = True; now = [100]
    service = delivery.DeliveryService(tmp_path / 'db', telegram, lambda a: b'png', clock=lambda: now[0])
    with pytest.raises(delivery.DeliveryError): service(ALERT)
    now[0] += 700; telegram.fail_photo = False; service(ALERT)
    assert len(telegram.texts) == 1

@pytest.mark.parametrize('message_id', [None, True, '77', -1])
def test_telegram_rejects_invalid_message_id(message_id):
    opener = Opener(json.dumps({'ok': True, 'result': {'message_id': message_id}}).encode())
    with pytest.raises(delivery.DeliveryError):
        delivery.TelegramTransport('fake', 'chat', opener).send_message('hello')

def test_model_failure_uses_formatter_fallback(tmp_path):
    telegram = Telegram()
    def fail(facts): raise RuntimeError('private-provider-detail')
    delivery.DeliveryService(tmp_path / 'db', telegram, summarize=fail)(ALERT)
    assert 'Missing samples' in telegram.texts[0]
    assert 'private-provider-detail' not in telegram.texts[0]

def test_firing_lease_end_changes_are_deduplicated(tmp_path):
    telegram = Telegram(); service = delivery.DeliveryService(tmp_path / 'db', telegram)
    service(ALERT); service(dict(ALERT, endsAt='later'))
    assert len(telegram.texts) == 1

def test_resolved_end_changes_have_distinct_identity(tmp_path):
    telegram = Telegram(); service = delivery.DeliveryService(tmp_path / 'db', telegram)
    service(dict(ALERT, status='resolved')); service(dict(ALERT, status='resolved', endsAt='later'))
    assert len(telegram.texts) == 2

@pytest.mark.parametrize('panel', ['1','panel-2'])
def test_renderer_parses_panel_url_but_fetches_only_local(panel):
    opener = Opener(b'\x89PNG\r\n\x1a\n\x00\x00\x00\x0dIHDR\x00\x00\x03\xe8\x00\x00\x01\xf4rest')
    renderer = configured_renderer('viewer', opener)
    assert renderer({'panelURL':f'https://untrusted.example/d/sample-dashboard/example?viewPanel={panel}'}) == opener.body
    assert opener.requests[0][0].full_url.startswith('http://127.0.0.1:3000/render/d-solo/sample-dashboard/')

@pytest.mark.parametrize('url', ['https://example/d/other/slug?viewPanel=1','https://example/d/sample-dashboard/slug?viewPanel=6','https://example/d/sample-dashboard/slug?viewPanel=1&viewPanel=2'])
def test_renderer_ignores_nonwhitelisted_panel_urls(url):
    opener = Opener(b'bad'); assert configured_renderer('viewer',opener)({'panelURL':url}) is None
    assert opener.requests == []

def test_recovered_photo_resets_dedup_window(tmp_path):
    telegram = Telegram(); telegram.fail_photo = True; now = [100]
    service = delivery.DeliveryService(tmp_path / 'db', telegram, lambda a: b'png', clock=lambda: now[0])
    with pytest.raises(delivery.DeliveryError): service(ALERT)
    now[0] += 700; telegram.fail_photo = False; service(ALERT); service(ALERT)
    assert len(telegram.texts) == 1

def test_graph_retry_expires_after_hour_without_repeating_text(tmp_path):
    telegram = Telegram(); now = [100]; renders = []
    def fail(alert): renders.append(alert); raise RuntimeError('unavailable')
    service = delivery.DeliveryService(tmp_path / 'db', telegram, fail, clock=lambda: now[0])
    with pytest.raises(delivery.DeliveryError): service(ALERT)
    now[0] += 3601
    service(ALERT); service(ALERT)
    assert len(telegram.texts) == len(renders) == 1
    assert service.db.execute('SELECT done FROM progress').fetchone()[0] == 1

def test_graph_lifetime_starts_when_text_is_accepted(tmp_path):
    telegram = Telegram(); now = [100]; original = telegram.send_message
    def text_fail(text): raise RuntimeError('unavailable')
    telegram.send_message = text_fail
    def graph_fail(alert): raise RuntimeError('unavailable')
    service = delivery.DeliveryService(tmp_path / 'db', telegram, graph_fail, clock=lambda: now[0])
    with pytest.raises(delivery.DeliveryError): service(ALERT)
    now[0] += 3601; telegram.send_message = original
    with pytest.raises(delivery.DeliveryError, match='graph rendering failed'): service(ALERT)
    assert len(telegram.texts) == 1

@pytest.mark.parametrize('path', ['/d/sample-dashboard', '/d/sample-dashboard/graphs'])
def test_renderer_accepts_grafana_dashboard_path_with_optional_slug(path):
    opener = Opener(b'\x89PNG\r\n\x1a\n\x00\x00\x00\x0dIHDR\x00\x00\x03\xe8\x00\x00\x01\xf4rest')
    assert configured_renderer('viewer',opener)({'panelURL':'https://grafana.example'+path+'?viewPanel=1'}) == opener.body

def test_renderer_rejects_extra_dashboard_segments():
    assert configured_panel('https://grafana.example/d/sample-dashboard/foo/bar?viewPanel=1') is None

VALID_START='2026-10-03T10:00:00Z'
def history_alert(**changes): return dict(ALERT, startsAt=VALID_START,values={'A':2}, **changes)

def test_history_transitions_restart_and_differences(tmp_path):
    now=[100]; seen=[]; telegram=Telegram(); path=tmp_path/'db'
    service=delivery.DeliveryService(path,telegram,summarize=lambda f:seen.append(f),clock=lambda:now[0])
    service(history_alert()); now[0]+=14400
    service=delivery.DeliveryService(path,telegram,summarize=lambda f:seen.append(f),clock=lambda:now[0])
    changed=history_alert(); changed['values']={'A':4}; service(changed)
    now[0]+=1; service(dict(changed,status='resolved'))
    now[0]+=1; service(dict(changed,startsAt='2026-10-03T11:00:00Z'))
    assert [f['transition'] for f in seen]==['first_recorded','continuing','recovered','new_incident']
    assert seen[1]['differences']['A']=={'before':2,'after':4,'delta':2}
    assert (path.stat().st_mode & 0o777)==0o600

def test_recovery_overtakes_failed_firing_and_old_incident(tmp_path):
    telegram=Telegram(); service=delivery.DeliveryService(tmp_path/'db',telegram)
    original=telegram.send_message
    telegram.send_message=lambda text:(_ for _ in ()).throw(RuntimeError())
    with pytest.raises(delivery.DeliveryError): service(history_alert())
    telegram.send_message=original; service(dict(history_alert(),status='resolved'))
    service(history_alert())
    service(dict(history_alert(),startsAt='2026-10-03T11:00:00Z'))
    service(dict(history_alert(),startsAt='2026-10-03T09:00:00Z'))
    assert len(telegram.texts)==2

def test_incomparable_dates_do_not_replace_valid_baseline(tmp_path):
    seen=[]; service=delivery.DeliveryService(tmp_path/'db',Telegram(),summarize=lambda f:seen.append(f))
    service(history_alert()); service(dict(history_alert(),startsAt='bad'))
    assert seen[1]['transition']=='incomparable' and seen[1]['previous'] is None
    assert service.db.execute('SELECT incident_start FROM history').fetchone()[0]==VALID_START

def test_history_failure_graph_retry_isolation_and_expiry(tmp_path):
    now=[100]; seen=[]; telegram=Telegram(); telegram.fail_photo=True
    service=delivery.DeliveryService(tmp_path/'db',telegram,lambda a:b'png',lambda f:seen.append(f),lambda:now[0])
    with pytest.raises(delivery.DeliveryError):service(history_alert())
    assert service.db.execute('SELECT count(*) FROM history').fetchone()[0]==1
    telegram.fail_photo=False; service(history_alert()); assert len(seen)==1
    service(dict(history_alert(),fingerprint='other',labels={'alertname':'WAN','site':'other'}))
    assert seen[-1]['previous'] is None
    now[0]+=31*86400; service(history_alert()); assert seen[-1]['transition']=='first_recorded'

def test_failed_text_does_not_advance_history(tmp_path):
    telegram=Telegram(); service=delivery.DeliveryService(tmp_path/'db',telegram)
    telegram.send_message=lambda text:(_ for _ in ()).throw(RuntimeError())
    with pytest.raises(delivery.DeliveryError):service(history_alert())
    assert service.db.execute('SELECT count(*) FROM history').fetchone()[0]==0

def test_bounded_history_and_numeric_storage(tmp_path):
    telegram=Telegram(); service=delivery.DeliveryService(tmp_path/'db',telegram)
    for i in range(1001):service(dict(history_alert(),fingerprint=str(i),labels={'alertname':'WAN','host':str(i)},values={str(k):k for k in range(30)}))
    assert service.db.execute('SELECT count(*) FROM history').fetchone()[0]==1000
    import json
    assert len(json.loads(service.db.execute('SELECT values_json FROM history LIMIT 1').fetchone()[0]))==20
    assert service.db.execute('PRAGMA secure_delete').fetchone()[0]==1

@pytest.mark.parametrize('start',[None,'','2026-99-03T10:00:00Z','2026-10-03','2026-10-03T10:00:00'])
def test_invalid_start_never_claims_incident_elapsed(tmp_path,start):
    seen=[]; now=[100];service=delivery.DeliveryService(tmp_path/'db',Telegram(),summarize=lambda f:seen.append(f),clock=lambda:now[0])
    service(dict(history_alert(),startsAt=start));now[0]+=14400;service(dict(history_alert(),startsAt=start))
    assert seen[-1]['transition']=='incomparable' and seen[-1]['previous'] is None

def test_pending_graph_completes_after_newer_recovery(tmp_path):
    telegram=Telegram();telegram.fail_photo=True;service=delivery.DeliveryService(tmp_path/'db',telegram,lambda a:b'png')
    with pytest.raises(delivery.DeliveryError):service(history_alert())
    telegram.fail_photo=False;service(dict(history_alert(),status='resolved'));service(history_alert())
    assert len(telegram.texts)==2 and len(telegram.photos)==2
    assert service.db.execute('SELECT status FROM history').fetchone()[0]=='resolved'

def test_valid_start_replaces_incomparable_first_snapshot(tmp_path):
    seen=[]; now=[100];service=delivery.DeliveryService(tmp_path/'db',Telegram(),summarize=lambda f:seen.append(f),clock=lambda:now[0])
    service(dict(history_alert(),startsAt='bad'));now[0]+=1;service(history_alert())
    now[0]+=14400;service(history_alert())
    assert service.db.execute('SELECT incident_start FROM history').fetchone()[0]==VALID_START
    assert seen[-1]['transition']=='continuing'
    assert seen[1]['history_available'] is True and seen[1]['incident_comparable'] is False


def test_native_readiness_graph_is_allowed():
    assert configured_panel('https://grafana.example/d/sample-dashboard/graphs?viewPanel=5') == '5'


def test_renderer_rejects_grafana_error_image_for_retry():
    import struct
    error_png=b'\x89PNG\r\n\x1a\n'+struct.pack('>I',13)+b'IHDR'+struct.pack('>II',526,202)+b'error'
    with pytest.raises(delivery.DeliveryError):configured_renderer('viewer',Opener(error_png))(ALERT)

def configured_renderer(*args, **kwargs):
    return delivery.GrafanaRenderer(*args, dashboard='sample-dashboard', panels=('1','2','3','4','5'), port=3000, **kwargs)
def configured_panel(url):
    return delivery.panel_from_url(url, dashboard='sample-dashboard', panels=('1','2','3','4','5'))

def test_adopts_preexisting_schema_and_accepted_text_pending_graph(tmp_path):
    import hashlib
    import sqlite3
    path=tmp_path/'preexisting.sqlite'
    fields={key:ALERT.get(key) for key in ('status','fingerprint','startsAt')}
    identity=hashlib.sha256(json.dumps(fields,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    with sqlite3.connect(str(path)) as db:
        db.execute('CREATE TABLE history (subject TEXT PRIMARY KEY, status TEXT NOT NULL, incident_start TEXT, accepted REAL NOT NULL, values_json TEXT NOT NULL)')
        db.execute('CREATE TABLE progress (identity TEXT PRIMARY KEY, created REAL NOT NULL, message_id INTEGER, done INTEGER NOT NULL DEFAULT 0)')
        db.execute('INSERT INTO progress VALUES (?,?,?,?)',(identity,100,42,0))
    telegram=Telegram()
    service=delivery.DeliveryService(path,telegram,lambda alert:b'png',clock=lambda:101)
    service(ALERT)
    assert telegram.texts==[]
    assert telegram.photos==[(b'png',42)]
    assert service.db.execute('SELECT message_id,done FROM progress WHERE identity=?',(identity,)).fetchone()==(42,1)
    assert [r[1] for r in service.db.execute('PRAGMA table_info(progress)')]==['identity','created','message_id','done']
    service.db.close()
