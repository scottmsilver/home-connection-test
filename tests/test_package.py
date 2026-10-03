import subprocess
import sys

def test_cli_rejects_missing_configuration():
    result = subprocess.run([sys.executable, '-m', 'connection_monitoring', '--config', '/nonexistent/config.json', 'validate'], capture_output=True, text=True)
    assert result.returncode == 2
    assert 'configuration' in result.stderr

def test_version_is_available_without_configuration():
    result = subprocess.run([sys.executable, '-m', 'connection_monitoring', '--version'], capture_output=True, text=True)
    assert result.returncode == 0
    assert result.stdout.strip()
