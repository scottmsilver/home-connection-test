import sqlite3
import pytest
from connection_monitoring import dirt_report as report


def event(**changes):
    values = dict(run_id='a'*32, phase='starting', outcome='pending', observed_at=1000, facts={'connection':'Link-A','checks':{'internet':True}})
    values.update(changes)
    return report.make_event(**values)


class Telegram:
    def __init__(self): self.messages=[]
    def send_message(self,text): self.messages.append(text); return 23


def test_event_and_receipt_contract(tmp_path):
    item=event(); transport=Telegram(); db=tmp_path/'reports.db'
    receipt=report.deliver_report(item,db,transport,clock=lambda:1001)
    assert set(item)=={'version','event_id','run_id','case_id','revision','site','phase','outcome','observed_at','facts'}
    assert set(receipt)=={'version','event_id','case_id','payload_sha256','message_id','accepted_at'}
    assert report.validate_receipt(receipt,item)
    assert report.deliver_report(item,db,transport,clock=lambda:100000)==receipt
    assert len(transport.messages)==1
    assert receipt['accepted_at']==1001
    assert 'DIRT' in transport.messages[0] and 'internet: true' in transport.messages[0]
    assert sqlite3.connect(db).execute("select name from sqlite_master where type='table'").fetchall()==[('dirt_reports',)]
    with pytest.raises(ValueError): report.deliver_report(event(facts={'connection':'Link-B'}),db,transport,clock=lambda:1001)


def test_expired_start_and_terminal_age(tmp_path):
    with pytest.raises(ValueError): report.deliver_report(event(),tmp_path/'r',Telegram(),clock=lambda:1301)
    item=event(phase='finished',outcome='failed')
    assert report.deliver_report(item,tmp_path/'r',Telegram(),clock=lambda:100000)['message_id']==23


def test_intent_committed_before_send_failure(tmp_path):
    db=tmp_path/'r'
    class Broken:
        def send_message(self,text):
            row=sqlite3.connect(db).execute('select event_json,receipt_json from dirt_reports').fetchone()
            assert row[0] and row[1] is None
            raise RuntimeError('failure')
    with pytest.raises(RuntimeError): report.deliver_report(event(),db,Broken(),clock=lambda:1000)


@pytest.mark.parametrize('changes',[{'run_id':'X'*32},{'revision':True},{'revision':2**64},{'observed_at':True},{'observed_at':float('inf')},{'site':'1.2.3.4'},{'phase':'starting','outcome':'passed'},{'phase':'recovery_update','outcome':'passed'},{'facts':{}},{'facts':{'log':'secret'}},{'facts':{'connection':'https://secret'}},{'facts':{'elapsed_seconds':float('nan')}},{'facts':{'checks':{'internet':object()}}}])
def test_invalid_events(changes):
    with pytest.raises(ValueError): event(**changes)


def test_lifecycle_model_facts_and_plain_rendering():
    seen=[]
    def summarize(facts):
        seen.append(facts)
        return dict(status=facts['status'],headline='Link-A checks complete',explanation='Restoration checks passed.',what_changed='The earlier interruption is still recorded.',evidence_ids=['checks'])
    item=event(phase='recovery_update',outcome='interrupted',facts={'checks':{'restoration':True}})
    text=report.format_report(item,summarize,previous=event(phase='interrupted',outcome='interrupted'))
    assert seen[0]['status']=='recovery_update/interrupted'
    assert seen[0]['history_available'] is True
    assert seen[0]['facts']==item['facts']
    assert 'interrupted' in text and 'passed' not in text.split('\n')[0]
    assert not any(ord(c)>0xffff for c in text)


def test_receipt_rejects_nonpositive_bool_and_mismatch(tmp_path):
    item=event(); receipt=report.deliver_report(item,tmp_path/'r',Telegram(),clock=lambda:1001)
    for key,value in [('message_id',True),('message_id',0),('accepted_at',float('nan')),('payload_sha256','0'*64)]:
        assert not report.validate_receipt(dict(receipt,**{key:value}),item)


def test_announcement_receipt_freshness_and_future_bound(tmp_path):
    item=event(); receipt=report.deliver_report(item,tmp_path/'r',Telegram(),clock=lambda:1001)
    assert report.validate_announcement_receipt(receipt,item,now=1301)
    assert not report.validate_announcement_receipt(receipt,item,now=1302)
    assert report.validate_announcement_receipt(receipt,item,now=996)
    assert not report.validate_announcement_receipt(receipt,item,now=995)
    assert not report.validate_announcement_receipt(receipt,event(phase='finished',outcome='passed'),now=1001)


def test_estate_selection_case_summary_and_reasons():
    item=event(facts={'selection':['Site-A','Site-B'],'cases':[{'name':'Case-A','site':'Site-A','connection':'Link-A','outcome':'failed','checks':{'internet':None},'restoration':{'restoration':True},'reason_codes':['health_unavailable']}]*4})
    assert len(item['facts']['cases'])==4


def test_generation_expiry_prevents_transport(tmp_path):
    times=iter([1000,1301]); transport=Telegram()
    with pytest.raises(ValueError): report.deliver_report(event(),tmp_path/'r',transport,clock=lambda:next(times))
    assert transport.messages==[]


def test_existing_alert_database_is_unchanged(tmp_path):
    db=tmp_path/'alerts'; connection=sqlite3.connect(db)
    connection.execute('create table history(subject text)');connection.execute("insert into history values ('old')");connection.commit()
    with pytest.raises(ValueError): report.deliver_report(event(),db,Telegram(),clock=lambda:1000)
    assert connection.execute('select * from history').fetchall()==[('old',)]
    assert connection.execute("select name from sqlite_master where type='table'").fetchall()==[('history',)]


def test_bad_transport_receipt_not_recorded(tmp_path):
    class Bad:
        def send_message(self,text): return True
    db=tmp_path/'r'
    with pytest.raises(ValueError): report.deliver_report(event(),db,Bad(),clock=lambda:1000)
    assert sqlite3.connect(db).execute('select receipt_json from dirt_reports').fetchone()==(None,)


def test_numeric_prefix_names_and_health_issue_facts():
    item=event(site='42-demo',facts={'selection':['42-demo'],'case':{'name':'42-case','site':'42-demo'},'health_issues':[{'check_id':'42-check.internet','available':True,'healthy':False,'observed_at':1000,'observations':{'age_seconds':12,'ready':False,'status':'unavailable'}}]})
    assert item['site']=='42-demo'
    assert 'ready: false' in report.format_report(item)


@pytest.mark.parametrize('issue',[{'check_id':'health','available':1,'healthy':False,'observed_at':1000,'observations':{}},{'check_id':'health','available':True,'healthy':False,'observed_at':-1,'observations':{}},{'check_id':'health','available':True,'healthy':False,'observed_at':1000,'observations':{'raw':'/ssh/secret'}}])
def test_hostile_health_issues_rejected(issue):
    with pytest.raises(ValueError): event(facts={'health_issues':[issue]})


@pytest.mark.parametrize('now',[True,float('nan'),float('inf')])
def test_announcement_rejects_invalid_clock(tmp_path,now):
    item=event();receipt=report.deliver_report(item,tmp_path/'r',Telegram(),clock=lambda:1000)
    assert not report.validate_announcement_receipt(receipt,item,now=now)


@pytest.mark.parametrize('facts',[{'schedule':{'due_at':-1}},{'window':{'starts_at':-1}},{'selection':['x']*33},{'health_issues':[{'check_id':'health','available':True,'healthy':None,'observed_at':1000,'observations':{'nested':{'nested':{'nested':{'nested':True}}}}}]},{'cases':[{'name':'x','checks':{'internet':True}}]*33}])
def test_fact_bounds(facts):
    with pytest.raises(ValueError): event(facts=facts)


@pytest.mark.parametrize('reverse',[False,True])
@pytest.mark.parametrize('use_model',[False,True])
def test_critical_checks_survive_large_health_details(reverse,use_model):
    facts={'health_issues':[{'check_id':'health-'+str(n),'available':True,'healthy':False,'observed_at':1000,'observations':{'measurement_name_'+str(k):100000+k for k in range(10)}} for n in range(20)],'checks':{'fault':True,'alert':True,'recovery':False,'cleanup':False},'restoration':{'restoration':False}}
    if reverse: facts=dict(reversed(list(facts.items())))
    item=event(phase='finished',outcome='recovery_unverified',facts=facts)
    def summarize(facts):
        return dict(status=facts['status'],headline='A'*120,explanation='&'*400,what_changed='<'*250,evidence_ids=['checks'])
    text=report.format_report(item,summarize if use_model else None)
    assert 'checks: recovery: false' in text and 'checks: cleanup: false' in text
    assert 'restoration: restoration: false' in text
    assert text.index('checks: recovery: false')<text.index('health_issues:')
    assert 'Additional recorded details omitted' in text
    assert len(text)<=4096
    assert not text.endswith(('&','&amp','&lt'))


def test_four_case_checks_and_identity_render_first():
    item=event(phase='finished',outcome='failed',case_id='b'*32,facts={'cases':[{'name':'Case-'+str(i),'site':'Site-A','connection':'Link-A','outcome':'failed','checks':{'fault':True,'alert':True,'recovery':False,'cleanup':False},'restoration':{'restoration':False}} for i in range(4)]})
    text=report.format_report(item)
    assert 'Case: '+('b'*32) in text
    assert text.count('checks: recovery: false')==4
    assert text.count('restoration: restoration: false')==4
    assert len(text)<=4096


def test_essential_overflow_rejected_before_send_without_receipt(tmp_path):
    item=event(phase='finished',outcome='failed',facts={'cases':[{'name':'Case-'+str(i),'checks':{'fault':True,'alert':True,'recovery':False,'cleanup':False},'restoration':{'restoration':False}} for i in range(32)]})
    transport=Telegram();db=tmp_path/'r'
    with pytest.raises(ValueError,match='essential'): report.deliver_report(item,db,transport,clock=lambda:1000)
    assert transport.messages==[]
    assert sqlite3.connect(db).execute('select receipt_json from dirt_reports').fetchone()==(None,)


def test_critical_results_precede_maximally_escaped_model_prose():
    item=event(phase='finished',outcome='failed',facts={'checks':{'recovery':False},'restoration':{'restoration':False}})
    def summarize(facts):
        return dict(status=facts['status'],headline='&'*120,explanation='&'*400,what_changed='<'*250,evidence_ids=['checks'])
    text=report.format_report(item,summarize)
    assert text.index('checks: recovery: false')<text.index('&amp;')
    assert 'restoration: restoration: false' in text
    assert len(text)<=4096 and '&amp;' in text
