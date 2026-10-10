"""CLI configuration contracts without downloading models or starting services."""
import json
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

SCRIPT = Path(__file__).resolve().parents[3] / 'examples/tinker_quick_start/configure.py'


@pytest.fixture
def prerequisites(tmp_path):
    roll = tmp_path / 'ROLL'
    (roll / 'roll').mkdir(parents=True)
    # Exercise the CLI with a minimal ROLL checkout; unit tests need no sibling repo.
    destination = roll / 'examples/tinker_backend_runtime'
    destination.mkdir(parents=True)
    destination.joinpath('public_swe_training_engine.yaml').write_text(yaml.safe_dump({
        'system_envs': {}, 'enable_reference': True,
        **{name: {'model_args': {}} for name in ('actor_train', 'actor_infer', 'reference')}}))
    task = tmp_path / 'task'
    (task / 'runtime-venv/bin').mkdir(parents=True)
    (task / 'runtime-venv/bin/python').touch()
    (task / 'assets/harbor-tasks').mkdir(parents=True)
    environment = task / 'assets/harbor-tasks/sympy__sympy-19637/environment'
    environment.mkdir(parents=True)
    (environment / 'Dockerfile').write_text('FROM public-image:fixed\n')
    (environment.parent / 'task.toml').write_text('[environment]\n')
    tools = tmp_path / 'cuda'
    tools.mkdir()
    (tools / 'cuda.h').touch()
    compiler = tools / 'tool'
    compiler.touch()
    measured = {'LD_LIBRARY_PATH': str(tools), 'LD_PRELOAD': str(compiler), 'CPATH': str(tools)}
    measured.update({key: str(compiler) for key in (
        'TRITON_PTXAS_PATH', 'TRITON_CUOBJDUMP_PATH', 'TRITON_NVDISASM_PATH', 'TRITON_LIBDEVICE_PATH')})
    (task / 'runtime-venv/setup-public-runtime.json').write_text(json.dumps({
        'runtime_env': measured, 'triton_cpu_compile_passed': True}))
    return roll, task


def run_config(prerequisites, *extra):
    roll, task = prerequisites
    result = subprocess.run([sys.executable, str(SCRIPT), '--roll-root', str(roll),
        '--task-root', str(task), '--outer-image', 'public:test', '--node-ip', '127.0.0.1',
        *extra], capture_output=True, text=True)
    return result, task / 'config'


@pytest.mark.parametrize('provider', ['HUGGINGFACE_HUB', 'MODELSCOPE'])
def test_repository_id_reaches_workers_and_all_backend_environments(prerequisites, provider):
    result, config = run_config(prerequisites, '--model', 'Qwen/Qwen3-4B-Instruct-2507',
                                '--model-download-type', provider)
    assert result.returncode == 0, result.stderr
    runtime = yaml.safe_load((config / 'runtime.yaml').read_text())
    engine = yaml.safe_load((config / 'engine.yaml').read_text())
    assert engine['pretrain'] == 'Qwen/Qwen3-4B-Instruct-2507'
    for worker in ('actor_train', 'actor_infer', 'reference'):
        assert engine[worker]['model_args']['model_name_or_path'] == engine['pretrain']
        assert engine[worker]['system_envs']['MODEL_DOWNLOAD_TYPE'] == provider
    for env in (runtime['runtime']['env'], runtime['rock']['env'], engine['system_envs']):
        assert env['MODEL_DOWNLOAD_TYPE'] == provider
        assert env['HF_HUB_OFFLINE'] == env['TRANSFORMERS_OFFLINE'] == '0'
        assert env['USE_MODELSCOPE'] == ('1' if provider == 'MODELSCOPE' else '0')


def test_default_model_is_public_repository_id(prerequisites):
    result, config = run_config(prerequisites)
    assert result.returncode == 0, result.stderr
    manifest = json.loads((config / 'manifest.json').read_text())
    assert manifest['model'] == 'Qwen/Qwen3-4B-Instruct-2507'
    assert manifest['model_is_local'] is False
    assert manifest['rollout_only'] is False
    engine = yaml.safe_load((config / 'engine.yaml').read_text())
    assert engine['enable_reference'] is True


def test_local_model_directory_remains_offline(prerequisites, tmp_path):
    model = tmp_path / 'model'
    model.mkdir()
    (model / 'config.json').write_text('{}')
    result, config = run_config(prerequisites, '--model', str(model))
    assert result.returncode == 0, result.stderr
    runtime = yaml.safe_load((config / 'runtime.yaml').read_text())
    engine = yaml.safe_load((config / 'engine.yaml').read_text())
    assert engine['pretrain'] == str(model.resolve())
    assert runtime['runtime']['env']['HF_HUB_OFFLINE'] == '1'


def test_rollout_only_disables_training_and_reference(prerequisites):
    result, config = run_config(prerequisites, '--rollout-only')
    assert result.returncode == 0, result.stderr
    runtime = yaml.safe_load((config / 'runtime.yaml').read_text())
    engine = yaml.safe_load((config / 'engine.yaml').read_text())
    assert runtime['runtime']['env']['TINKER_ENABLE_TRAINING_BACKEND'] == '0'
    assert engine['system_envs']['TINKER_ENABLE_TRAINING_BACKEND'] == '0'
    assert engine['enable_reference'] is False
    assert runtime['tinker_runtime']['action_types'] == ['sample', 'close_runtime']


@pytest.mark.parametrize('extra', [(), ('--rollout-only',)])
def test_training_and_evaluation_share_scored_rollout_budget(prerequisites, extra):
    result, config = run_config(prerequisites, *extra)
    assert result.returncode == 0, result.stderr
    runtime = yaml.safe_load((config / 'runtime.yaml').read_text())
    engine = yaml.safe_load((config / 'engine.yaml').read_text())
    harbor = yaml.safe_load((config / 'harbor.yaml').read_text())
    agent = harbor['agents'][0]['kwargs']
    assert (agent['max_input_tokens'], agent['max_output_tokens'],
            agent['max_observation_length']) == ('32768', '8192', '12000')
    assert json.loads(agent['completion_kwargs'])['max_tokens'] == 8192
    assert next(item['n'] for item in json.loads(agent['history_processors'])
                if item['type'] == 'last_n_observations') == 8
    assert 'observation[:9000]' in agent['next_step_truncated_observation_template']
    assert 'observation[-3000:]' in agent['next_step_truncated_observation_template']
    assert engine['sequence_length'] == 65536
    assert engine['actor_infer']['strategy_args']['strategy_config']['max_model_len'] == 65536
    assert runtime['rock']['operator_factory'] == 'examples.tinker_quick_start.harbor_pull_runner:PublicModelServiceOperator'


def test_missing_explicit_local_path_fails_without_configs(prerequisites, tmp_path):
    result, config = run_config(prerequisites, '--model', str(tmp_path / 'not-present'))
    assert result.returncode != 0
    assert 'existing model directory' in result.stderr
    assert not config.exists()


def test_existing_config_is_never_overwritten(prerequisites):
    result, config = run_config(prerequisites)
    assert result.returncode == 0, result.stderr
    original = (config / 'runtime.yaml').read_bytes()
    result, _ = run_config(prerequisites, '--model', 'Other/model')
    assert result.returncode != 0
    assert 'Configuration already exists' in result.stderr
    assert (config / 'runtime.yaml').read_bytes() == original


def test_preserves_original_preinstalled_agent_and_task_files(prerequisites):
    task = prerequisites[1]
    environment = task / 'assets/harbor-tasks/sympy__sympy-19637/environment'
    # Existing task artifacts remain user-owned, even an old generated agent YAML.
    existing = environment / 'tinker-agent.yaml'
    existing.write_text('user configuration')
    result, config = run_config(prerequisites)
    assert result.returncode == 0, result.stderr
    harbor = yaml.safe_load((config / 'harbor.yaml').read_text())
    original = yaml.safe_load((SCRIPT.parent / 'harbor.yaml').read_text())
    assert harbor['agents'] == original['agents']
    assert harbor['agents'][0]['import_path'] == 'public_swe_agent:PreinstalledSweAgent'
    runtime = yaml.safe_load((config / 'runtime.yaml').read_text())
    original_runtime = yaml.safe_load((SCRIPT.parent / 'runtime.yaml').read_text())
    assert runtime['rock']['operator_kwargs'] == original_runtime['rock']['operator_kwargs']
    assert (environment / 'Dockerfile').read_text() == 'FROM public-image:fixed\n'
    assert existing.read_text() == 'user configuration'
    manifest = json.loads((config / 'manifest.json').read_text())
    assert 'sandbox_mode' not in manifest
    assert 'derived_files' not in manifest


def test_does_not_generate_agent_yaml_or_require_official_default(prerequisites):
    result, config = run_config(prerequisites)
    assert result.returncode == 0, result.stderr
    environment = prerequisites[1] / 'assets/harbor-tasks/sympy__sympy-19637/environment'
    assert not (environment / 'tinker-agent.yaml').exists()
    assert (environment / 'Dockerfile').read_text() == 'FROM public-image:fixed\n'


@pytest.mark.parametrize('mode,entry', [
    ('rollout', 'eval_swe_bench.py'), ('ppo', 'train_swe_bench.py'),
    ('ppo-kl', 'train_swe_bench_kl.py'), ('grpo', 'train_swe_bench_grpo.py'),
])
def test_launcher_passes_shared_budget_and_preserves_overrides(tmp_path, mode, entry):
    import os
    task = tmp_path / 'task'
    binary = task / 'control-venv/bin/python'
    binary.parent.mkdir(parents=True)
    binary.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$TASK/args.txt"\n')
    binary.chmod(0o755)
    env = dict(os.environ, TASK=str(task), ROLL=str(tmp_path / 'ROLL'))
    for extra in ([], ['--max-tokens', '4096']):
        result = subprocess.run(['bash', str(SCRIPT.parent / 'run.sh'), mode, *extra],
                                env=env, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        args = (task / 'args.txt').read_text().splitlines()
        assert Path(args[0]).name == entry
        values = [args[i + 1] for i, value in enumerate(args) if value == '--max-tokens']
        assert values == (["8192", "4096"] if extra else ["8192"])


def test_external_rock_and_selected_task(prerequisites):
    _, task = prerequisites
    task_dir = task / 'assets/harbor-tasks/pallets__flask-5014'
    task_dir.mkdir()
    (task_dir / 'task.toml').write_text('[environment]\n')
    result, config = run_config(prerequisites, '--task-id', 'pallets__flask-5014',
                                '--rock-base-url', 'https://rock.example.org/admin/',
                                '--rock-cluster', 'gpu-sandboxes')
    assert result.returncode == 0, result.stderr
    harbor = yaml.safe_load((config / 'harbor.yaml').read_text())
    assert harbor['environment']['base_url'] == 'https://rock.example.org/admin'
    assert harbor['environment']['cluster'] == 'gpu-sandboxes'
    assert all(d['task_names'] == ['pallets__flask-5014'] for d in harbor['datasets'])
    manifest = json.loads((config / 'manifest.json').read_text())
    assert manifest['task_id'] == 'pallets__flask-5014'
    assert manifest['rock_cluster'] == 'gpu-sandboxes'


@pytest.mark.parametrize('url', ['file:///tmp/rock', 'http://user:password@rock.example.org',
                                'https://rock.example.org?token=secret'])
def test_reject_invalid_rock_endpoint(prerequisites, url):
    result, _ = run_config(prerequisites, '--rock-base-url', url)
    assert result.returncode != 0
    assert '--rock-base-url' in result.stderr


def test_selected_task_must_be_prepared(prerequisites):
    result, _ = run_config(prerequisites, '--task-id', 'pallets__flask-5014')
    assert result.returncode != 0
    assert 'Missing prerequisite' in result.stderr
