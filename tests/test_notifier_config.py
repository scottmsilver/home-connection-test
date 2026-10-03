import copy
import pytest
from tests.notifier_helpers import notifier_config
from connection_monitoring import alert_server as server
from connection_monitoring import openrouter_summary as adapter

@pytest.mark.parametrize('change', [
 lambda c:c['secrets'].update(telegram_token='literal-secret'),
 lambda c:c.pop('delivery_database'),
 lambda c:c['renderer'].update(origin='https://evil.example'),
 lambda c:c['renderer'].update(panels=[]),
 lambda c:c['summary'].update(zdr=False),
 lambda c:c['summary'].update(data_collection='allow'),
 lambda c:c['summary']['free'][0].update(max_price=dict(prompt=.01,completion=0,request=0)),
 lambda c:c['summary']['free'][1].update(providers=['provider-a']),
 lambda c:c['summary']['free'][0].pop('model'),
 lambda c:c['summary']['free'][0].update(total_timeout=0),
])
def test_invalid_runtime_rejected_before_database(tmp_path, change):
    config=notifier_config(tmp_path); change(config)
    with pytest.raises(ValueError): server.validate_runtime_config(config)
    assert not (tmp_path/'delivery.sqlite').exists()
    assert not (tmp_path/'queue.sqlite').exists()

def test_valid_explicit_runtime(tmp_path):
    config=notifier_config(tmp_path)
    assert server.validate_runtime_config(config)==config

def test_adapter_requires_explicit_model():
    with pytest.raises(TypeError): adapter.OpenRouterSummary('secret')

def test_policy_cannot_relax_retention(tmp_path):
    policy=notifier_config(tmp_path)['summary']; policy['zdr']=False
    with pytest.raises(ValueError): adapter.production_summary('key',policy)

@pytest.mark.parametrize('kwargs', [dict(model=''), dict(model='example/free:free',max_price=dict(prompt=.1,completion=0,request=0)), dict(model='example/paid',providers=[]), dict(model='example/paid',total_timeout=-1)])
def test_adapter_rejects_invalid_explicit_policy(kwargs):
    with pytest.raises(ValueError): adapter.OpenRouterSummary('key',**kwargs)

def test_cli_notifier_requires_config_before_effects(monkeypatch):
    from connection_monitoring.cli import main
    monkeypatch.setattr(server,'main',lambda c:pytest.fail('must validate first'))
    with pytest.raises(SystemExit): main(['notifier'])

@pytest.mark.parametrize('field',['request_timeout','total_timeout'])
def test_enormous_integer_deadline_rejected_safely(tmp_path,field):
    config=notifier_config(tmp_path);config['summary']['free'][0][field]=10**1000
    with pytest.raises(ValueError):server.validate_runtime_config(config)
    assert list(tmp_path.iterdir())==[]

@pytest.mark.parametrize('field',['prompt','completion','request'])
def test_enormous_price_rejected_safely(tmp_path,field):
    config=notifier_config(tmp_path);config['summary']['paid']['max_price'][field]=10**1000
    with pytest.raises(ValueError):server.validate_runtime_config(config)

def test_cli_validate_rejects_invalid_nested_notifier(tmp_path):
    import json
    from connection_monitoring.cli import main
    config=notifier_config(tmp_path);config['renderer']['panels']=[]
    path=tmp_path/'config.json';path.write_text(json.dumps(dict(version=1,notifier=config)))
    with pytest.raises(SystemExit):main(['--config',str(path),'validate'])
    assert not (tmp_path/'delivery.sqlite').exists()

def test_cli_validate_does_not_require_secret_values(tmp_path,monkeypatch):
    import json
    from connection_monitoring.cli import main
    config=notifier_config(tmp_path)
    for name in config['secrets'].values():monkeypatch.delenv(name,raising=False)
    path=tmp_path/'config.json';path.write_text(json.dumps(dict(version=1,notifier=config)))
    assert main(['--config',str(path),'validate'])==0
    assert not (tmp_path/'delivery.sqlite').exists()

@pytest.mark.parametrize('entrypoint',['runtime_delivery','main'])
def test_runtime_invalid_policy_precedes_secret_lookup_and_effects(tmp_path,monkeypatch,entrypoint):
    config=notifier_config(tmp_path);config['summary']['zdr']=False
    monkeypatch.setattr(server,'resolve_secrets',lambda c:pytest.fail('secret lookup before validation'))
    with pytest.raises(ValueError):getattr(server,entrypoint)(config)
    assert list(tmp_path.iterdir())==[]

@pytest.mark.parametrize('change',[
 lambda c:c.update(delivery_database='relative.sqlite'),
 lambda c:c.update(delivery_database=c['queue_database']),
 lambda c:c['queue'].update(capacity=10**1000),
 lambda c:c['listen'].update(host='0.0.0.0'),
 lambda c:c['renderer'].update(dashboard='../../other'),
 lambda c:c['summary']['free'][1].update(providers=['PROVIDER-A']),
])
def test_explicit_configuration_bounds(tmp_path,change):
    config=notifier_config(tmp_path);change(config)
    with pytest.raises(ValueError):server.validate_runtime_config(config)
