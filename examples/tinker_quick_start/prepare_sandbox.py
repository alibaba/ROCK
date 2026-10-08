#!/usr/bin/env python3
"""Prepare a public Harbor controller and one unchanged task using existing Docker.

Run with the control Python. No services or GPU jobs are started. The fixed public
SWE-agent is packaged separately from the original Harbor task. Runtime installs
this offline payload without changing the task environment or verifier.
Optional caches are only for restricted-network validation of these same inputs.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def run(command, **kwargs):
    try:
        subprocess.run([str(x) for x in command], check=True, **kwargs)
    except subprocess.CalledProcessError as error:
        # Build args may contain authenticated proxy URLs. Omit the command.
        raise RuntimeError(f'External command failed with exit code {error.returncode}') from None


def task_files(root, destination, cache, task_id="sympy__sympy-19637"):
    return load_helper(root, 'download_public_harbor_task').download(
        destination, task_id=task_id, cache=cache)


def load_helper(root, name):
    spec = importlib.util.spec_from_file_location(name, root / 'examples/tinker_quick_start' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def preparation_bundle(module, args):
    class PreinstalledBundle(module.Bundle):
        def wheels(self):
            # Preserve original source/version/layout. Only the agent closure is
            # prepared here; ROCK controller dependencies come from uv.lock.
            if not self.python.exists():
                self.call([sys.executable, '-m', 'venv', self.root / 'build-venv'])
                self.pip('install', 'setuptools==80.9.0', 'wheel==0.45.1')
            source = self.root / 'build/SWE-agent'
            if not source.exists():
                staging = self.root / 'build/extract'
                staging.mkdir(parents=True, exist_ok=True)
                with tarfile.open(self.assets / 'swe-agent-source.tar.gz') as archive:
                    archive.extractall(staging, filter='data')
                shutil.move(str(staging / ('SWE-agent-' + module.COMMIT)), source)
            agent = self.root / 'agent-wheels'
            agent.mkdir(exist_ok=True)
            wheel = agent / 'sweagent-1.1.0-py3-none-any.whl'
            self.pip('wheel', '--no-deps', '--no-build-isolation', '--wheel-dir', agent, source)
            module.check_source_wheel(wheel, self.assets / 'swe-agent-source.tar.gz')
            self.record(wheel, built_from_revision=module.COMMIT)
            self.source_manifest(wheel)
            # Reuse existing native wheel provenance checks, not legacy Gem download.
            self.wheelhouse(agent)
            self.pip('download', '--only-binary=:all:', '--find-links', agent, '--dest', agent,
                     '-c', self.source_dir / 'public_bundle.requirements.txt', wheel)
            self.verify_pypi_wheels(agent, ('sweagent',))
            tools = self.root / 'tool-wheels'
            tools.mkdir(exist_ok=True)
            self.pip('download', '--no-deps', '--only-binary=:all:', '--platform', 'manylinux2014_x86_64',
                     '--python-version', '312', '--implementation', 'cp', '--abi', 'cp312', '--dest', tools,
                     'tree-sitter==0.21.3', 'tree-sitter-languages==1.10.2')
            self.verify_pypi_wheels(tools)
    return PreinstalledBundle(args)


def materialize_cache(cache, destination, module):
    manifest = json.loads((cache / 'bundle-manifest.json').read_text())
    if manifest.get('revision') != module.COMMIT:
        raise ValueError('Preinstalled bundle cache has wrong public SWE-agent revision')
    selected = {}
    for name, record in manifest['files'].items():
        parts = Path(name).parts
        if not parts or parts[0] not in {'assets', 'agent-wheels', 'tool-wheels'}:
            continue
        if Path(name).is_absolute() or '..' in parts:
            raise ValueError('Unsafe bundle cache path')
        if Path(name).name.startswith(('gem_llm-', 'rl_rock-')):
            continue
        if parts[0] == 'tool-wheels' and '-cp39-' in name:
            continue
        source = cache / name
        if source.is_symlink() or digest(source) != record['sha256']:
            raise ValueError('Preinstalled public cache checksum mismatch')
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        selected[name] = record
    (destination / 'bundle-manifest.json').write_text(json.dumps(
        {'revision': module.COMMIT, 'files': selected, 'completed_stages': [], 'cache_reuse': True}, indent=2) + '\n')


def export_agent_payload(docker, image, destination):
    """Copy the build-time archive without starting a container; always remove it."""
    container = subprocess.check_output([docker, 'create', image], text=True).strip()
    if not container:
        raise RuntimeError('Docker did not return an agent export container ID')
    try:
        run([docker, 'cp', container + ':/sweagent-runtime.tar.gz', destination])
    finally:
        run([docker, 'rm', container])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rock-root', type=Path, default=os.environ.get('ROCK_ROOT'))
    parser.add_argument('--task', type=Path, default=os.environ.get('TASK'))
    parser.add_argument('--image', default='tinker-harbor:local')
    parser.add_argument('--docker', default='docker')
    parser.add_argument('--build-network', help='Optional Docker build network (validation helper)')
    parser.add_argument('--wheel', type=Path)
    parser.add_argument('--task-id', default='sympy__sympy-19637', help='Public SWE-bench Verified Harbor task ID')
    parser.add_argument('--task-source', type=Path, help='Verified public task cache (validation helper)')
    parser.add_argument('--bundle-cache', type=Path, help='SHA-verified existing public bundle (validation helper)')
    parser.add_argument('--download-cache', type=Path, help='Optional public SHA256-keyed download cache')
    parser.add_argument('--wheel-cache', type=Path, help='Optional controller wheel cache')
    parser.add_argument('--prepare-only', action='store_true', help='Prepare inputs, do not build/load any image')
    args = parser.parse_args()
    if not args.rock_root or not args.task:
        parser.error('Set ROCK_ROOT and TASK, or pass --rock-root and --task')
    root, task = args.rock_root.resolve(), args.task.resolve()
    output = task / 'sandbox-preparation'
    dataset = task / 'assets/harbor-tasks'
    if output.exists() or dataset.exists():
        parser.error('Refusing to overwrite existing preparation or task assets')
    if not args.prepare_only:
        run([args.docker, 'info'], stdout=subprocess.DEVNULL)
    output.mkdir(parents=True)
    if args.wheel:
        wheel = args.wheel.resolve()
    else:
        run(['uv', 'build', '--project', root, '--wheel', '--out-dir', output / 'dist'])
        candidates = list((output / 'dist').glob('rl_rock-*.whl'))
        if len(candidates) != 1:
            raise ValueError('Expected one newly built unified wheel')
        wheel = candidates[0]
    helper = load_helper(root, 'prepare_public_bundle')
    bundle_root = output / 'public-bundle'
    bundle_root.mkdir()
    if args.bundle_cache:
        materialize_cache(args.bundle_cache, bundle_root, helper)
    namespace = argparse.Namespace(rock_root=root, work_dir=bundle_root, rock_wheel=wheel,
        download_cache=args.download_cache, stages='', index_url='https://pypi.org/simple')
    bundle = preparation_bundle(helper, namespace)
    if args.bundle_cache:
        # Cache does not skip immutable source or unchanged wheel verification.
        for name, (_, expected) in helper.ASSETS.items():
            if digest(bundle.assets / name) != expected:
                raise ValueError('Pinned public source mismatch')
        for expected in helper.ENCODINGS.values():
            if not any(digest(p) == expected for p in (bundle.assets / 'tiktoken-cache').iterdir() if p.is_file()):
                raise ValueError('Pinned tokenizer cache mismatch')
        agent_wheel = bundle_root / 'agent-wheels/sweagent-1.1.0-py3-none-any.whl'
        helper.check_source_wheel(agent_wheel, bundle.assets / 'swe-agent-source.tar.gz')
        bundle.source_manifest(agent_wheel)
        tools = bundle_root / 'tool-wheels'
        for package in ('tree_sitter-0.21.3', 'tree_sitter_languages-1.10.2'):
            if not any(tools.glob(package + '-cp312-*.whl')):
                raise ValueError('Bundle cache lacks CP312 agent tool wheels; rebuild without --bundle-cache')
    else:
        bundle.sources()
        bundle.wheels()
    bundle.contexts()
    agent, context = bundle_root / 'agent-context', bundle_root / 'outer-context'
    # The task-independent agent recipe is authoritative. The controller uses the current
    # frozen sandbox extra and ordinary dockerd, without VFS/network overrides.
    controller_wheels = context / 'wheels'
    controller_wheels.mkdir()
    shutil.copyfile(wheel, controller_wheels / wheel.name)
    if args.wheel_cache:
        for p in args.wheel_cache.glob('*.whl'):
            if not p.name.startswith(('gem_llm-', 'rl_rock-')):
                shutil.copyfile(p, controller_wheels / p.name)
    run(['uv', 'export', '--project', root / 'rock-tinker/local', '--frozen', '--group', 'sandbox',
         '--no-default-groups', '--no-emit-project', '--no-emit-package', 'rl-rock', '--no-hashes',
         '--output-file', context / 'requirements-rocklet.txt'], stdout=subprocess.DEVNULL)
    for path in context.rglob('*'):
        if path.is_file() and not path.is_symlink():
            bundle.record(path, flush=False, docker_copy_input=True)
    source_task = output / args.task_id
    source = task_files(root, source_task, args.task_source, args.task_id)
    load_helper(root, 'prepare_harbor_task').prepare(source_task, dataset)
    # Include all build inputs: a source pin alone does not identify tool/config changes.
    identity = hashlib.sha256()
    for path in sorted(agent.rglob('*')):
        if path.is_file():
            identity.update(str(path.relative_to(agent)).encode())
            identity.update(digest(path).encode())
    agent_tag = 'public-sweagent-runtime:' + identity.hexdigest()[:24]
    manifest = {'image': args.image, 'agent_image': agent_tag, 'wheel_sha256': digest(wheel),
        'context': str(context), 'agent_context': str(agent), 'dataset_path': str(dataset),
        'task': source, 'image_built': False, 'agent_installation': 'fixed public offline SWE-agent payload',
        'bundle_cache_used': bool(args.bundle_cache), 'agent_payload_created': False,
        'controller_lock_sha256': digest(root / 'rock-tinker/local/uv.lock'),
        'controller_requirements_sha256': digest(context / 'requirements-rocklet.txt')}
    bundle.manifest['pending_external_inputs'] = ['build agent payload', 'build outer image']
    helper.write_json(bundle.manifest_path, bundle.manifest)
    report = output / 'manifest.json'
    report.write_text(json.dumps(manifest, indent=2) + '\n')
    if not args.prepare_only:
        def build(directory, image, offline=False):
            command = [args.docker, 'build', '-f', directory / ('Dockerfile.agent' if directory == agent else 'Dockerfile'), '-t', image]
            if args.build_network:
                command += ['--network', args.build_network]
            for name in ('HTTP_PROXY', 'HTTPS_PROXY', 'NO_PROXY', 'http_proxy', 'https_proxy', 'no_proxy', 'DEBIAN_MIRROR'):
                if os.environ.get(name):
                    command += ['--build-arg', name + '=' + os.environ[name]]
            if offline:
                command += ['--build-arg', 'INSTALL_OFFLINE=1']
            run(command + [directory])
        build(agent, agent_tag)
        export_agent_payload(args.docker, agent_tag, context / 'sweagent-runtime.tar.gz')
        manifest['agent_payload_created'] = True
        manifest['agent_payload_sha256'] = digest(context / 'sweagent-runtime.tar.gz')
        build(context, args.image, bool(args.wheel_cache))
        manifest['image_built'] = True
        manifest['image_id'] = subprocess.check_output([args.docker, 'image', 'inspect', '--format', '{{.Id}}', args.image], text=True).strip()
        report.write_text(json.dumps(manifest, indent=2) + '\n')
    print(json.dumps({'manifest': str(report), 'dataset_path': str(dataset), 'image_built': manifest['image_built']}, indent=2))


if __name__ == '__main__':
    main()
