"""Private alert transport and durable, bounded retry progress."""
import hashlib
import json
import secrets
import struct
import sqlite3
import time
import os
import re
import math
from datetime import datetime
from pathlib import Path
from urllib import request
from urllib.parse import urlencode, urlsplit, parse_qs
from .alert_summary import format_alert, FACT_LABELS, selected_values


# Text remains deliverable indefinitely; optional graph retries stop after one hour.
GRAPH_RETRY_LIFETIME = 3600


class DeliveryError(Exception):
    """Sanitized delivery failure; never includes upstream URLs or responses."""


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch(url, data=None, headers=None, timeout=10, limit=1024 * 1024, opener=None):
    try:
        transport = opener or request.build_opener(NoRedirect())
        with transport.open(request.Request(url, data=data, headers=headers or {}), timeout=timeout) as response:
            body = response.read(limit + 1)
            if len(body) > limit:
                raise ValueError('oversized')
            return body
    except Exception:
        raise DeliveryError('upstream request failed') from None


class TelegramTransport:
    def __init__(self, token, chat_id, opener=None):
        self.base = 'https://api.telegram.org/bot' + token
        self.chat_id = chat_id
        self.opener = opener

    def _post(self, method, body, content_type):
        result = fetch(self.base + '/' + method, body, {'Content-Type': content_type}, opener=self.opener)
        try:
            parsed = json.loads(result)
            if parsed.get('ok') is not True:
                raise ValueError()
            message_id = parsed['result']['message_id']
            if type(message_id) is not int or message_id <= 0:
                raise ValueError()
            return message_id
        except Exception:
            raise DeliveryError('telegram delivery failed') from None

    def send_message(self, text):
        body = json.dumps({'chat_id': self.chat_id, 'text': text, 'parse_mode': 'HTML', 'link_preview_options': {'is_disabled': True}}).encode()
        return self._post('sendMessage', body, 'application/json')

    def send_photo(self, image, reply):
        boundary = 'monitoring' + secrets.token_hex(16)
        fields = {'chat_id': self.chat_id, 'reply_parameters': json.dumps({'message_id': reply})}
        chunks = []
        for name, value in fields.items():
            chunks.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
        chunks.extend([f'--{boundary}\r\nContent-Disposition: form-data; name="photo"; filename="graph.png"\r\nContent-Type: image/png\r\n\r\n'.encode(), image, f'\r\n--{boundary}--\r\n'.encode()])
        return self._post('sendPhoto', b''.join(chunks), 'multipart/form-data; boundary=' + boundary)


def panel_from_url(url, *, dashboard, panels):
    """Parse a whitelisted reference only; never fetch an incoming URL."""
    try:
        parsed = urlsplit(url)
        segments = parsed.path.split('/')
        values = parse_qs(parsed.query).get('viewPanel', [])
        if parsed.scheme not in ('http', 'https') or len(segments) not in (3, 4) or segments[1:3] != ['d', dashboard] or len(values) != 1:
            return None
        panel = values[0][6:] if values[0].startswith('panel-') else values[0]
        return panel if panel in panels else None
    except (TypeError, ValueError):
        return None


class GrafanaRenderer:
    def __init__(self, token, opener=None, *, dashboard, panels, port):
        from .notifier_config import validate_renderer
        validate_renderer(dict(dashboard=dashboard, panels=list(panels), port=port))
        self.dashboard, self.panels, self.port = dashboard, tuple(panels), port
        self.token = token
        self.opener = opener

    def __call__(self, alert):
        annotations = alert.get('annotations', {})
        panel = annotations.get('__panelId__')
        if annotations.get('__dashboardUid__') != self.dashboard or panel not in self.panels:
            panel = panel_from_url(alert.get('panelURL', ''), dashboard=self.dashboard, panels=self.panels)
        if panel is None:
            return None
        url = 'http://127.0.0.1:{}/render/d-solo/{}/{}?'.format(self.port, self.dashboard, self.dashboard) + urlencode({'orgId': '1', 'panelId': panel, 'from': 'now-6h', 'to': 'now', 'width': '1000', 'height': '500', 'tz': 'UTC'})
        image = fetch(url, headers={'Authorization': 'Bearer ' + self.token}, timeout=35, limit=5 * 1024 * 1024, opener=self.opener)
        # Grafana returns a small PNG error banner on render contention.
        # Reject it so durable graph retries run without resending the text.
        if (len(image) < 24 or not image.startswith(b'\x89PNG\r\n\x1a\n')
                or image[8:16] != b'\x00\x00\x00\x0dIHDR'
                or struct.unpack('>II', image[16:24]) != (1000, 500)):
            raise DeliveryError('invalid graph image')
        return image


class DeliveryService:
    def __init__(self, database, telegram, render=None, summarize=None, clock=time.time):
        Path(database).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(database))
        os.chmod(database, 0o600)
        self.db.execute('PRAGMA secure_delete=ON')
        self.db.execute('CREATE TABLE IF NOT EXISTS history (subject TEXT PRIMARY KEY, status TEXT NOT NULL, incident_start TEXT, accepted REAL NOT NULL, values_json TEXT NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS progress (identity TEXT PRIMARY KEY, created REAL NOT NULL, message_id INTEGER, done INTEGER NOT NULL DEFAULT 0)')
        self.telegram, self.render, self.summarize, self.clock = telegram, render, summarize, clock

    @staticmethod
    def _instant(value):
        if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})', value):
            return None
        try:
            return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
        except (ValueError, OverflowError):
            return None

    def _history_context(self, alert, now):
        labels = {key: value for key, value in alert.get('labels', {}).items()
                  if key in (*FACT_LABELS, '__alert_rule_uid__') and isinstance(value, str)}
        subject = hashlib.sha256(json.dumps(labels, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        self.db.execute('DELETE FROM history WHERE accepted < ?', (now - 30 * 86400,))
        self.db.commit()
        row = self.db.execute('SELECT status,incident_start,accepted,values_json FROM history WHERE subject=?', (subject,)).fetchone()
        current = self._instant(alert.get('startsAt'))
        context = {'previous': None, 'transition': 'first_recorded', 'differences': {}, 'history_available': bool(row), 'incident_comparable': False}
        replace, suppress = True, False
        if row:
            prior = self._instant(row[1])
            if current is None or prior is None:
                context['transition'] = 'incomparable'
                replace = current is not None and prior is None
            elif current < prior or (current == prior and row[0] == 'resolved' and alert['status'] == 'firing'):
                suppress = True
                replace = False
            else:
                context['incident_comparable'] = True
                previous = {'status': row[0], 'startsAt': row[1], 'accepted_at': row[2], 'values': json.loads(row[3])}
                context['previous'] = previous
                if current > prior:
                    context['transition'] = 'new_incident'
                elif row[0] == 'firing' and alert['status'] == 'resolved':
                    context['transition'] = 'recovered'
                else:
                    context['transition'] = 'continuing' if alert['status'] == 'firing' else 'still_resolved'
                if current == prior:
                    for key, value in selected_values(alert).items():
                        if key in previous['values']:
                            before = previous['values'][key]
                            delta = value - before
                            if type(delta) is int or (type(delta) is float and math.isfinite(delta)):
                                context['differences'][key] = {'before': before, 'after': value, 'delta': delta}
        return subject, context, replace, suppress

    def __call__(self, alert):
        identity_fields = {key: alert.get(key) for key in ('status', 'fingerprint', 'startsAt', 'endsAt')}
        if alert.get('status') == 'firing':
            identity_fields.pop('endsAt')
        if not identity_fields['fingerprint']:
            identity_fields['fingerprint'] = alert.get('labels', {})
        identity = hashlib.sha256(json.dumps(identity_fields, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        now = self.clock()
        row = self.db.execute('SELECT created,message_id,done FROM progress WHERE identity=?', (identity,)).fetchone()
        if row and now - row[0] < 600 and row[2]:
            return
        if row and row[2] and now - row[0] >= 600:
            self.db.execute('DELETE FROM progress WHERE identity=?', (identity,))
            row = None
        if row is None:
            self.db.execute('INSERT INTO progress(identity,created) VALUES (?,?)', (identity, now))
            self.db.commit()
        message_id = row[1] if row else None
        if message_id is None:
            subject, context, replace_history, suppress = self._history_context(alert, now)
            if suppress:
                self.db.execute('UPDATE progress SET done=1,created=? WHERE identity=?', (now, identity))
                self.db.execute('DELETE FROM progress WHERE done=1 AND created < ?', (now - 86400,))
                self.db.commit()
                return
            try:
                message_id = self.telegram.send_message(format_alert(alert, self.summarize, context).text)
            except Exception:
                raise DeliveryError('text delivery failed') from None
            accepted = self.clock()
            with self.db:
                self.db.execute('UPDATE progress SET message_id=?,created=? WHERE identity=?', (message_id, accepted, identity))
                if replace_history:
                    start = alert.get('startsAt') if self._instant(alert.get('startsAt')) is not None else None
                    self.db.execute('INSERT OR REPLACE INTO history VALUES (?,?,?,?,?)', (subject, alert['status'], start, accepted, json.dumps(selected_values(alert), sort_keys=True)))
                    self.db.execute('DELETE FROM history WHERE subject IN (SELECT subject FROM history ORDER BY accepted DESC,subject LIMIT -1 OFFSET 1000)')
        if row and row[1] is not None and now - row[0] >= GRAPH_RETRY_LIFETIME:
            self.db.execute('UPDATE progress SET done=1,created=? WHERE identity=?', (now, identity))
            self.db.commit()
            return
        try:
            image = self.render(alert) if self.render else None
        except Exception:
            raise DeliveryError('graph rendering failed') from None
        if image:
            try:
                self.telegram.send_photo(image, message_id)
            except Exception:
                raise DeliveryError('photo delivery failed') from None
        self.db.execute('UPDATE progress SET done=1,created=? WHERE identity=?', (now, identity))
        self.db.execute('DELETE FROM progress WHERE done=1 AND created < ?', (now - 86400,))
        self.db.commit()
