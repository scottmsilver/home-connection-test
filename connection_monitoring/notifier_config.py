"""Notifier configuration validation; contains no deployment policy or credentials."""
import math
import os
import re
from pathlib import Path
from .config import ConfigError


def _object(value, fields, name):
    if not isinstance(value, dict) or set(value) != set(fields):
        raise ConfigError('{} requires explicit fields: {}'.format(name, ', '.join(fields)))


def _number(value, name, maximum=300):
    if type(value) not in (int, float) or not 0 < value <= maximum or not math.isfinite(value):
        raise ConfigError('{} must be a positive bounded number'.format(name))


def _branch(branch, free=False):
    _object(branch, ('model','providers','request_timeout','total_timeout','max_price'), 'model branch')
    if not isinstance(branch['model'], str) or not branch['model'].strip() or len(branch['model']) > 256:
        raise ConfigError('explicit model required')
    providers = branch['providers']
    if not isinstance(providers, list) or not 1 <= len(providers) <= 20 or any(not isinstance(p, str) or not p.strip() or len(p) > 128 for p in providers) or len(set(providers)) != len(providers):
        raise ConfigError('explicit provider allowlist required')
    _number(branch['request_timeout'], 'request_timeout')
    _number(branch['total_timeout'], 'total_timeout')
    if branch['request_timeout'] > branch['total_timeout']:
        raise ConfigError('request timeout exceeds total timeout')
    price = branch['max_price']
    _object(price, ('prompt','completion','request'), 'max_price')
    for value in price.values():
        if type(value) not in (int,float) or not 0 <= value <= 100 or not math.isfinite(value):
            raise ConfigError('finite bounded price required')
    if free and (not branch['model'].endswith(':free') or any(price.values())):
        raise ConfigError('free branches require zero price and explicit free model')


def validate_summary_policy(policy):
    _object(policy, ('free','paid','hedge_delay','race_timeout','zdr','data_collection'), 'summary')
    if policy['zdr'] is not True or policy['data_collection'] != 'deny':
        raise ConfigError('zero retention and denied data collection required')
    free = policy['free']
    if not isinstance(free, list) or len(free) != 2:
        raise ConfigError('exactly two free branches required')
    for branch in free:
        _branch(branch, free=True)
    if set(p.lower() for p in free[0]['providers']) & set(p.lower() for p in free[1]['providers']):
        raise ConfigError('free provider allowlists must be disjoint')
    _branch(policy['paid'])
    _number(policy['hedge_delay'], 'hedge_delay')
    _number(policy['race_timeout'], 'race_timeout')
    if policy['hedge_delay'] >= policy['race_timeout']:
        raise ConfigError('hedge delay must be shorter than race deadline')
    return policy


def validate_renderer(renderer):
    _object(renderer, ('dashboard','panels','port'), 'renderer')
    if not isinstance(renderer['dashboard'], str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', renderer['dashboard']):
        raise ConfigError('dashboard identifier required')
    panels = renderer['panels']
    if not isinstance(panels, list) or not 1 <= len(panels) <= 100 or any(not isinstance(p, str) or not re.fullmatch(r'[1-9][0-9]{0,8}',p) for p in panels) or len(set(panels)) != len(panels):
        raise ConfigError('panel allowlist required')
    if type(renderer['port']) is not int or not 1 <= renderer['port'] <= 65535:
        raise ConfigError('renderer port required')
    return renderer


def validate_runtime_config(config):
    _object(config, ('secrets','delivery_database','queue_database','listen','renderer','summary','queue'), 'notifier')
    _object(config['secrets'], ('telegram_token','telegram_chat_id','webhook_token','grafana_token','openrouter_key'), 'secrets')
    for name in config['secrets'].values():
        if not isinstance(name,str) or not re.fullmatch(r'[A-Z][A-Z0-9_]{0,127}',name):
            raise ConfigError('secret environment variable reference required')
    for field in ('delivery_database','queue_database'):
        path = config[field]
        if not isinstance(path,str) or not path or '\x00' in path or not Path(path).is_absolute() or '..' in Path(path).parts:
            raise ConfigError('explicit absolute database path required')
    if config['delivery_database'] == config['queue_database']:
        raise ConfigError('database paths must differ')
    _object(config['listen'], ('host','port'), 'listen')
    if config['listen']['host'] != '127.0.0.1' or type(config['listen']['port']) is not int or not 1 <= config['listen']['port'] <= 65535:
        raise ConfigError('loopback listener required')
    validate_renderer(config['renderer'])
    validate_summary_policy(config['summary'])
    _object(config['queue'], ('capacity','retry_delay'), 'queue')
    if type(config['queue']['capacity']) is not int or not 1 <= config['queue']['capacity'] <= 100000:
        raise ConfigError('bounded queue capacity required')
    _number(config['queue']['retry_delay'],'retry_delay',86400)
    return config


def resolve_secrets(config):
    values = {field: os.environ.get(name, '') for field,name in config['secrets'].items()}
    if any(not value for value in values.values()):
        raise ConfigError('notifier credentials required')
    return values
