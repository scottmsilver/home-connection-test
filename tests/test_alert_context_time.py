import json
from connection_monitoring.alert_summary import facts_for_model, format_alert, valid_summary
from connection_monitoring.alert_delivery import DeliveryService
class Telegram:
    def __init__(self): self.texts=[]
    def send_message(self,text): self.texts.append(text); return len(self.texts)


def alert(status='firing',name='Internet'):
    return {'status':status,'startsAt':'2026-10-13T16:00:00Z','endsAt':'2026-10-13T17:00:00Z' if status=='resolved' else '',
            'labels':{'site':'house','alertname':name},'annotations':{'summary':'Last read at 2026-10-13T15:59:00Z'},'values':{'A':10}}


def test_model_and_message_have_local_times_and_no_utc():
    a=alert(); facts=facts_for_model(a)
    assert '9:00 AM PDT' in facts['timestamps']['startsAt']
    assert '2026-10-13T' not in json.dumps(facts)
    assert '8:59 AM PDT' in format_alert(a).text
    candidate={'status':'firing','headline':'Internet warning','explanation':'Reported at 16:00 UTC.','what_changed':'A warning was reported.','next_step':None,'evidence_ids':['status']}
    assert not valid_summary(candidate,facts)


def test_history_survives_restart_and_tracks_ongoing_incident(tmp_path):
    path=tmp_path/'history.db';now=[1791907200.];seen=[];telegram=Telegram()
    service=DeliveryService(path,telegram,summarize=lambda f:seen.append(f),clock=lambda:now[0])
    service(alert());now[0]+=601
    update=alert();update['values']['A']=20;service(update)
    service.db.close();now[0]+=601
    service=DeliveryService(path,telegram,summarize=lambda f:seen.append(f),clock=lambda:now[0])
    service(alert(name='Disk'));now[0]+=601
    service(alert('resolved'))
    facts=seen[-1]
    assert len(facts['incident_history']['updates'])==2
    assert [i['values']['A'] for i in facts['incident_history']['updates']]==[10,20]
    assert facts['incident_history']['complete'] is False
    assert facts['related_alerts']['updates'][0]['alertname']=='Disk'
    assert 'T16:00:00Z' not in json.dumps(facts)
    assert facts['transition']=='recovered'


def test_duration_uses_incident_times_and_handles_daylight_saving():
    from connection_monitoring.alert_time import local_time
    a=alert()
    now=1791907200.+7200
    facts=facts_for_model(a,{'now':now})
    assert facts['timing']['duration']=='2 hours'
    assert facts['timing']['duration_seconds']==7200
    done=facts_for_model(alert('resolved'),{'now':now})
    assert done['timing']['duration']=='1 hour'
    assert 'PST' in local_time('2026-12-01T16:00:00Z')
    assert 'PDT' in local_time('2026-07-01T16:00:00Z')
    assert facts_for_model(a,{'now':1})['timing']['duration'] is None


def test_raw_utc_without_date_is_not_guessed_or_displayed():
    a=alert();a['annotations']['summary']='Last received at 16:00 utc'
    message=format_alert(a,context={'now':1791914400.}).text
    assert '16:00' not in message and ' utc' not in message
    assert 'Started:' in message and 'Active for:' in message and '2 hours' in message


def test_unknown_start_does_not_merge_incident_history(tmp_path):
    now=[1791907200.];seen=[]
    service=DeliveryService(tmp_path/'db',Telegram(),summarize=lambda f:seen.append(f),clock=lambda:now[0])
    a=alert();a['startsAt']='bad';service(a);now[0]+=601;service(a)
    assert seen[-1]['incident_comparable'] is False
    assert seen[-1]['incident_history']['updates']==[]
    assert seen[-1]['timing']['duration'] is None
