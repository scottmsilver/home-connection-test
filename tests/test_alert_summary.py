import sys, json
from pathlib import Path
import pytest
from connection_monitoring import alert_summary as summary

@pytest.fixture
def alert():
    return {'status':'firing','startsAt':'2026-10-03T10:00:00Z','labels':{'site':'sample-site','alertname':'WAN quality missing','wan':'examplelink'},'annotations':{'summary':'Old source readings','estate_context':json.dumps({'kind':'missing','measurement':'readings','sample_window':'15m','pending_duration':'2m','limits':['Missing readings do not prove an outage.']})},'values':{'Condition0':1,'A':None},'generatorURL':'https://grafana.example/alert'}

def prose(facts):
    return dict(status=facts['status'],headline='Readings need a look',explanation='ExampleLink measurements have become unavailable.',what_changed='This is the first recorded update.',evidence_ids=['status'])

@pytest.mark.parametrize('status', ['firing','resolved'])
def test_original_model_prose_for_every_status(alert,status):
    alert['status']=status
    result=summary.format_alert(alert,prose)
    assert result.used_llm
    assert 'ExampleLink measurements have become unavailable.' in result.text
    assert 'What changed' in result.text and 'Old source readings' in result.text
    assert '⚠' not in result.text and '✅' not in result.text

@pytest.mark.parametrize('status,label', [('firing','Warning'), ('resolved','Cleared')])
def test_heading_does_not_repeat_model_status_prefix(alert,status,label):
    alert['status']=status
    result=summary.format_alert(alert,lambda facts:dict(prose(facts),headline=label+': ExampleLink readings'))
    assert result.used_llm
    assert result.text.splitlines()[0] == '<b>sample-site · '+label+': ExampleLink readings</b>'

def test_evidence_privacy_types_and_previous(alert):
    alert['labels']['token']='secret'; alert['annotations']['other']='secret'
    facts=summary.facts_for_model(alert,{'previous':{'status':'firing','values':{'Condition0':0}},'transition':'continuing','differences':{'Condition0':{'before':0,'after':1,'delta':1}}})
    assert 'summary_options' not in facts and 'secret' not in str(facts)
    assert facts['timestamps']['startsAt']==alert['startsAt']
    assert facts['numeric_evidence'][0]['kind']=='condition_flag'
    assert facts['numeric_evidence'][1]['value'] is None
    assert all('unit' not in item for item in facts['numeric_evidence'])
    assert facts['previous']['status']=='firing' and facts['rule']['kind']=='missing'
    assert 'status' in facts['evidence_ids']

@pytest.mark.parametrize('change',[{'status':'resolved'},{'evidence_ids':['unknown']},{'headline':'x'*201},{'explanation':None},{'next_step':42}])
def test_invalid_structured_output_falls_back(alert,change):
    result=summary.format_alert(alert,lambda facts:dict(prose(facts),**change))
    assert not result.used_llm and 'Summary unavailable' in result.text


def test_html_links_details_and_length(alert):
    alert['annotations']['summary']='<b>&' * 3000
    alert['labels']['site']='<a href="evil">x</a>'
    alert['generatorURL']='javascript:evil'
    def markup(facts):return dict(prose(facts),explanation='<b>Check readings & names</b>')
    result=summary.format_alert(alert,markup)
    assert result.used_llm and '&lt;b&gt;' in result.text and '&amp;' in result.text
    assert '<a href="evil">' not in result.text and 'javascript:' not in result.text
    assert len(result.text)<=4096 and result.text.endswith('</blockquote>')


@pytest.mark.parametrize('status', ['firing', 'resolved'])
@pytest.mark.parametrize('summarize', [None, prose])
def test_grafana_link_remains_visible_outside_truncated_details(alert, status, summarize):
    alert['status'] = status
    alert['annotations']['summary'] = '<long original facts & measurements>' * 1000
    alert['generatorURL'] = 'https://grafana.example/alerting/grafana/example/view?orgId=1&view=details'
    result = summary.format_alert(alert, summarize)
    assert len(result.text) <= 4096
    assert result.text.endswith(
        '</blockquote>\n\n<a href="https://grafana.example/alerting/grafana/example/view?orgId=1&amp;view=details">View alert details in Grafana</a>')


@pytest.mark.parametrize('status', ['firing', 'resolved'])
@pytest.mark.parametrize('summarize', [None, prose])
def test_graph_link_survives_queue_and_takes_priority_over_alert_page(alert, status, summarize):
    from connection_monitoring.alert_queue import selected_alert
    alert['status'] = status
    alert['annotations']['summary'] = '<long original facts & measurements>' * 1000
    alert['panelURL'] = 'https://grafana.example/d/alert-graphs/graphs?orgId=1&viewPanel=panel-5&secret=discard'
    result = summary.format_alert(selected_alert(alert), summarize)
    assert len(result.text) <= 4096
    assert result.text.endswith('</blockquote>\n\n<a href="https://grafana.example/d/alert-graphs/graphs?viewPanel=panel-5">View graph in Grafana</a>')
    assert 'https://grafana.example/alert' not in result.text
    assert 'secret=discard' not in result.text


@pytest.mark.parametrize('url', ['javascript:evil', 'https://user:password@grafana.example/d/graph'])
def test_invalid_graph_link_falls_back_to_alert_details(alert, url):
    alert['panelURL'] = url
    result = summary.format_alert(alert)
    assert result.text.endswith('<a href="https://grafana.example/alert">View alert details in Grafana</a>')
    assert 'javascript:' not in result.text and 'password' not in result.text

def test_recovery_fallback_marks_historical_annotation(alert):
    alert['status']='resolved'
    result=summary.format_alert(alert,lambda f:(_ for _ in ()).throw(TimeoutError('secret')))
    assert 'Historical firing source facts' in result.text and 'Summary unavailable' in result.text
    assert 'secret' not in result.text

@pytest.mark.parametrize('raw',['not json','[]','x'*4097,json.dumps({'threshold':True,'limits':['x'*301]})])
def test_malformed_context_unspecified(alert,raw):
    alert['annotations']['estate_context']=raw
    assert summary.facts_for_model(alert)['rule']=={}

def test_verified_measurement_does_not_promote_classic_condition(alert):
    alert['annotations']['estate_context']=json.dumps({'measurement_ref':'A0','unit':'percent'})
    alert['values']={'A0':80,'A':70}
    assert all(item['kind']=='unknown_reference' and 'unit' not in item for item in summary.facts_for_model(alert)['numeric_evidence'])

def test_plain_prose_field_limits(alert):
    for field,length in [('headline',121),('explanation',401),('what_changed',251),('next_step',201)]:
        result=summary.format_alert(alert,lambda facts:dict(prose(facts),**{field:'x'*length}))
        assert not result.used_llm

@pytest.mark.parametrize('word',['WAN','UPS','telemetry','collector','firing','resolved','null','schema','evidence_ids'])
def test_model_visible_prose_rejects_internal_jargon(alert,word):
    result=summary.format_alert(alert,lambda facts:dict(prose(facts),headline='Check '+word))
    assert not result.used_llm

def test_original_plain_prose_accepts_names_containing_word_fragments(alert):
    result=summary.format_alert(alert,lambda facts:dict(prose(facts),headline='Swan provider readings missing'))
    assert result.used_llm

@pytest.mark.parametrize('raw',[json.dumps({'threshold':10**400}),'['*1800+']'*1800])
def test_hostile_optional_context_still_delivers_original_facts(alert,raw):
    alert['annotations']['estate_context']=raw
    result=summary.format_alert(alert)
    assert 'Old source readings' in result.text and 'Summary unavailable' in result.text
    assert summary.facts_for_model(alert)['rule']=={}

def test_deep_optional_context_recursion_is_contained(alert,monkeypatch):
    def exhausted(raw):raise RecursionError('nested context')
    monkeypatch.setattr(summary.json,'loads',exhausted)
    assert 'Old source readings' in summary.format_alert(alert).text

def test_current_readings_require_verified_available_measurement(alert):
    assert summary.facts_for_model(alert)['current_readings_provided'] is False
    alert['annotations']['estate_context']=json.dumps({'measurement_ref':'Alarm','unit':'percent'})
    alert['values']={'Alarm':None,'Condition':1}
    assert summary.facts_for_model(alert)['current_readings_provided'] is False
    alert['values']['Alarm']=45
    assert summary.facts_for_model(alert)['current_readings_provided'] is True

@pytest.mark.parametrize('field,text',[
    ('headline','Clear active warning on test site'),
    ('what_changed','Condition0 dropped from 1 to 0.'),
    ('what_changed','First accepted update: condition flag became active.'),
    ('explanation','The transition is first_recorded.'),
    ('next_step','None'),
])
def test_editorial_regressions_are_rejected_for_model_correction(alert,field,text):
    facts=summary.facts_for_model(alert)
    candidate=dict(prose(facts),**{field:text})
    assert not summary.valid_summary(candidate,facts)

def test_natural_recovery_headline_and_real_measurements_remain_valid(alert):
    facts=summary.facts_for_model(alert)
    candidate=dict(prose(facts),headline='ExampleLink test warning cleared',what_changed='Loss fell from 10% to 0%.')
    assert summary.valid_summary(candidate,facts)
