"""Direct Firestore writes with Firebase Auth credentials, never IAM site keys."""
import base64
import json
import os
from pathlib import Path
import stat
import tempfile
import time
from urllib.parse import urlencode
from urllib.request import Request, build_opener, HTTPRedirectHandler
from .heartbeat import validate_snapshot, commit_body, PROJECT, IDENTIFIER, COLLECTION

AUTH_BASE = 'https://securetoken.googleapis.com/v1/token'
FIRESTORE_BASE = 'https://firestore.googleapis.com/v1'
HTTP_TIMEOUT = 10
MAX_BODY = 65536


class HeartbeatError(ValueError):
    """Sanitized operational error; provider responses never appear in logs."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs): return None


def request_json(url, body, headers, timeout=HTTP_TIMEOUT):
    try:
        with build_opener(NoRedirect()).open(Request(url, data=body, headers=headers, method='POST'), timeout=timeout) as response:
            raw = response.read(MAX_BODY + 1)
        if len(raw) > MAX_BODY: raise ValueError('oversized response')
        result = json.loads(raw)
        if type(result) is not dict: raise ValueError('invalid response')
        return result
    except Exception:
        raise HeartbeatError('request_unavailable') from None


def private_parent(path):
    path = Path(path)
    if not path.is_absolute(): raise HeartbeatError('credential_path_invalid')
    try:
        info = path.parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise ValueError('unsafe parent')
    except (OSError, ValueError): raise HeartbeatError('credential_path_invalid') from None
    return path


def read_private(path, missing=False, malformed=False):
    path = private_parent(path)
    try:
        fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        if missing: return None
        raise HeartbeatError('credential_file_unavailable') from None
    except OSError: raise HeartbeatError('credential_file_invalid') from None
    try:
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077 or info.st_size > MAX_BODY:
                raise HeartbeatError('credential_file_invalid')
            raw = stream.read(MAX_BODY + 1)
        value = json.loads(raw)
        if type(value) is not dict: raise ValueError('invalid JSON')
        return value
    except HeartbeatError:
        raise
    except (ValueError, UnicodeError):
        if malformed: return None
        raise HeartbeatError('credential_file_invalid') from None


def write_private(path, value):
    path = private_parent(path)
    # Existing symlinks/world-readable files are never silently adopted.
    if path.exists() or path.is_symlink(): read_private(path, malformed=True)
    temp = None
    try:
        fd, temp = tempfile.mkstemp(prefix='.heartbeat-', dir=str(path.parent))
        with os.fdopen(fd, 'w') as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(value, stream, allow_nan=False, separators=(',', ':'))
            stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
        os.replace(temp, str(path)); temp = None
    except (OSError, ValueError): raise HeartbeatError('credential_write_unavailable') from None
    finally:
        if temp is not None:
            try: os.unlink(temp)
            except OSError: pass


def validate_config(raw):
    keys = {'project_id', 'site_id', 'collection', 'api_key', 'expected_uid'}
    if type(raw) is not dict or set(raw) not in (keys|{'service_keys'},keys|{'check_keys','check_revision'}): raise HeartbeatError('client_configuration_invalid')
    for key, pattern in [('project_id', PROJECT), ('site_id', IDENTIFIER), ('collection', COLLECTION)]:
        if type(raw[key]) is not str or not pattern.fullmatch(raw[key]): raise HeartbeatError('client_configuration_invalid')
    for key, maximum in [('api_key', 256), ('expected_uid', 128)]:
        if type(raw[key]) is not str or not 1 <= len(raw[key]) <= maximum or any(ord(c) < 32 for c in raw[key]):
            raise HeartbeatError('client_configuration_invalid')
    if 'service_keys' in raw:
        services = raw['service_keys']
        if type(services) is not list or not 1 <= len(services) <= 8 or any(type(k) is not str or not IDENTIFIER.fullmatch(k) for k in services) or len(set(services)) != len(services):
            raise HeartbeatError('client_configuration_invalid')
        return dict(raw, service_keys=list(services))
    from .health_report import CHECK_ID,REVISION
    checks=raw['check_keys'];revision=raw['check_revision']
    if (type(checks) is not list or not 1<=len(checks)<=64
            or any(type(k) is not str or not CHECK_ID.fullmatch(k) or not k.startswith(raw['site_id']+'.') for k in checks)
            or len(set(checks))!=len(checks)
            or type(revision) is not str or not REVISION.fullmatch(revision)):
        raise HeartbeatError('client_configuration_invalid')
    return dict(raw,check_keys=list(checks))


def token_valid(token, config, now):
    # Google HTTPS token responses and protected local cache are trusted inputs.
    # These identity checks are defensive; Firestore verifies the JWT signature.
    try:
        if type(token) is not str or not 1 <= len(token) <= 16384: return False
        parts = token.split('.')
        if len(parts) != 3: return False
        raw = parts[1] + '=' * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(raw))
        return (type(claims) is dict and claims.get('aud') == config['project_id']
                and claims.get('iss') == 'https://securetoken.google.com/' + config['project_id']
                and claims.get('sub') == config['expected_uid']
                and type(claims.get('exp')) is int and now + 60 < claims['exp'] <= now + 7200
                and type(claims.get('firebase')) is dict and claims['firebase'].get('sign_in_provider') == 'custom')
    except (ValueError, TypeError, UnicodeError): return False


class HeartbeatClient:
    def __init__(self, config, credential_file, token_file, request=None, clock=time.time):
        self.config = validate_config(config)
        self.credential_file = Path(credential_file); self.token_file = Path(token_file)
        if self.credential_file == self.token_file: raise HeartbeatError('credential_path_invalid')
        self.request = request or request_json; self.clock = clock

    def call(self, url, body, headers):
        try:
            value = self.request(url, body, headers, HTTP_TIMEOUT)
            if type(value) is not dict: raise ValueError('invalid response')
            return value
        except Exception: raise HeartbeatError('request_unavailable') from None

    def id_token(self):
        credentials = read_private(self.credential_file)
        if (set(credentials) != {'version', 'project_id', 'uid', 'refresh_token'}
                or type(credentials.get('version')) is not int or credentials['version'] != 1
                or credentials.get('project_id') != self.config['project_id'] or credentials.get('uid') != self.config['expected_uid']
                or type(credentials.get('refresh_token')) is not str or not 1 <= len(credentials['refresh_token']) <= 8192):
            raise HeartbeatError('credential_identity_invalid')
        now = self.clock()
        cached = read_private(self.token_file, missing=True, malformed=True)
        if cached is not None and set(cached) == {'id_token'} and token_valid(cached['id_token'], self.config, now):
            return cached['id_token']
        url = AUTH_BASE + '?' + urlencode({'key': self.config['api_key']})
        body = urlencode({'grant_type': 'refresh_token', 'refresh_token': credentials['refresh_token']}).encode()
        result = self.call(url, body, {'Content-Type': 'application/x-www-form-urlencoded'})
        token, refresh = result.get('id_token'), result.get('refresh_token')
        if (result.get('user_id') != self.config['expected_uid'] or not token_valid(token, self.config, now)
                or type(refresh) is not str or not 1 <= len(refresh) <= 8192):
            raise HeartbeatError('token_identity_invalid')
        # Persist a rotated refresh token before caching its ID token.
        if refresh != credentials['refresh_token']:
            write_private(self.credential_file, dict(credentials, refresh_token=refresh))
        write_private(self.token_file, {'id_token': token})
        return token

    def publish(self, snapshot):
        if 'check_keys' in self.config:
            from .health_report import validate_health_snapshot
            snapshot=validate_health_snapshot(snapshot,self.config['site_id'],self.config['check_keys'],self.config['check_revision'])
        else:snapshot = validate_snapshot(snapshot, self.config['site_id'], self.config['service_keys'])
        token = self.id_token()
        url = FIRESTORE_BASE + '/projects/' + self.config['project_id'] + '/databases/(default)/documents:commit'
        body = json.dumps(commit_body(self.config['project_id'], self.config['collection'], snapshot), allow_nan=False).encode()
        result = self.call(url, body, {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token})
        if type(result.get('commitTime')) is not str or not result['commitTime']:
            raise HeartbeatError('write_not_confirmed')
        return {'accepted': True}
