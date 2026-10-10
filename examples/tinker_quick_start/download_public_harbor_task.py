"""Fetch a complete public Harbor task at a fixed dataset revision."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

COMMIT = '86723674f04e4209ac479d0fb75d9d9f44b4377e'
DEFAULT_TASK_ID = 'sympy__sympy-19637'
REPOSITORY = 'https://github.com/laude-institute/harbor-datasets.git'
SOURCE_ROOT = 'https://raw.githubusercontent.com/laude-institute/harbor-datasets/' + COMMIT + '/datasets/swebench-verified/'
MANIFEST = 'public-harbor-task-source.json'


def validate_task_id(task_id):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*__[A-Za-z0-9][A-Za-z0-9_.-]*', task_id):
        raise ValueError('Expected a SWE-bench task ID such as pallets__flask-5014')
    return task_id


def file_hashes(directory):
    result = {}
    for path in sorted(directory.rglob('*')):
        if path.is_symlink():
            raise ValueError('Task cache must contain regular task files, not symlinks')
        if path.is_file() and path.relative_to(directory).as_posix() != MANIFEST:
            result[path.relative_to(directory).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    for name in ('instruction.md', 'task.toml', 'environment/Dockerfile', 'tests/test.sh', 'tests/config.json'):
        if name not in result:
            raise ValueError('Incomplete public SWE-bench Harbor task: ' + name)
    return result


def download(target, task_id=DEFAULT_TASK_ID, cache=None):
    validate_task_id(task_id)
    target = Path(target)
    if target.exists() and any(target.iterdir()):
        raise ValueError('Refusing to overwrite an existing task directory')
    source_url = SOURCE_ROOT + task_id + '/'
    with tempfile.TemporaryDirectory(prefix='tinker-public-task-') as temporary:
        if cache:
            source = Path(cache)
            provenance = json.loads((source / MANIFEST).read_text())
            if (provenance.get('source_commit') != COMMIT or
                    provenance.get('source_url') != source_url or
                    provenance.get('instance_id') != task_id):
                raise ValueError('Task cache has unexpected public source provenance')
            hashes = file_hashes(source)
            if hashes != provenance.get('files_sha256'):
                raise ValueError('Task cache checksum mismatch')
        else:
            checkout = Path(temporary) / 'dataset'
            commands = [
                ['git', 'init', '-q', str(checkout)],
                ['git', '-C', str(checkout), 'remote', 'add', 'origin', REPOSITORY],
                ['git', '-C', str(checkout), 'sparse-checkout', 'init', '--cone'],
                ['git', '-C', str(checkout), 'sparse-checkout', 'set', 'datasets/swebench-verified/' + task_id],
                ['git', '-C', str(checkout), 'fetch', '--depth=1', '--filter=blob:none', 'origin', COMMIT],
                ['git', '-C', str(checkout), 'checkout', '--detach', 'FETCH_HEAD'],
            ]
            for command in commands:
                subprocess.run(command, check=True)
            source = checkout / 'datasets/swebench-verified' / task_id
            hashes = file_hashes(source)
        shutil.copytree(source, target, dirs_exist_ok=True, copy_function=shutil.copy2)
    provenance = {'source_commit': COMMIT, 'source_url': source_url, 'instance_id': task_id,
                  'task_count': 1, 'files_sha256': hashes, 'task_files_unchanged': True,
                  'retrieval': 'validated_cache' if cache else 'public_git'}
    (target / MANIFEST).write_text(json.dumps(provenance, indent=2) + '\n')
    return provenance


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', required=True, type=Path)
    parser.add_argument('--task-id', default=DEFAULT_TASK_ID)
    parser.add_argument('--cache', type=Path)
    args = parser.parse_args()
    print(json.dumps(download(args.target, args.task_id, args.cache), indent=2))
