import sys, json, io
from pathlib import Path
import pytest
from connection_monitoring import openrouter_summary as adapter
from connection_monitoring.alert_summary import facts_for_model

def facts(): return facts_for_model({'status':'resolved','labels':{},'annotations':{},'values':{}})
def answer(): return {'status':'resolved','headline':'Warning cleared','explanation':'Grafana cleared the condition.','what_changed':'No previous update is recorded.','evidence_ids':['status']}
def response(content): return io.BytesIO(json.dumps({'choices':[{'message':{'content':content}}]}).encode())

def test_original_prose_and_private_free_request():
    seen=[]
    def transport(req,timeout): seen.append((json.loads(req.data),timeout)); return response(json.dumps(answer()))
    assert configured_adapter('secret',transport)(facts())==answer()
    body,timeout=seen[0]
    assert body['max_tokens']==1024 and timeout<=5
    assert body['provider']['zdr'] and body['provider']['max_price']=={'prompt':0,'completion':0,'request':0}
    assert body['provider']['data_collection']=='deny' and body['provider']['require_parameters']
    assert 'response_format' not in body
    assert 'what_changed' in body['messages'][0]['content']
    assert 'secret' not in str(body) and 'tools' not in body and 'plugins' not in body

def test_one_repair_for_schema_only():
    seen=[]
    def transport(req,timeout): seen.append(json.loads(req.data)); return response('bad' if len(seen)==1 else json.dumps(answer()))
    assert configured_adapter('secret',transport)(facts())==answer()
    assert len(seen)==2 and seen[0]['provider']==seen[1]['provider']

def test_no_network_retry():
    seen=[]
    def transport(*a,**kw): seen.append(1); raise TimeoutError('secret')
    with pytest.raises(Exception): configured_adapter('secret',transport)(facts())
    assert len(seen)==1

def test_missing_credentials_raises():
    with pytest.raises(ValueError,match='credentials'): configured_adapter('')(facts())

@pytest.mark.parametrize('raw',[b'x'*32769,b'{}',b'{"error":{"message":"private"}}',b'not-json'])
def test_bad_provider_envelope_not_retried(raw):
    seen=[]
    def transport(*a,**kw):seen.append(1);return io.BytesIO(raw)
    with pytest.raises(ValueError):configured_adapter('secret',transport)(facts())
    assert len(seen)==1

@pytest.mark.parametrize('change',[{'status':'firing'},{'evidence_ids':['unknown']},{'headline':'x'*201},{'explanation':None}])
def test_invalid_output_two_calls_max(change):
    seen=[]
    def transport(*a,**kw):seen.append(1);return response(json.dumps(dict(answer(),**change)))
    with pytest.raises(ValueError):configured_adapter('secret',transport)(facts())
    assert len(seen)==2

def test_repair_uses_remaining_deadline():
    now=[0];seen=[]
    def transport(req,timeout):
        seen.append(timeout);now[0]+=6
        return response('bad' if len(seen)==1 else json.dumps(answer()))
    with pytest.raises(ValueError,match='deadline'):configured_adapter('secret',transport,clock=lambda:now[0])(facts())
    assert seen==[5,4]

def test_redirect_disabled():
    assert adapter.NoRedirect().redirect_request(None,None,302,'',{},'https://evil') is None

def test_blocked_body_read_has_wall_deadline_and_one_inflight(monkeypatch):
    import threading, time
    monkeypatch.setattr(adapter,'REQUEST_TIMEOUT',0.03)
    release=threading.Event(); entered=threading.Event(); calls=[]
    class Slow:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def read(self,size):entered.set();release.wait(2);return response(json.dumps(answer())).read()
    def transport(*args,**kwargs):calls.append(1);return Slow()
    callback=configured_adapter('secret',transport)
    started=time.monotonic()
    try:
        with pytest.raises(ValueError,match='deadline'):callback(facts())
        assert entered.is_set() and time.monotonic()-started<0.3
        with pytest.raises(ValueError):callback(facts())
        assert calls==[1]
    finally:release.set()
    callback._inflight.join(1)
    callback._transport=lambda *a,**kw:response(json.dumps(answer()))
    assert callback(facts())==answer()

def test_style_repair_includes_candidate_and_specific_constraints():
    seen=[]
    bad=dict(answer(),headline='WAN telemetry resolved')
    def transport(req,timeout):seen.append(json.loads(req.data));return response(json.dumps(bad if len(seen)==1 else answer()))
    assert configured_adapter('secret',transport)(facts())==answer()
    messages=seen[1]['messages']
    assert messages[-2]['role']=='assistant' and json.loads(messages[-2]['content'])==bad
    assert 'plain' in messages[-1]['content'] and 'lookback' in messages[-1]['content']

@pytest.mark.parametrize('fence',['```json\n','```\n'])
def test_single_whole_json_fence_accepted(fence):
    callback=configured_adapter('secret',lambda *a,**kw:response(fence+json.dumps(answer())+'\n```'))
    assert callback(facts())==answer()

@pytest.mark.parametrize('wrapped',['before\n```json\n{}\n```','```json\n{}\n```\nafter','```json\n```json\n{}\n```\n```'])
def test_fence_with_extra_text_is_rejected(wrapped):
    with pytest.raises(ValueError):configured_adapter('secret',lambda *a,**kw:response(wrapped))(facts())

def test_deliberate_free_model_choice():
    seen=[]
    def transport(req,timeout):seen.append(json.loads(req.data));return response(json.dumps(answer()))
    configured_adapter('secret',transport)(facts())
    assert seen[0]['model']=='example/primary:free'
    assert seen[0]['provider']['max_price']=={'prompt':0,'completion':0,'request':0}
    assert 'does not confirm new readings' in seen[0]['messages'][0]['content']

def test_race_primary_success_does_not_launch_secondary():
    calls=[]
    def primary(f): calls.append('primary');return answer()
    def secondary(f): calls.append('secondary');return answer()
    assert adapter.RacingSummary(primary,secondary,hedge_delay=.03,total_timeout=.2)(facts())==answer()
    assert calls==['primary']

def test_race_secondary_wins_when_primary_stalls_and_late_reply_is_isolated():
    import threading,time
    release=threading.Event();calls=[]
    def primary(f):calls.append('primary');release.wait(1);return dict(answer(),headline='Late answer')
    def secondary(f):calls.append('secondary');return answer()
    race=adapter.RacingSummary(primary,secondary,hedge_delay=.01,total_timeout=.1)
    try:
        assert race(facts())==answer()
        assert calls==['primary','secondary']
        assert race(facts())==answer()
        assert calls==['primary','secondary','secondary']
    finally:release.set()

def test_race_primary_failure_launches_secondary_immediately():
    def failed(f):raise ValueError('private')
    race=adapter.RacingSummary(failed,lambda f:answer(),hedge_delay=1,total_timeout=.1)
    assert race(facts())==answer()

def test_race_all_fail_or_stall_are_bounded():
    import threading,time
    release=threading.Event()
    def stalled(f):release.wait(1);return answer()
    race=adapter.RacingSummary(stalled,stalled,hedge_delay=.01,total_timeout=.04)
    started=time.monotonic()
    try:
        with pytest.raises(ValueError):race(facts())
        with pytest.raises(ValueError):race(facts())
        assert time.monotonic()-started<.2
    finally:release.set()

def test_explicit_secondary_model_preserves_privacy_and_extended_timeout():
    seen=[]
    def transport(req,timeout):seen.append((json.loads(req.data),timeout));return response(json.dumps(answer()))
    callback=configured_adapter('secret',transport,model='example/secondary:free',request_timeout=15,total_timeout=30)
    assert callback(facts())==answer()
    body,timeout=seen[0]
    assert body['model']=='example/secondary:free'
    assert body['provider']['zdr'] and body['provider']['data_collection']=='deny'
    assert body['provider']['max_price']=={'prompt':0,'completion':0,'request':0}
    assert body['provider']['sort']=='latency' and timeout<=15

def test_production_factory_enforces_disjoint_providers():
    race=adapter.production_summary('secret', summary_policy())._free
    assert [c._providers for c in race._callbacks]==[('provider-a',),('provider-b',)]
    assert [c._model for c in race._callbacks]==['example/primary:free','example/secondary:free']
    assert all(c._request_timeout==15 and c._total_timeout==30 for c in race._callbacks)

def test_paid_fallback_is_not_called_after_free_success():
    calls=[]
    def paid(f):calls.append(1);return answer()
    assert adapter.FallbackSummary(lambda f:answer(),paid)(facts())==answer()
    assert calls==[]

def test_paid_fallback_runs_once_after_free_failure():
    calls=[]
    def free(f):calls.append('free');raise ValueError('Unavailable')
    def paid(f):calls.append('paid');return answer()
    assert adapter.FallbackSummary(free,paid)(facts())==answer()
    assert calls==['free','paid']

def test_paid_failure_preserves_existing_fact_fallback():
    def unavailable(f):raise ValueError('Unavailable')
    with pytest.raises(ValueError):adapter.FallbackSummary(unavailable,unavailable)(facts())

def test_paid_request_and_correction_keep_zero_retention_and_price_ceiling():
    seen=[]
    def transport(req,timeout):seen.append(json.loads(req.data));return response('bad' if len(seen)==1 else json.dumps(answer()))
    paid=configured_adapter('secret',transport,model='example/paid',providers=('provider-c',),max_price={'prompt': .1, 'completion': .4, 'request': 0},request_timeout=10,total_timeout=15)
    assert paid(facts())==answer()
    assert len(seen)==2
    for body in seen:
        assert body['model']=='example/paid'
        assert body['provider']['only']==['provider-c']
        assert body['provider']['zdr'] and body['provider']['data_collection']=='deny'
        assert body['provider']['max_price']=={'prompt':.10,'completion':.40,'request':0}
        assert body['reasoning']=={'enabled':False}

def test_http_failure_category_is_sanitized(caplog):
    from urllib.error import HTTPError
    def transport(*a,**kw):raise HTTPError('https://private/?secret',429,'private message',None,None)
    with pytest.raises(ValueError,match='HTTP 429'):configured_adapter('secret',transport)(facts())
    assert 'HTTP 429' in caplog.text and 'private message' not in caplog.text and 'secret' not in caplog.text

def test_production_paid_config_is_bounded_and_explicit():
    callback=adapter.production_summary('secret', summary_policy())
    assert callback._paid._model=='example/paid'
    assert callback._paid._providers==('provider-c',)
    assert callback._paid._request_timeout==10 and callback._paid._total_timeout==15
    assert callback._paid._max_price=={'prompt':.10,'completion':.40,'request':0}

from tests.notifier_helpers import summary_policy
def configured_adapter(*args, **kwargs):
    kwargs.setdefault('model', 'example/primary:free')
    kwargs.setdefault('providers', ('provider-a',))
    return adapter.OpenRouterSummary(*args, **kwargs)


def test_custom_prompts_preserve_provider_policy_and_repair():
    seen=[]
    def transport(req,timeout):
        seen.append(json.loads(req.data))
        return response('bad' if len(seen)==1 else json.dumps(answer()))
    callback=configured_adapter('secret',transport,system_prompt='DIRT custom prose',correction_prompt='DIRT correction')
    assert callback(facts())==answer()
    assert seen[0]['messages'][0]['content'].startswith('DIRT custom prose\nRequired JSON schema: ')
    assert seen[1]['messages'][-1]['content']=='DIRT correction'
    assert seen[0]['provider']==seen[1]['provider']
    assert seen[0]['provider']['zdr'] is True


def test_custom_prompts_reach_every_production_branch():
    callback=adapter.production_summary('secret',summary_policy(),system_prompt='custom',correction_prompt='repair')
    branches=(*callback._free._callbacks,callback._paid)
    assert len(branches)==3
    assert all(branch._system_prompt=='custom' and branch._correction_prompt=='repair' for branch in branches)
