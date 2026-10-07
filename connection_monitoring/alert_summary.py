"""Provider-independent facts, short summaries, and safe Telegram formatting.

The provider adapter must enforce its own request deadline. This module never
receives credentials and does not perform network requests or change routing.
"""

from dataclasses import dataclass
from copy import deepcopy
import html
import math
import json
import re
from urllib.parse import urlsplit


MAX_MESSAGE = 4096
MAX_SUMMARY = 700
FACT_LABELS = ("site", "alertname", "service", "peer", "wan", "host")


@dataclass(frozen=True)
class FormattedAlert:
    text: str
    used_llm: bool


def _string(value, limit):
    return value[:limit] if isinstance(value, str) else ""


def finite_number(value):
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def rule_context(raw):
    """Parse only bounded verified provisioning metadata, never arbitrary annotations."""
    if not isinstance(raw, str) or len(raw) > 4096:
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, TypeError, RecursionError, OverflowError):
        return {}
    if not isinstance(data, dict):
        return {}
    result = {}
    for key in ('kind', 'measurement', 'unit', 'comparator', 'sample_window', 'pending_duration', 'measurement_ref'):
        if isinstance(data.get(key), str) and len(data[key]) <= 200:
            result[key] = data[key]
    threshold = data.get('threshold')
    if finite_number(threshold):
        result['threshold'] = threshold
    if isinstance(data.get('limits'), list) and len(data['limits']) <= 8 and all(isinstance(x, str) and len(x) <= 300 for x in data['limits']):
        result['limits'] = data['limits']
    return result


def selected_values(alert, include_null=False):
    return dict(list((key[:100], value) for key, value in (alert.get('values') or {}).items()
                     if isinstance(key, str) and len(key) <= 100 and
                     ((include_null and value is None) or finite_number(value)))[:20])


def facts_for_model(alert, context=None):
    labels, annotations = alert.get('labels', {}), alert.get('annotations', {})
    status = alert.get('status')
    if status not in ('firing', 'resolved'):
        raise ValueError('alert status must be firing or resolved')
    rule = rule_context(annotations.get('estate_context'))
    values = selected_values(alert, include_null=True)
    numeric = []
    for key, value in values.items():
        kind = 'condition_flag' if key.lower().startswith('condition') else 'unknown_reference'
        item = {'id': 'value:' + key, 'name': key, 'value': value, 'kind': kind}
        if key == rule.get('measurement_ref') and kind != 'condition_flag' and not re.search(r'[0-9]+$', key):
            item['kind'] = 'measurement'
            if rule.get('unit'):
                item['unit'] = rule['unit']
        numeric.append(item)
    context = context or {}
    facts = {'status': status, **{key: _string(labels.get(key), 200) for key in FACT_LABELS},
             'source_summary': _string(annotations.get('summary'), 1600),
             'source_summary_role': 'historical firing annotation' if status == 'resolved' else 'untrusted source annotation',
             'values': values, 'numeric_evidence': numeric, 'rule': rule,
             'current_readings_provided': any(item['kind'] == 'measurement' and item['value'] is not None for item in numeric),
             'timestamps': {key: _string(alert.get(key), 100) for key in ('startsAt', 'endsAt')},
             'history_available': context.get('history_available', False),
             'incident_comparable': context.get('incident_comparable', False),
             'previous': context.get('previous'), 'transition': context.get('transition', 'first_recorded'),
             'differences': context.get('differences', {}), 'cause_confirmed': False}
    facts['evidence_ids'] = ['status', 'subject', 'source_summary', 'timestamps', 'previous', 'transition']
    if rule:
        facts['evidence_ids'].append('rule')
    facts['evidence_ids'] += [item['id'] for item in numeric]
    facts['evidence_ids'] += ['difference:' + key for key in facts['differences']]
    return facts


FIELD_LIMITS = {'headline': 120, 'explanation': 400, 'what_changed': 250, 'next_step': 200}


def valid_summary(candidate, facts):
    if not isinstance(candidate, dict) or candidate.get('status') != facts['status']:
        return False
    if set(candidate) - {'status', 'evidence_ids', *FIELD_LIMITS}:
        return False
    for key, limit in FIELD_LIMITS.items():
        value = candidate.get(key)
        if key == 'next_step' and value is None:
            continue
        if not isinstance(value, str) or not value.strip() or len(value) > limit:
            return False
        if re.search(r'\b(?:WAN|UPS|telemetry|collector|firing|resolved|null|schema|evidence_ids)\b', value, re.IGNORECASE):
            return False
        if re.search(r'\b(?:Condition[0-9]+|first_recorded|condition[ -]flag|accepted update|source annotation|selected window)\b', value, re.IGNORECASE):
            return False
        if key == 'headline' and re.match(r'^(?:Clear|Check|Verify|Restore)\s', value.strip(), re.IGNORECASE):
            return False
        if key == 'next_step' and value.strip().lower() == 'none':
            return False
        if any(0x1F000 <= ord(ch) <= 0x1FAFF or 0x2600 <= ord(ch) <= 0x27BF for ch in value):
            return False
    ids = candidate.get('evidence_ids')
    return isinstance(ids, list) and 1 <= len(ids) <= 40 and all(isinstance(x, str) and x in facts['evidence_ids'] for x in ids)


def _escaped(text, budget):
    """Bound HTML without cutting an entity or leaving an unclosed tag."""
    pieces = []
    used = 0
    for character in text:
        escaped = html.escape(character, quote=True)
        if used + len(escaped) > budget - 1:
            return "".join(pieces) + "…"
        pieces.append(escaped)
        used += len(escaped)
    return "".join(pieces)


def _details(alert, facts):
    lines = ["Historical firing source facts" if facts["status"] == "resolved" else "Original source facts", _string(alert.get("annotations", {}).get("summary"), 12000)]
    for key in FACT_LABELS:
        if facts.get(key):
            lines.append(f"{key}: {facts[key]}")
    lines.extend(f"{key}: {value}" for key, value in facts["values"].items())
    return "\n".join(lines)


def _source_link(alert):
    url = _string(alert.get("generatorURL"), 2048)
    try:
        parsed = urlsplit(url)
    except ValueError:
        return ""
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        return ""
    escaped_url = html.escape(url, quote=True)
    if len(escaped_url) > 700:
        return ""
    return f'\n\n<a href="{escaped_url}">Open in Grafana</a>'


def format_alert(alert, summarize=None, context=None):
    facts = facts_for_model(alert, context)
    candidate = None
    if summarize is not None:
        try:
            result = summarize(deepcopy(facts))
            if valid_summary(result, facts):
                candidate = result
        except Exception:
            pass
    site = facts['site'] or 'Unknown site'
    status = 'Cleared' if facts['status'] == 'resolved' else 'Warning'
    title = f"{site} · {status}"
    if candidate:
        headline = re.sub(r'^' + status + r'\s*:\s*', '', candidate['headline'], flags=re.IGNORECASE)
        message = f"<b>{_escaped(title + ': ' + headline, 450)}</b>\n\n"
        message += _escaped(candidate['explanation'], 850) + '\n\n'
        message += '<b>What changed:</b> ' + _escaped(candidate['what_changed'], 600) + '\n\n'
        if candidate.get('next_step'):
            message += '<b>Next step:</b> ' + _escaped(candidate['next_step'], 450) + '\n\n'
    else:
        message = f"<b>{_escaped(title, 300)}</b>\n\nSummary unavailable\n\n"
    link = _source_link(alert)
    opening, closing = '<blockquote expandable>', '</blockquote>'
    budget = MAX_MESSAGE - len(message + opening + closing + link)
    return FormattedAlert(message + opening + _escaped(_details(alert, facts), budget) + closing + link, bool(candidate))


def format_notification(payload, summarize=None):
    alerts = payload.get("alerts")
    if not isinstance(alerts, list) or not alerts:
        raise ValueError("notification must contain alerts")
    return [format_alert(alert, summarize) for alert in alerts]
