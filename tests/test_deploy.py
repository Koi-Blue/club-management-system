import argparse
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import tarfile

import pytest

from scripts.backup_data import backup
from scripts import deploy


def test_backup_restores_committed_wal_and_uploaded_files(tmp_path):
    data = tmp_path / 'source'
    (data / 'uploads').mkdir(parents=True)
    (data / 'uploads' / '发票.pdf').write_bytes(b'invoice bytes')
    db = sqlite3.connect(data / 'club.db')
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('PRAGMA wal_autocheckpoint=0')
    db.execute('CREATE TABLE records (body TEXT)')
    db.execute("INSERT INTO records VALUES ('committed')")
    db.commit()
    db.execute("INSERT INTO records VALUES ('uncommitted')")
    assert (data / 'club.db-wal').stat().st_size > 0
    stream = io.BytesIO()
    backup(data, stream)
    stream.seek(0)
    restored = tmp_path / 'restore'
    with tarfile.open(fileobj=stream) as archive:
        assert 'data/club.db-wal' not in archive.getnames()
        archive.extractall(restored, filter='data')
    with sqlite3.connect(restored / 'data' / 'club.db') as result:
        assert result.execute('SELECT body FROM records').fetchall() == [('committed',)]
        assert result.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    assert (restored / 'data' / 'uploads' / '发票.pdf').read_bytes() == b'invoice bytes'
    db.rollback()
    db.close()


def test_backup_missing_original_database_refuses_empty_deploy(tmp_path):
    with pytest.raises(RuntimeError, match='找不到原有'):
        backup(tmp_path, io.BytesIO())


@pytest.fixture
def deployment(tmp_path, monkeypatch):
    monkeypatch.setattr(deploy, 'ROOT', tmp_path)
    (tmp_path / '.env').write_text('keep these settings')
    (tmp_path / 'docker-compose.yml').write_text('services: {}')
    (tmp_path / 'scripts').mkdir()
    (tmp_path / 'scripts' / 'backup_data.py').write_text('backup program')
    old = {
        'Image': 'sha256:old-image',
        'Mounts': [{'Destination': '/app/data', 'Type': 'volume', 'Name': 'original-project_club-data'}],
        'Config': {'Env': ['DATABASE_URL=sqlite:////app/data/club.db', 'UPLOAD_DIR=/app/data/uploads'],
                   'Labels': {'com.docker.compose.project': 'original-project', 'com.docker.compose.service': 'club'}},
    }
    config = {'services': {'club': {'environment': {'DATABASE_URL': 'sqlite:////app/data/club.db', 'UPLOAD_DIR': '/app/data/uploads'},
                                   'volumes': [{'source': 'club-data', 'target': '/app/data', 'type': 'volume'}]}},
              'volumes': {'club-data': {'name': 'club-management-system_club-data'}}}
    state = {'container': old, 'commands': [], 'overrides': [], 'health_checks': 0, 'fail': None, 'dirty': False}

    def output(*args):
        if args[:3] == ('git', 'branch', '--show-current'):
            return deploy.BRANCH
        if args[:3] == ('git', 'status', '--porcelain'):
            return ' M README.md' if state['dirty'] else ''
        if args[:3] == ('git', 'rev-parse', 'HEAD'):
            return 'new-commit'
        if args[-3:] == ('config', '--format', 'json'):
            return json.dumps(config)
        if args[:3] == ('docker', 'volume', 'ls'):
            return state.get('volumes', '')
        raise AssertionError(args)

    def run(*args, **kwargs):
        state['commands'].append(args)
        for arg in args:
            if isinstance(arg, str) and arg.endswith('.json'):
                state['overrides'].append(json.loads(Path(arg).read_text()))
        phase = state['fail']
        if (phase == 'build' and 'build' in args) or (phase == 'backup' and args[:2] == ('docker', 'run')):
            raise subprocess.CalledProcessError(1, args)
        if args[:2] == ('docker', 'run'):
            kwargs['stdout'].write(b'tar stream (backup algorithm tested independently)')

    def healthy():
        state['health_checks'] += 1
        if state['fail'] == 'health' and state['health_checks'] == 1:
            raise RuntimeError('health failed')

    monkeypatch.setattr(deploy, 'output', output)
    monkeypatch.setattr(deploy, 'run', run)
    monkeypatch.setattr(deploy, 'inspect_container', lambda: state['container'])
    monkeypatch.setattr(deploy, 'wait_healthy', healthy)
    return state, tmp_path


def test_update_pins_actual_volume_backups_and_retains_env(deployment):
    state, root = deployment
    deploy.deploy(argparse.Namespace(local=True, init=False))
    commands = state['commands']
    build = next(i for i, c in enumerate(commands) if 'build' in c)
    stop = next(i for i, c in enumerate(commands) if c[:2] == ('docker', 'stop'))
    backup_at = next(i for i, c in enumerate(commands) if c[:2] == ('docker', 'run'))
    up = next(i for i, c in enumerate(commands) if 'up' in c)
    assert build < stop < backup_at < up
    assert any(value.get('volumes', {}).get('club-data') == {'external': True, 'name': 'original-project_club-data'} for value in state['overrides'])
    assert '-p' in commands[build] and 'original-project' in commands[build]
    assert not any('down' in c or 'prune' in c for c in commands)
    assert (root / '.env').read_text() == 'keep these settings'
    assert len(list((root / 'backups').glob('*/data.tar.gz'))) == 1
    env_backup = next((root / 'backups').glob('*/.env'))
    assert env_backup.stat().st_mode & 0o777 == 0o600
    assert state['health_checks'] == 1


@pytest.mark.parametrize('phase', ['build', 'backup', 'health'])
def test_update_failure_recovery_preserves_volume(deployment, phase):
    state, _ = deployment
    state['fail'] = phase
    with pytest.raises((RuntimeError, subprocess.CalledProcessError)):
        deploy.deploy(argparse.Namespace(local=True, init=False))
    if phase == 'build':
        assert not any(command[:2] == ('docker', 'stop') for command in state['commands'])
    elif phase == 'backup':
        assert ('docker', 'start', 'club-management') in state['commands']
        assert not any('up' in command for command in state['commands'])
    else:
        assert any(value.get('services', {}).get('club', {}).get('image', '').startswith('club-management:backup-') for value in state['overrides'])
        assert state['health_checks'] == 2
    assert not any('volume' in c and 'rm' in c for c in state['commands'])


@pytest.mark.parametrize('scenario', ['missing', 'dirty', 'bind', 'custom_db', 'init_existing', 'init_existing_volume'])
def test_preflight_refuses_unsafe_or_unsupported_deploy(deployment, scenario):
    state, _ = deployment
    init = scenario.startswith('init_')
    if scenario in {'missing', 'init_existing_volume'}:
        state['container'] = None
        state['volumes'] = 'club-management-system_club-data'
    elif scenario == 'dirty':
        state['dirty'] = True
    elif scenario == 'bind':
        state['container']['Mounts'][0]['Type'] = 'bind'
    elif scenario == 'custom_db':
        state['container']['Config']['Env'][0] = 'DATABASE_URL=postgresql://custom'
    with pytest.raises(RuntimeError):
        deploy.deploy(argparse.Namespace(local=True, init=init))
    assert not any('build' in c or 'stop' in c or 'up' in c for c in state['commands'])
