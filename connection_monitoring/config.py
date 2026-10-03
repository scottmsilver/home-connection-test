"""Load versioned runtime configuration without environment-specific defaults."""
import json
import math
from pathlib import Path


class ConfigError(ValueError):
    """Configuration is missing or invalid."""


def _unique_object(pairs):
    value = {}
    for name, item in pairs:
        if name in value:
            raise ConfigError('configuration contains duplicate fields')
        value[name] = item
    return value


def _reject_constant(value):
    raise ConfigError('configuration contains a nonfinite number')


def _finite_float(text):
    value = float(text)
    if not math.isfinite(value):
        raise ConfigError('configuration contains a nonfinite number')
    return value


def load_config(path, section=None):
    try:
        value = json.loads(Path(path).read_text(encoding='utf-8'), object_pairs_hook=_unique_object, parse_constant=_reject_constant, parse_float=_finite_float)
    except (OSError, ValueError, TypeError) as error:
        raise ConfigError('cannot read valid configuration') from error
    if not isinstance(value, dict):
        raise ConfigError('configuration must be an object')
    if type(value.get('version')) is not int or value['version'] != 1:
        raise ConfigError('configuration version must be 1')
    unknown = set(value) - {'version', 'notifier', 'sites', 'firewalla', 'dirt'}
    if unknown:
        raise ConfigError('unknown configuration fields: {}'.format(', '.join(sorted(unknown))))
    for name in ('notifier', 'firewalla', 'dirt'):
        if name in value and not isinstance(value[name], dict):
            raise ConfigError('{} must be an object'.format(name))
    if 'sites' in value and not isinstance(value['sites'], dict):
        raise ConfigError('sites must be an object')
    if section is not None and not isinstance(value.get(section), dict):
        raise ConfigError('configuration requires {} object'.format(section))
    return value
