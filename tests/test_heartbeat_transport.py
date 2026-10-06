import base64
import json
import os
from pathlib import Path
import pytest
from connection_monitoring.heartbeat_transport import HeartbeatClient, HeartbeatError
from tests.test_heartbeat import snapshot

NOW = 1791288000
CONFIG = {'project_id':'example-project', 'site_id':'example-site', 'collection':'siteHeartbeats',
          'api_key':'public-api-identifier', 'expected_uid':'machine-example', 'service_keys':list(snapshot()['services'])}


def jwt(**overrides):
    claims = {'aud':CONFIG['project_id'], 'iss':'https://securetoken.google.com/'+CONFIG['project_id'],
              'sub':CONFIG['expected_uid'], 'exp':NOW+3600, 'firebase':{'sign_in_provider':'custom'}}
    claims.update(overrides)
    value=base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip('=')
    return 'header.'+value+'.signature'


def setup(tmp_path):
    tmp_path.chmod(0o700)
    creds=tmp_path/'credential.json'; creds.write_text(json.dumps({'version':1,'project_id':CONFIG['project_id'],
        'uid':CONFIG['expected_uid'],'refresh_token':'initial-sensitive-refresh'}));creds.chmod(0o600)
    return creds,tmp_path/'token.json'


def test_refresh_and_publish_are_bounded_and_rotate_private_credential(tmp_path):
    creds,cache=setup(tmp_path); calls=[]
    def request(url,body,headers,timeout):
        calls.append((url,body,headers,timeout))
        if 'securetoken' in url:
            return {'id_token':jwt(),'user_id':CONFIG['expected_uid'],'refresh_token':'rotated-sensitive-refresh'}
        return {'commitTime':'2026-10-06T12:00:00Z'}
    client=HeartbeatClient(CONFIG,creds,cache,request=request,clock=lambda:NOW)
    assert client.publish(snapshot()) == {'accepted':True}
    assert len(calls)==2 and all(0<t<=10 for _,_,_,t in calls)
    body=json.loads(calls[1][1]);assert len(body['writes'])==1
    assert body['writes'][0]['updateTransforms'][0]['setToServerValue']=='REQUEST_TIME'
    assert json.loads(creds.read_text())['refresh_token']=='rotated-sensitive-refresh'
    assert (creds.stat().st_mode & 0o777)==(cache.stat().st_mode & 0o777)==0o600
    client.publish(snapshot());assert len(calls)==3 # cached ID token, one write


@pytest.mark.parametrize('claims', [{'aud':'other-project'},{'sub':'other-machine'}, {'exp':NOW-1},
    {'exp':True},{'iss':'https://evil.example'}, {'firebase':{'sign_in_provider':'google.com'}}])
def test_wrong_identity_token_never_writes(tmp_path,claims):
    creds,cache=setup(tmp_path);calls=[]
    def request(url,*args):
        calls.append(url)
        return {'id_token':jwt(**claims),'user_id':CONFIG['expected_uid'],'refresh_token':'safe'}
    with pytest.raises(HeartbeatError,match='identity'):
        HeartbeatClient(CONFIG,creds,cache,request=request,clock=lambda:NOW).publish(snapshot())
    assert len(calls)==1


def test_response_uid_is_bound_and_secrets_are_not_in_error(tmp_path):
    creds,cache=setup(tmp_path)
    def request(*args):return {'id_token':jwt(),'user_id':'other','refresh_token':'SECRET-MUST-NOT-PRINT'}
    with pytest.raises(HeartbeatError) as error: HeartbeatClient(CONFIG,creds,cache,request=request,clock=lambda:NOW).publish(snapshot())
    assert 'SECRET' not in str(error.value)


@pytest.mark.parametrize('kind',['world_readable','symlink','unsafe_parent'])
def test_unsafe_secret_paths_rejected_before_http(tmp_path,kind):
    creds,cache=setup(tmp_path)
    if kind=='world_readable':creds.chmod(0o644)
    elif kind=='unsafe_parent':tmp_path.chmod(0o755)
    else:
        alias=tmp_path/'alias';alias.symlink_to(creds);creds=alias
    with pytest.raises(HeartbeatError):
        HeartbeatClient(CONFIG,creds,cache,request=lambda *args:pytest.fail('HTTP must not run'),clock=lambda:NOW).publish(snapshot())


def test_failure_never_leaks_provider_message_or_retries(tmp_path):
    creds,cache=setup(tmp_path);calls=[]
    def request(*args):calls.append(args);raise RuntimeError('SECRET token in provider details')
    with pytest.raises(HeartbeatError) as error:
        HeartbeatClient(CONFIG,creds,cache,request=request,clock=lambda:NOW).publish(snapshot())
    assert 'SECRET' not in str(error.value) and len(calls)==1


def test_malformed_cache_recovers_by_refresh(tmp_path):
    creds,cache=setup(tmp_path);cache.write_text('not-json');cache.chmod(0o600);calls=[]
    def request(url,*args):
        calls.append(url)
        return {'id_token':jwt(),'user_id':CONFIG['expected_uid'],'refresh_token':'refreshed'} if 'securetoken' in url else {'commitTime':'x'}
    assert HeartbeatClient(CONFIG,creds,cache,request=request,clock=lambda:NOW).publish(snapshot())=={'accepted':True}
    assert len(calls)==2


def test_insecure_cache_is_not_silently_adopted(tmp_path):
    creds,cache=setup(tmp_path);cache.write_text('{}');cache.chmod(0o644)
    with pytest.raises(HeartbeatError,match='file_invalid'):
        HeartbeatClient(CONFIG,creds,cache,request=lambda *args:pytest.fail('HTTP must not run'),clock=lambda:NOW).publish(snapshot())
