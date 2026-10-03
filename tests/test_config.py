import json
import pytest

def test_explicit_valid_config_has_no_defaults(tmp_path):
    from connection_monitoring.config import load_config
    path = tmp_path / 'config.json'
    path.write_text(json.dumps({'version': 1}))
    assert load_config(path) == {'version': 1}

@pytest.mark.parametrize('value', [{}, {'version': 2}, {'version': True}, {'version': 1, 'unknown': {}}, []])
def test_reject_invalid_config(tmp_path, value):
    from connection_monitoring.config import load_config, ConfigError
    path = tmp_path / 'config.json'
    path.write_text(json.dumps(value))
    with pytest.raises(ConfigError):
        load_config(path)

def test_missing_file_and_section_fail(tmp_path):
    from connection_monitoring.config import load_config, ConfigError
    with pytest.raises(ConfigError):
        load_config(tmp_path / 'missing.json')
    path = tmp_path / 'config.json'
    path.write_text('{"version": 1}')
    with pytest.raises(ConfigError):
        load_config(path, section='notifier')

@pytest.mark.parametrize('text', ['{"version":1,"version":1}', '{"version":1,"sites":{"number":NaN}}', '{"version":1,"sites":[]}'])
def test_reject_ambiguous_json_and_sites_shape(tmp_path, text):
    from connection_monitoring.config import ConfigError, load_config
    path = tmp_path / 'config.json'
    path.write_text(text)
    with pytest.raises(ConfigError):
        load_config(path)

@pytest.mark.parametrize('number', ['1e999', '-1e999'])
def test_reject_overflowing_json_numbers(tmp_path, number):
    from connection_monitoring.config import ConfigError, load_config
    path = tmp_path / 'config.json'
    path.write_text('{"version":1,"sites":{"sample":{"number":' + number + '}}}')
    with pytest.raises(ConfigError, match='cannot read valid configuration'):
        load_config(path)
