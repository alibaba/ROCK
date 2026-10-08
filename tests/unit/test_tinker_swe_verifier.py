"""Preserve arbitrary public task build inputs and grading scripts verbatim."""
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2] / 'examples/tinker_quick_start'


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def public_task(tmp_path):
    source = tmp_path / 'source/pallets__flask-5014'
    data = {'instruction.md': 'Reject an empty blueprint name.\n',
            'task.toml': '[environment]\n', 'environment/Dockerfile': 'FROM public:base\nRUN echo original\n',
            'environment/setup.sh': '#!/bin/sh\necho original-setup\n',
            'tests/test.sh': '#!/bin/bash\necho original-verifier\n',
            'tests/config.json': '{}\n', 'tests/assets/input.txt': 'auxiliary data\n'}
    for name, text in data.items():
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    (source / 'environment/setup.sh').chmod(0o755)
    return source


def test_complete_task_preserves_bytes_and_executable_mode(public_task, tmp_path):
    result = load('prepare_harbor_task').prepare(public_task, tmp_path / 'prepared')
    assert result.name == 'pallets__flask-5014'
    for source in public_task.rglob('*'):
        if source.is_file():
            target = result / source.relative_to(public_task)
            assert target.read_bytes() == source.read_bytes()
            assert target.stat().st_mode == source.stat().st_mode
    with pytest.raises(ValueError, match='overwrite'):
        load('prepare_harbor_task').prepare(public_task, tmp_path / 'prepared')


def test_sparse_download_preserves_full_task_from_pinned_revision(public_task, tmp_path, monkeypatch):
    module = load('download_public_harbor_task')
    repository = tmp_path / 'upstream'
    import shutil
    destination = repository / 'datasets/swebench-verified' / public_task.name
    shutil.copytree(public_task, destination)
    def git(*args):
        return subprocess.check_output(['git', '-C', str(repository), *args], text=True).strip()
    git('init', '-q'); git('add', '.')
    git('-c', 'user.name=Test', '-c', 'user.email=test@example.org', 'commit', '-qm', 'Public task fixture')
    monkeypatch.setattr(module, 'COMMIT', git('rev-parse', 'HEAD'))
    monkeypatch.setattr(module, 'REPOSITORY', str(repository))
    target = tmp_path / 'downloaded'
    result = module.download(target, public_task.name)
    assert result['instance_id'] == public_task.name
    assert (target / 'environment/setup.sh').read_bytes() == (public_task / 'environment/setup.sh').read_bytes()
    assert (target / 'tests/assets/input.txt').exists()
    assert (target / 'environment/setup.sh').stat().st_mode & 0o111
    cached = tmp_path / 'cached'
    module.download(cached, public_task.name, target)
    (target / 'tests/test.sh').write_text('modified')
    with pytest.raises(ValueError, match='checksum'):
        module.download(tmp_path / 'bad', public_task.name, target)


@pytest.mark.parametrize('task_id', ['../escape', 'a/b', 'bad task', '/tmp/task'])
def test_download_rejects_invalid_task_id(task_id, tmp_path):
    with pytest.raises(ValueError, match='task ID'):
        load('download_public_harbor_task').download(tmp_path / 'target', task_id)
