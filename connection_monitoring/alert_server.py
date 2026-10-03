"""Authenticated loopback Grafana webhook endpoint. No request logging."""
import hmac
import json
import math
import os
from http.server import BaseHTTPRequestHandler, HTTPServer

MAX_BODY = 256 * 1024


def validate_payload(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get('alerts'), list) or not 1 <= len(payload['alerts']) <= 20:
        raise ValueError('invalid alerts')
    for alert in payload['alerts']:
        if not isinstance(alert, dict) or alert.get('status') not in ('firing', 'resolved'):
            raise ValueError('invalid status')
        for field in ('labels', 'annotations'):
            mapping = alert.get(field)
            if mapping is None:
                mapping = {}
                alert[field] = mapping
            if not isinstance(mapping, dict) or len(mapping) > 100 or any(not isinstance(k, str) or not isinstance(v, str) for k, v in mapping.items()):
                raise ValueError('invalid mapping')
        values = alert.get('values')
        if values is None:
            values = {}
        alert['values'] = values
        if not isinstance(values, dict) or len(values) > 100 or any(not isinstance(k, str) or (v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v))) for k, v in values.items()):
            raise ValueError('invalid values')
        for field in ('fingerprint', 'startsAt', 'endsAt', 'generatorURL', 'panelURL'):
            if field in alert and not isinstance(alert[field], str):
                raise ValueError('invalid string')
    return payload['alerts']


def make_server(token, deliver, address=('127.0.0.1', 8092)):
    if not token:
        raise ValueError('webhook token required')
    if address[0] != '127.0.0.1':
        raise ValueError('loopback binding required')
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(10)
        def log_message(self, *args):
            pass
        def respond(self, status):
            self.send_response(status)
            self.send_header('Content-Length', '0')
            self.send_header('Connection', 'close')
            self.end_headers()
            self.close_connection = True
        def do_GET(self):
            if self.path != '/healthz':
                self.respond(404)
            else:
                healthy = getattr(deliver, 'healthy', lambda: True)
                self.respond(200 if healthy() else 503)
        def do_POST(self):
            if self.path != '/alerts':
                self.respond(404); return
            supplied = self.headers.get('Authorization', '')
            if not hmac.compare_digest(supplied.encode(), ('Bearer ' + token).encode()):
                self.respond(401); return
            try:
                if self.headers.get('Transfer-Encoding') or len(self.headers.get_all('Content-Length', [])) != 1:
                    raise ValueError()
                length = int(self.headers['Content-Length'])
                if length > MAX_BODY:
                    self.respond(413); return
                if length <= 0:
                    raise ValueError()
                body = self.rfile.read(length)
                if len(body) != length:
                    raise ValueError()
                alerts = validate_payload(json.loads(body))
            except Exception:
                self.respond(400); return
            try:
                enqueue = getattr(deliver, 'enqueue_batch', None)
                if enqueue is not None:
                    enqueue(alerts)
                else:
                    for alert in alerts:
                        deliver(alert)
            except Exception:
                self.respond(503); return
            self.respond(200)
    return HTTPServer(address, Handler)


from .notifier_config import validate_runtime_config, resolve_secrets


def runtime_delivery(config):
    validate_runtime_config(config)
    secrets = resolve_secrets(config)
    from .alert_delivery import DeliveryService, GrafanaRenderer, TelegramTransport
    from .openrouter_summary import production_summary
    return DeliveryService(config['delivery_database'], TelegramTransport(secrets['telegram_token'], secrets['telegram_chat_id']), GrafanaRenderer(secrets['grafana_token'], **config['renderer']), production_summary(secrets['openrouter_key'], config['summary']))


def serve_runtime(listener, queue):
    listener.timeout = 1
    while queue.healthy():
        listener.handle_request()
    raise SystemExit('notifier worker unavailable')


def main(config):
    from .alert_queue import AlertQueue
    validate_runtime_config(config)
    secrets = resolve_secrets(config)
    queue = AlertQueue(config['queue_database'], **config['queue'])
    try:
        queue.start(lambda: runtime_delivery(config))
        listener = make_server(secrets['webhook_token'], queue, (config['listen']['host'], config['listen']['port']))
        try:
            serve_runtime(listener, queue)
        finally:
            listener.server_close()
    finally:
        queue.close()
