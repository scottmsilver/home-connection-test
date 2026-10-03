"""Explicit helper commands work from a standalone package artifact."""
import json
import os
from pathlib import Path
import subprocess
import sys
import zipapp

import pytest


@pytest.fixture
def artifact(tmp_path):
    import shutil
    package = Path(__file__).resolve().parents[1] / 'connection_monitoring'
    staging = tmp_path / 'stage'
    shutil.copytree(package, staging / 'connection_monitoring', ignore=shutil.ignore_patterns('__pycache__'))
    target = tmp_path / 'monitoring.pyz'
    (staging / '__main__.py').write_text('from connection_monitoring.cli import main\nraise SystemExit(main())\n')
    zipapp.create_archive(staging, target)
    return target


def invoke(artifact, tmp_path, command, *args, raw='', original='unknown sensitive command'):
    env = os.environ.copy()
    env.pop('PYTHONPATH', None)
    env['SSH_ORIGINAL_COMMAND'] = original
    return subprocess.run([sys.executable, str(artifact), command, *args], cwd=tmp_path,
                          env=env, input=raw, text=True, capture_output=True)


def test_artifact_quality_cli_without_global_config(artifact, tmp_path):
    uuid = '11111111-1111-4111-8111-111111111111'
    payload = {'time': 1000, 'data': {uuid: {'ready': True, 'active': False}}}
    result = invoke(artifact, tmp_path, 'firewalla-quality', '--state', '--site', 'demo',
                    '--router', 'router', '--target', '192.0.2.1', '--wan', uuid + '=fiber', raw=json.dumps(payload))
    assert result.returncode == 0, result.stderr
    assert result.stdout == 'wan_state,site=demo,wan=fiber,router=router,source=firewalla ready=1i,active=0i 1000000000000\n'


def test_artifact_gate_cli_fixed_rejection(artifact, tmp_path):
    result = invoke(artifact, tmp_path, 'firewalla-gate', '--allowlist', str(tmp_path / 'missing.json'))
    assert result.returncode == 64
    assert result.stderr == 'rejected command\n'
    assert result.stdout == ''


def test_artifact_readiness_cli_fixed_rejection(artifact, tmp_path):
    result = invoke(artifact, tmp_path, 'firewalla-readiness', '--gate', '/demo/gate',
                    '--freshness-cutoff', 'secret', '--expected-keys', 'secret')
    assert result.returncode == 65
    assert result.stderr == 'invalid network-quality readiness inputs\n'
    assert result.stdout == ''


def test_artifact_quality_history_without_checkout(artifact, tmp_path):
    uuid = '11111111-1111-4111-8111-111111111111'
    payload = {'code': 200, 'data': {
        'metric:monitor:raw:ping:192.0.2.1:' + uuid: {
            '1000.5': {'stat': {'mean': 2.5, 'lossrate': 0.125}}
        }
    }}
    result = invoke(artifact, tmp_path, 'firewalla-quality', '--site', 'demo',
                    '--router', 'router', '--target', '192.0.2.1', '--wan', uuid + '=fiber', raw=json.dumps(payload))
    assert result.returncode == 0, result.stderr
    assert result.stdout == 'wan_quality,site=demo,wan=fiber,router=router,source=firewalla,target=192.0.2.1 packet_loss_percent=12.5,latency_ms=2.5 1000500000000\n'
