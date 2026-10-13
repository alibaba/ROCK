#!/usr/bin/env python3
"""Generate standalone local configs for the Tinker quick start."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import socket
from urllib.parse import urlsplit

import yaml


def write_yaml(path, data):
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding='utf-8')


def resolve_model(value, parser):
    """Keep a public repository ID intact; normalize existing local directories."""
    path = Path(value).expanduser()
    if path.is_dir():
        path = path.resolve()
        if not (path / 'config.json').is_file():
            parser.error(f'Missing prerequisite: {path / "config.json"}')
        return str(path), True
    if path.is_absolute() or value.startswith(('./', '../', '~/')) or not re.fullmatch(
        r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', value
    ) or any(part in {'.', '..'} for part in value.split('/')):
        parser.error('--model must be an existing model directory or an owner/repository ID')
    return value, False


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--roll-root', type=Path, required=True)
    p.add_argument('--task-root', type=Path, required=True)
    p.add_argument('--model', default='Qwen/Qwen3-4B-Instruct-2507',
                   help='Public repository ID or existing local model directory')
    p.add_argument('--model-download-type', choices=('HUGGINGFACE_HUB', 'MODELSCOPE'),
                   default='HUGGINGFACE_HUB', help='ROLL model download provider')
    p.add_argument('--rollout-only', action='store_true',
                   help='Disable actor training/reference initialization and claim only sampling actions')
    p.add_argument('--task-id', default=os.environ.get('TASK_ID', 'sympy__sympy-19637'))
    p.add_argument('--rock-base-url', help='ROCK Admin endpoint; defaults to local admin port')
    p.add_argument('--rock-cluster', default='local', help='Cluster name configured by the ROCK administrator')
    p.add_argument('--outer-image', required=True)
    p.add_argument('--admin-port', type=int, default=18080)
    p.add_argument('--backend-port', type=int, default=19210)
    p.add_argument('--model-service-port', type=int, default=28080)
    p.add_argument('--node-ip', required=True, help='Reachable local Ray node IPv4 address')
    args = p.parse_args()
    socket.inet_aton(args.node_ip)
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*__[A-Za-z0-9][A-Za-z0-9_.-]*', args.task_id):
        p.error('Invalid SWE-bench task ID')
    rock_base_url = (args.rock_base_url or f'http://127.0.0.1:{args.admin_port}').rstrip('/')
    endpoint = urlsplit(rock_base_url)
    if endpoint.scheme not in ('http', 'https') or not endpoint.hostname or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment:
        p.error('--rock-base-url must be an HTTP(S) endpoint without credentials, query or fragment')
    rock = Path(__file__).resolve().parents[2]
    roll, task = (v.expanduser().resolve() for v in (args.roll_root, args.task_root))
    model, model_is_local = resolve_model(args.model, p)
    for required in (roll / 'roll', task / 'runtime-venv/bin/python', task / 'assets/harbor-tasks' / args.task_id / 'task.toml'):
        if not required.exists():
            p.error(f'Missing prerequisite: {required}')
    ray_root = Path('/tmp') / ('tk-' + hashlib.sha256(str(task).encode()).hexdigest()[:8])
    config = task / 'config'
    config.mkdir(parents=True, exist_ok=True)
    # Refuse accidental replacement of a user's edited configuration.
    if any((config / name).exists() for name in ('runtime.yaml', 'harbor.yaml', 'engine.yaml', 'admin.yaml')):
        p.error('Configuration already exists; choose a new task directory or preserve/remove it explicitly')
    report_path = task / 'runtime-venv/setup-public-runtime.json'
    try:
        report = json.loads(report_path.read_text())
        measured = report['runtime_env']
    except (OSError, ValueError, KeyError):
        p.error('Run setup_public_runtime.sh first; its verified runtime environment report is required')
    keys = ('LD_LIBRARY_PATH', 'LD_PRELOAD', 'CPATH', 'TRITON_PTXAS_PATH',
            'TRITON_CUOBJDUMP_PATH', 'TRITON_NVDISASM_PATH', 'TRITON_LIBDEVICE_PATH')
    if not report.get('triton_cpu_compile_passed') or any(not measured.get(k) for k in keys):
        p.error('GPU environment report lacks CUDA compiler checks; rerun setup_public_runtime.sh')
    for key in keys:
        if key.startswith('TRITON_') and not Path(measured[key]).is_file():
            p.error(f'GPU environment report references a missing file: {key}')
    if not any((Path(d) / 'cuda.h').is_file() for d in measured['CPATH'].split(':') if d):
        p.error('Verified CUDA include directory is no longer available')
    gpu_env = {'PYTHONPATH': f'{roll}/mcore_adapter/src:{roll}',
               **{key: measured[key] for key in keys}}
    model_env = {'MODEL_DOWNLOAD_TYPE': args.model_download_type,
                 'USE_MODELSCOPE': '1' if args.model_download_type == 'MODELSCOPE' else '0',
                 'HF_HUB_OFFLINE': '1' if model_is_local else '0',
                 'TRANSFORMERS_OFFLINE': '1' if model_is_local else '0'}
    gpu_env.update(model_env)
    runtime = yaml.safe_load((rock / 'examples/tinker_quick_start/runtime.yaml').read_text())
    runtime['backend']['base_url'] = f'http://127.0.0.1:{args.backend_port}'
    runtime['runtime']['workdir'] = str(roll)
    runtime['runtime']['launch_command'][0] = str(task / 'runtime-venv/bin/python')
    runtime['runtime']['env'].update(gpu_env)
    runtime_options = runtime['tinker_runtime']
    runtime_options['backend_config'].update(config_path=str(config), config_name='engine', ray_temp_dir=str(ray_root / 'gpu'))
    runtime_options['checkpoints_base'] = str(task / 'checkpoints')
    adapter = runtime['rock']
    adapter.update(workdir=str(rock), job_config_path=str(config / 'harbor.yaml'), model_service_port=args.model_service_port)
    adapter['operator_kwargs']['port'] = args.model_service_port
    adapter['env']['PYTHONPATH'] = f'{rock}/rock-tinker/src:{rock}/rock-tinker:{rock}/tinker-backend/src:{rock}:{roll}'
    adapter['env'].update(model_env)
    runtime['tinker_backend']['backend_config']['port'] = args.backend_port
    harbor = yaml.safe_load((rock / 'examples/tinker_quick_start/harbor.yaml').read_text())
    harbor['environment'].update(base_url=rock_base_url, cluster=args.rock_cluster, image=args.outer_image, uploads=[[str(task / 'assets/harbor-tasks'), '/opt/swe/harbor-tasks']])
    for dataset in harbor['datasets']:
        dataset['task_names'] = [args.task_id]
    harbor['orchestrator']['n_concurrent_trials'] = 1
    engine = yaml.safe_load((roll / 'examples/tinker_backend_runtime/public_swe_training_engine.yaml').read_text())
    engine.update(pretrain=model, logging_dir=str(task / 'logs/runtime'), output_dir=str(task / 'engine'))
    engine['system_envs'].update(gpu_env)
    for worker in ('actor_train', 'actor_infer', 'reference'):
        engine[worker]['model_args']['model_name_or_path'] = model
        engine[worker].setdefault('system_envs', {}).update(model_env)
    # Share the scored rollout budget across evaluation and all training recipes.
    engine['sequence_length'] = 65536
    engine['actor_infer'].setdefault('strategy_args', {}).setdefault(
        'strategy_config', {})['max_model_len'] = 65536
    if args.rollout_only:
        runtime['runtime']['env']['TINKER_ENABLE_TRAINING_BACKEND'] = '0'
        engine['system_envs']['TINKER_ENABLE_TRAINING_BACKEND'] = '0'
        engine['enable_reference'] = False
        runtime_options['action_types'] = ['sample', 'close_runtime']
    key = config / 'local-key.yaml'
    if not key.exists():
        fd = os.open(key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as f:
            json.dump({'aes_encrypt_key': base64.b64encode(secrets.token_bytes(32)).decode()}, f)
    admin = yaml.safe_load((rock / 'examples/tinker_quick_start/public_rock_admin.yaml').read_text())
    admin['_base'] = str(key)
    admin['ray'].update(namespace='tinker-quick-start', temp_dir=str(ray_root / 'admin'))
    for name, value in [('runtime', runtime), ('harbor', harbor), ('engine', engine), ('admin', admin)]:
        write_yaml(config / f'{name}.yaml', value)
    (config / 'manifest.json').write_text(json.dumps({'rock_root': str(rock), 'roll_root': str(roll), 'task_root': str(task), 'node_ip': args.node_ip, 'admin_port': args.admin_port, 'backend_port': args.backend_port, 'model_service_port': args.model_service_port, 'model': model, 'model_download_type': args.model_download_type, 'model_is_local': model_is_local, 'rollout_only': args.rollout_only, 'outer_image': args.outer_image, 'task_id': args.task_id, 'rock_base_url': rock_base_url, 'rock_cluster': args.rock_cluster}, indent=2) + '\n')
    print(f'Generated local configs: {config}')


if __name__ == '__main__':
    main()
