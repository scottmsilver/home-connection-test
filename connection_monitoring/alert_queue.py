"""Bounded, durable private queue. Only selected alert facts are persisted."""
import json
import math
import os
import re
import sqlite3
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit, parse_qs, urlencode
from .alert_summary import FACT_LABELS



class QueueError(Exception):
    """Sanitized queue failure."""


def selected_alert(alert):
    result = {key: alert[key] for key in ('status', 'fingerprint', 'startsAt', 'endsAt') if key in alert}
    result['labels'] = {key: value for key, value in alert.get('labels', {}).items() if key in (*FACT_LABELS, '__alert_rule_uid__')}
    result['annotations'] = {key: value for key, value in alert.get('annotations', {}).items() if key in ('summary', '__dashboardUid__', '__panelId__', 'estate_context')}
    result['values'] = {key: value for key, value in (alert.get('values') or {}).items() if isinstance(key, str) and (value is None or (type(value) in (int, float) and math.isfinite(value)))}
    url = alert.get('generatorURL', '')
    try:
        parsed = urlsplit(url)
        if parsed.scheme in ('http', 'https') and parsed.hostname and not parsed.username and not parsed.password:
            # Query and fragment may contain credentials; alert view needs neither.
            result['generatorURL'] = parsed._replace(query='', fragment='').geturl()
    except ValueError:
        pass
    try:
        parsed = urlsplit(alert.get('panelURL', ''))
        values = parse_qs(parsed.query).get('viewPanel', [])
        if parsed.scheme in ('http', 'https') and parsed.hostname and not parsed.username and not parsed.password and len(values) == 1:
            panel = values[0][6:] if values[0].startswith('panel-') else values[0]
            if re.fullmatch(r'[1-9][0-9]{0,8}', panel):
                result['panelURL'] = parsed._replace(query=urlencode({'viewPanel': panel}), fragment='').geturl()
    except (TypeError, ValueError):
        pass
    return result


class AlertQueue:
    def __init__(self, database, capacity=1000, retry_delay=30):
        path = Path(database)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        os.chmod(path, 0o600)
        self.db.execute('PRAGMA secure_delete=ON')
        self.db.execute('CREATE TABLE IF NOT EXISTS jobs (id INTEGER PRIMARY KEY, payload TEXT NOT NULL, due REAL NOT NULL)')
        self.db.commit()
        self.capacity, self.retry_delay = capacity, retry_delay
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.ready = threading.Event()
        self.thread = None
        self.start_failed = False
        self.worker_failed = False

    def healthy(self):
        return self.thread is not None and self.thread.is_alive() and not self.worker_failed

    def pending(self):
        with self.lock:
            return self.db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]

    def enqueue_batch(self, alerts):
        if self.thread is not None and not self.healthy():
            raise QueueError('worker unavailable')
        payloads = [json.dumps(selected_alert(alert), separators=(',', ':'), allow_nan=False) for alert in alerts]
        with self.lock:
            try:
                with self.db:
                    if self.db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0] + len(payloads) > self.capacity:
                        raise QueueError('alert queue full')
                    self.db.executemany('INSERT INTO jobs(payload,due) VALUES (?,?)', [(payload, time.time()) for payload in payloads])
            except QueueError:
                raise
            except Exception:
                raise QueueError('queue persistence failed') from None
        self.wake.set()

    def __call__(self, alert):
        self.enqueue_batch([alert])

    def start(self, factory, timeout=5):
        if self.thread is not None:
            raise QueueError('worker already started')
        self.thread = threading.Thread(target=self._run, args=(factory,), daemon=True)
        self.thread.start()
        if not self.ready.wait(timeout) or self.start_failed:
            self.stop.set()
            raise QueueError('worker startup failed') from None

    def _run(self, factory):
        try:
            deliver = factory()
        except Exception:
            self.start_failed = True
            self.ready.set()
            return
        self.ready.set()
        try:
            while not self.stop.is_set():
                with self.lock:
                    row = self.db.execute('SELECT id,payload FROM jobs WHERE due<=? ORDER BY due,id LIMIT 1', (time.time(),)).fetchone()
                if row is None:
                    self.wake.wait(.5)
                    self.wake.clear()
                    continue
                try:
                    deliver(json.loads(row[1]))
                except Exception:
                    with self.lock, self.db:
                        self.db.execute('UPDATE jobs SET due=? WHERE id=?', (time.time() + self.retry_delay, row[0]))
                else:
                    with self.lock, self.db:
                        self.db.execute('DELETE FROM jobs WHERE id=?', (row[0],))
        except Exception:
            self.worker_failed = True
        finally:
            close = getattr(deliver, 'close', None)
            if close:
                try:
                    close()
                except Exception:
                    self.worker_failed = True

    def close(self):
        self.stop.set()
        self.wake.set()
        if self.thread:
            self.thread.join(timeout=5)
            if self.thread.is_alive():
                return  # In-flight work retains its SQLite connection until process exit.
        with self.lock:
            self.db.close()
