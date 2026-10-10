"""CPU tests extract pure helpers, avoiding optional Harbor/GPU imports."""
import ast
import asyncio
import hashlib
import importlib.metadata
import importlib.util
import json
import inspect
from pathlib import Path
import tempfile
import tarfile
import io
import sys
import subprocess
import os
import shlex
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
import zipfile


def helpers():
    source = Path(__file__).resolve().parents[3] / 'examples/tinker_quick_start/public_swe_agent.py'
    tree = ast.parse(source.read_text())
    selected = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.Assign))]
    ns = dict(Path=Path, hashlib=hashlib, json=json, zipfile=zipfile, importlib=__import__('importlib'), tarfile=tarfile)
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(source), 'exec'), ns)
    return ns


class PublicAgentTests(unittest.TestCase):
    def test_copies_complete_default_scaffold_and_uses_real_public_fields(self):
        import yaml
        ns = helpers()
        with tempfile.TemporaryDirectory() as directory:
            default, output = Path(directory) / 'default.yaml', Path(directory) / 'public.yaml'
            scaffold = {'agent': {'templates': {'system_template': 'official', 'instance_template': 'issue'},
                                  'tools': {'bundles': [{'path': 'tools/registry'}]},
                                  'history_processors': [{'type': 'cache_control', 'last_n_messages': 2}]}}
            default.write_text(yaml.safe_dump(scaffold))
            config = ns['build_public_config'](default, output, max_turns=8, max_tokens=512)
            for key in ('templates', 'history_processors'):
                self.assertEqual(config['agent'][key], scaffold['agent'][key])
            model = config['agent']['model']
            self.assertEqual(model['per_instance_call_limit'], 7)
            self.assertEqual(model['completion_kwargs']['max_tokens'], 512)
            self.assertNotIn('max_iterations', config['agent'])
            self.assertEqual(config['env']['post_startup_commands'], ns['PUBLIC_TOOL_STARTUP_COMMANDS'])
            self.assertEqual(yaml.safe_load(output.read_text()), config)
            rebuilt = ns['build_public_config'](output, output, max_turns=8, max_tokens=512)
            self.assertEqual(rebuilt['env']['post_startup_commands'], ns['PUBLIC_TOOL_STARTUP_COMMANDS'])
            expanded = ns['build_public_config'](default, output, max_turns=32, max_tokens=1024)
            self.assertEqual(expanded['agent']['model']['per_instance_call_limit'], 31)
            self.assertEqual(expanded['agent']['model']['max_output_tokens'], 1024)
            self.assertEqual(expanded['agent']['model']['max_input_tokens'], 7168)

    def test_budget_options_compile_using_actual_public_harbor_options(self):
        from typing import Annotated, Any
        from pydantic import Field, field_validator
        wheel = Path('/root/tinker-swe-verified/assets/harbor-0.24.0-py3-none-any.whl')
        if not wheel.is_file():
            self.skipTest('Pinned public Harbor wheel is not available on this host')
        ns = {'__name__': 'public_budget_test'}
        with zipfile.ZipFile(wheel) as archive:
            exec(compile(archive.read('harbor/agents/options.py'), 'public_harbor_options', 'exec'), ns)
            tree = ast.parse(archive.read('harbor/agents/installed/swe_agent.py'))
        original = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'SweAgentOptions')
        ns.update(Annotated=Annotated, Any=Any, Field=Field, field_validator=field_validator)
        exec(compile(ast.Module(body=[original], type_ignores=[]), 'public_swe_options', 'exec'), ns)
        source = Path(__file__).resolve().parents[3] / 'examples/tinker_quick_start/public_swe_agent.py'
        extension = next(node for node in ast.parse(source.read_text()).body if isinstance(node, ast.ClassDef) and node.name == 'PublicSweAgentOptions')
        exec(compile(ast.Module(body=[extension], type_ignores=[]), str(source), 'exec'), ns)
        # CLI roundtrip uses a native tool-policy fixture, independent of a driver.
        policy = {
            'max_observation_length': 2000,
            'next_step_truncated_observation_template':
                'Observation: {{observation[:1500]}}<response clipped>{{observation[-500:]}}'
                '<NOTE>{{elided_chars}} characters were elided.</NOTE>',
            'history_processors': [
                {'type': 'last_n_observations', 'n': 2, 'polling': 1},
                {'type': 'cache_control', 'last_n_messages': 2},
            ],
        }
        options = ns['PublicSweAgentOptions'](per_instance_call_limit='31', max_output_tokens='1024',
            max_observation_length=str(policy['max_observation_length']),
            next_step_truncated_observation_template=policy['next_step_truncated_observation_template'],
            history_processors=json.dumps(policy['history_processors']),
            max_input_tokens='7168', completion_kwargs='{"max_tokens":1024}')
        flags = shlex.split(' '.join(ns['compile_cli_from_options'](options)))
        self.assertEqual(flags[flags.index('--agent.model.per_instance_call_limit') + 1], '31')
        self.assertEqual(flags[flags.index('--agent.model.max_output_tokens') + 1], '1024')
        self.assertEqual(flags[flags.index('--agent.model.max_input_tokens') + 1], '7168')
        self.assertEqual(json.loads(flags[flags.index('--agent.model.completion_kwargs') + 1]), {'max_tokens': 1024})
        self.assertEqual(flags[flags.index('--agent.templates.max_observation_length') + 1], '2000')
        self.assertEqual(flags[flags.index('--agent.templates.next_step_truncated_observation_template') + 1], policy['next_step_truncated_observation_template'])
        self.assertEqual(json.loads(flags[flags.index('--agent.history_processors') + 1]), policy['history_processors'])
        # The original public parser/schema runs in an existing CPU-only probe.
        # No environment or model is started by this config validation.
        python = Path('/root/tinker-swe-verified/work/sweagent-cpu-smoke/venv/bin/python')
        if python.is_file():
            script = """import sys,json
from sweagent.run.run_single import RunSingleConfig, BasicCLI
from sweagent.agent.agents import DefaultAgent
from types import SimpleNamespace
from jinja2 import Template
payload=json.load(sys.stdin)
cfg=BasicCLI(RunSingleConfig, help_text="CPU config contract").get_config(payload["args"])
assert cfg.agent.model.per_instance_call_limit==31
assert cfg.agent.model.max_output_tokens==1024
assert cfg.agent.templates.max_observation_length==2000
assert cfg.agent.history_processors[0].type=='last_n_observations'
assert cfg.agent.history_processors[0].n==2 and cfg.agent.history_processors[0].polling==1
# Execute the actual official clip branch and actual official Jinja rendering,
# with a history sink. No model, tool, or environment is started.
history=[]
agent=SimpleNamespace(name='main',templates=cfg.agent.templates,
    _append_history=history.append, _get_format_dict=lambda **kw: kw,
    logger=SimpleNamespace(info=lambda *a,**kw: None))
agent._add_templated_messages_to_history=lambda *a,**kw: DefaultAgent._add_templated_messages_to_history(agent,*a,**kw)
observation='HEAD'+('x'*4990)+'REAL_FAILED_RESULT'
execution_result={'exit_code':17,'output':observation}
step=SimpleNamespace(output='tool action',thought='short',action='real command',
    tool_calls=[{'id':'call-real'}],thinking_blocks=None,observation=observation,
    tool_call_ids=['call-real'],state={})
DefaultAgent.add_step_to_history(agent,step)
clipped=history[-1]['content']
assert '<response clipped>' in clipped and 'HEAD' in clipped and 'REAL_FAILED_RESULT' in clipped
assert str(len(observation)-2000)+' characters were elided' in clipped
assert observation[1500:-500] not in clipped
assert step.observation==observation and execution_result=={'exit_code':17,'output':observation}
assert 'exit_code' not in cfg.agent.templates.next_step_truncated_observation_template
assert 'PASSED' not in clipped
# Short observations use the unchanged normal template and are not clipped.
step.observation='real short output'
DefaultAgent.add_step_to_history(agent,step)
assert 'real short output' in history[-1]['content'] and '<response clipped>' not in history[-1]['content']
long_history=[{'role':'system','content':'original system'},
              {'role':'user','message_type':'observation','content':'original task'}]
for i in range(6):
    long_history += [{'role':'assistant','message_type':'action','content':'action '+str(i)},
                     {'role':'user','message_type':'observation','content':'observation '+str(i)}]
processed=cfg.agent.history_processors[0](long_history)
assert processed[0]['content']=='original system' and processed[1]['content']=='original task'
assert sum(item.get('message_type')=='action' for item in processed)==6
assert sum('Old environment output:' in item['content'] for item in processed)==4
assert processed[-1]['content']=='observation 5' and processed[-3]['content']=='observation 4'
assert long_history[3]['content']=='observation 0'
print("actual RunSingleConfig CLI/Jinja multiline roundtrip passed")
"""
            # Validate CLI/template options on the host using the probe's tools.
            import yaml
            probe_config = yaml.safe_load((source.parent / 'public-sweagent.yaml').read_text())
            for bundle in probe_config['agent']['tools']['bundles']:
                bundle['path'] = 'tools/' + Path(bundle['path']).name
            config_file = tempfile.NamedTemporaryFile(mode='w', suffix='.yaml')
            yaml.safe_dump(probe_config, config_file)
            config_file.flush()
            cfg_path = Path(config_file.name)
            environment = dict(os.environ, PYTHON_DOTENV_DISABLED='1', LITELLM_LOCAL_MODEL_COST_MAP='True', LITELLM_TELEMETRY='False')
            result = subprocess.run([str(python), '-c', script], input=json.dumps({'args':['--config',str(cfg_path),*flags]}), text=True, capture_output=True, env=environment, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('actual RunSingleConfig CLI/Jinja multiline roundtrip passed', result.stdout)


    def test_example_uses_original_system_template(self):
        import yaml
        example = Path(__file__).resolve().parents[3] / 'examples/tinker_quick_start'
        harbor = yaml.safe_load((example / 'harbor.yaml').read_text())
        self.assertNotIn('system_template', harbor['agents'][0]['kwargs'])
        public = yaml.safe_load((example / 'public-sweagent.yaml').read_text())
        self.assertEqual(public['agent']['templates']['system_template'],
                         'You are a helpful assistant that can interact with a computer to solve tasks.')

    def source_fixture(self, root, ns):
        wheel, manifest = root / 'agent.whl', root / 'source.json'
        source = root / 'swe-agent-source.tar.gz'
        code = b'public = True\n'
        with tarfile.open(source, 'w:gz') as archive:
            info = tarfile.TarInfo('SWE-agent-' + ns['SWE_AGENT_COMMIT'] + '/sweagent/__init__.py')
            info.size = len(code)
            archive.addfile(info, io.BytesIO(code))
        ns['SWE_AGENT_SOURCE_SHA256'] = hashlib.sha256(source.read_bytes()).hexdigest()
        with zipfile.ZipFile(wheel, 'w') as archive:
            archive.writestr('sweagent/__init__.py', code)
        record = {'revision': ns['SWE_AGENT_COMMIT'], 'sha256': ns['SWE_AGENT_SOURCE_SHA256'],
                  'wheel': {'sha256': hashlib.sha256(wheel.read_bytes()).hexdigest()}}
        manifest.write_text(json.dumps(record))
        package = root / 'sweagent'
        package.mkdir()
        (package / '__init__.py').write_bytes(code)
        config = root / 'config.yaml'
        config.write_text('env: {}')
        distribution = SimpleNamespace(version='1.1.0', locate_file=lambda name: root / name)
        validated = SimpleNamespace(env=SimpleNamespace(post_startup_commands=ns['PUBLIC_TOOL_STARTUP_COMMANDS']))
        run_single = SimpleNamespace(RunSingleConfig=SimpleNamespace(model_validate=lambda config: validated))
        return wheel, manifest, config, record, distribution, run_single

    def test_rejects_modified_wheel_and_installed_source_before_agent_runs(self):
        ns = helpers()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wheel, manifest, config, record, distribution, _ = self.source_fixture(root, ns)
            record['wheel']['sha256'] = '0' * 64
            manifest.write_text(json.dumps(record))
            with self.assertRaisesRegex(RuntimeError, 'checksum'):
                ns['verify_installation'](wheel, manifest, root, config)
            record['wheel']['sha256'] = hashlib.sha256(wheel.read_bytes()).hexdigest()
            manifest.write_text(json.dumps(record))
            (root / 'sweagent/__init__.py').write_text('modified = True\n')
            with patch('importlib.metadata.distribution', return_value=distribution):
                with self.assertRaisesRegex(RuntimeError, 'differs from public wheel'):
                    ns['verify_installation'](wheel, manifest, root, config)

    def test_accepts_each_recorded_build_hash_with_identical_pinned_python_source(self):
        ns = helpers()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wheel, manifest, config, record, distribution, run_single = self.source_fixture(root, ns)
            hashes = []
            with patch('importlib.metadata.distribution', return_value=distribution), patch.dict(sys.modules, {'sweagent.run.run_single': run_single}):
                for comment in [b'first public build', b'another public build']:
                    with zipfile.ZipFile(wheel, 'a') as archive:
                        archive.comment = comment
                    record['wheel']['sha256'] = hashlib.sha256(wheel.read_bytes()).hexdigest()
                    manifest.write_text(json.dumps(record))
                    result = ns['verify_installation'](wheel, manifest, root, config)
                    hashes.append(result['wheel_sha256'])
                    self.assertEqual(result['wheel_sha256'], record['wheel']['sha256'])
            self.assertNotEqual(*hashes)

    def test_recording_forged_wheel_hash_cannot_change_public_python_payload(self):
        ns = helpers()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wheel, manifest, config, record, _, _ = self.source_fixture(root, ns)
            with zipfile.ZipFile(wheel, 'w') as archive:
                archive.writestr('sweagent/__init__.py', 'tampered = True\n')
            record['wheel']['sha256'] = hashlib.sha256(wheel.read_bytes()).hexdigest()
            manifest.write_text(json.dumps(record))
            with self.assertRaisesRegex(RuntimeError, 'differs from pinned public archive'):
                ns['verify_installation'](wheel, manifest, root, config)

    def test_rejects_unpinned_revision_and_source_archive(self):
        ns = helpers()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wheel, manifest, config, record, _, _ = self.source_fixture(root, ns)
            record['revision'] = 'main'
            manifest.write_text(json.dumps(record))
            with self.assertRaisesRegex(RuntimeError, 'revision'):
                ns['verify_installation'](wheel, manifest, root, config)
            record['revision'] = ns['SWE_AGENT_COMMIT']
            manifest.write_text(json.dumps(record))
            (root / 'swe-agent-source.tar.gz').write_bytes(b'tampered source')
            with self.assertRaisesRegex(RuntimeError, 'source archive checksum'):
                ns['verify_installation'](wheel, manifest, root, config)

    def test_payload_rejects_task_paths_and_escaping_links(self):
        ns = helpers()
        with tempfile.TemporaryDirectory() as directory:
            payload = Path(directory) / 'runtime.tar.gz'
            for name, link, accepted in [
                ('opt/python312/bin/python3', '../lib/python3', True),
                ('testbed/change.py', None, False),
                ('opt/python312/../../testbed/change.py', None, False),
                ('opt/python312/bin/python3', '/testbed/python', False),
            ]:
                with tarfile.open(payload, 'w:gz') as archive:
                    entry = tarfile.TarInfo(name)
                    if link:
                        entry.type = tarfile.SYMTYPE
                        entry.linkname = link
                    archive.addfile(entry)
                if accepted:
                    ns['validate_runtime_payload'](payload)
                else:
                    with self.assertRaises(RuntimeError):
                        ns['validate_runtime_payload'](payload)

    def test_tool_runtime_preserves_logic_and_never_installs_in_task_python(self):
        helper = Path(__file__).resolve().parents[3] / 'examples/tinker_quick_start/prepare_agent_tools.py'
        spec = importlib.util.spec_from_file_location('prepare_tools', helper)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ('registry', 'edit_anthropic', 'review_on_submit_m'):
                bundle = root / 'source' / name
                (bundle / 'bin').mkdir(parents=True)
                (bundle / 'bin/tool').write_text('#!/usr/bin/env python3\nprint("unchanged")\n')
                (bundle / 'install.sh').write_text('pip install unsafe\n')
            module.prepare_agent_tools(root / 'source', root / 'runtime')
            self.assertEqual((root / 'source/edit_anthropic/install.sh').read_text(), 'pip install unsafe\n')
            self.assertEqual((root / 'runtime/edit_anthropic/bin/tool').read_text(), '#!/opt/sweagent-venv/bin/python\nprint("unchanged")\n')
            self.assertNotIn('pip install', (root / 'runtime/edit_anthropic/install.sh').read_text())
            with self.assertRaises(ValueError):
                module.prepare_agent_tools(root / 'source', root / 'runtime')

    def test_adapter_inherits_official_run_setup_and_conversion(self):
        source = Path(__file__).resolve().parents[3] / 'examples/tinker_quick_start/public_swe_agent.py'
        tree = ast.parse(source.read_text())
        cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "PreinstalledSweAgent")
        self.assertEqual(cls.bases[0].id, 'SweAgent')
        self.assertEqual([node.name for node in cls.body if isinstance(node, ast.AsyncFunctionDef)], ['install'])
        self.assertFalse(any(isinstance(node, ast.FunctionDef) and node.name in ('run', 'setup', 'name') for node in cls.body))

    def test_install_checks_pin_and_only_executes_offline_validation(self):
        async def run():
            source = Path(__file__).resolve().parents[3] / 'examples/tinker_quick_start/public_swe_agent.py'
            cls = next(node for node in ast.parse(source.read_text()).body if isinstance(node, ast.ClassDef) and node.name == "PreinstalledSweAgent")
            ns = helpers()
            ns.update(SweAgent=type('OfficialSweAgentStub', (), {}), PublicSweAgentOptions=type('OptionsStub', (), {}), inspect=inspect, shlex=shlex)
            exec(compile(ast.Module(body=[cls], type_ignores=[]), str(source), 'exec'), ns)
            agent = ns['PreinstalledSweAgent']()
            agent._version = 'main'
            with self.assertRaisesRegex(RuntimeError, 'fixed public'):
                await agent.install(None)
            agent._version = ns['SWE_AGENT_COMMIT']
            agent._get_env = lambda name: '/opt/sweagent-configs/public.yaml'
            agent.exec_as_root = AsyncMock(return_value=SimpleNamespace(return_code=0, stderr=''))
            environment = SimpleNamespace(upload_file=AsyncMock())
            with patch.dict(ns, validate_runtime_payload=lambda path: None):
                await agent.install(environment)
            environment.upload_file.assert_awaited_once_with(ns['RUNTIME_PAYLOAD'], '/tmp/sweagent-runtime.tar.gz')
            self.assertIn('--keep-old-files', agent.exec_as_root.call_args_list[0].kwargs['command'])
            command = agent.exec_as_root.call_args.kwargs['command']
            self.assertNotIn('git clone', command)
            self.assertNotIn('pip install', command)
            self.assertNotIn('http://', command)
            validation = shlex.split(command)[-1]
            compile(validation, '<inner-validation>', 'exec')
            self.assertIn('verify_installation', validation)
            self.assertIn('/opt/sweagent-artifacts/sweagent-1.1.0-py3-none-any.whl', validation)
            self.assertIn('RunSingleConfig.model_validate(config)', validation)
            self.assertIn('PUBLIC_TOOL_STARTUP_COMMANDS=', validation)
        asyncio.run(run())


class PublicBundlePreparationTests(unittest.TestCase):
    def setUp(self):
        source = Path(__file__).resolve().parents[3] / 'examples/tinker_quick_start/prepare_public_bundle.py'
        spec = importlib.util.spec_from_file_location('public_bundle_under_test', source)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)

    def test_existing_identical_symlinks_merge_idempotently_without_removing_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, destination = root / 'source', root / 'destination'
            source.mkdir()
            (source / 'python3.12').write_bytes(b'public executable')
            (source / 'python3').symlink_to('python3.12')
            self.module.merge_identical_tree(source, destination)
            (destination / 'user-note').write_text('retain me')
            self.module.merge_identical_tree(source, destination)
            self.assertEqual(os.readlink(destination / 'python3'), 'python3.12')
            self.assertEqual((destination / 'user-note').read_text(), 'retain me')
            (destination / 'python3').unlink()
            (destination / 'python3').symlink_to('user-note')
            with self.assertRaisesRegex(ValueError, 'conflicting asset symlink'):
                self.module.merge_identical_tree(source, destination)
            self.assertEqual(os.readlink(destination / 'python3'), 'user-note')
            self.assertEqual((destination / 'user-note').read_text(), 'retain me')

    def test_cache_hit_is_verified_and_requires_no_network(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = root / 'cache'
            cache.mkdir()
            data = b'public fixture'
            digest = hashlib.sha256(data).hexdigest()
            (cache / digest).write_bytes(data)
            output = root / 'asset'
            with patch.object(self.module.urllib.request, 'urlopen', side_effect=AssertionError('network')):
                self.assertEqual(self.module.fetch('https://pypi.org/fixture', output, digest, cache), 'sha256_download_cache')
                self.assertEqual(self.module.fetch('https://pypi.org/fixture', output, digest, cache), 'existing_verified_file')
            self.assertEqual(output.read_bytes(), data)

    def test_modified_cache_and_existing_asset_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = root / 'cache'
            cache.mkdir()
            digest = hashlib.sha256(b'expected').hexdigest()
            (cache / digest).write_bytes(b'modified')
            with self.assertRaisesRegex(ValueError, 'cache checksum'):
                self.module.fetch('https://pypi.org/fixture', root / 'asset', digest, cache)
            (root / 'asset').write_bytes(b'unknown existing')
            with self.assertRaisesRegex(ValueError, 'Existing asset checksum'):
                self.module.fetch('https://pypi.org/fixture', root / 'asset', digest, cache)
            self.assertEqual((root / 'asset').read_bytes(), b'unknown existing')

    def test_recorded_bundle_tampering_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'asset').write_bytes(b'original')
            manifest = {'revision': self.module.COMMIT, 'completed_stages': [], 'files': {'asset': {'sha256': hashlib.sha256(b'original').hexdigest()}}}
            (root / 'bundle-manifest.json').write_text(json.dumps(manifest))
            args = SimpleNamespace(work_dir=root, rock_root=root, index_url='https://pypi.org/simple')
            bundle = self.module.Bundle(args)
            bundle.record(root / 'asset', retrieval='public_download')
            bundle.record(root / 'asset', retrieval='existing_verified_file')
            self.assertEqual(bundle.manifest['files']['asset']['first_retrieval'], 'public_download')
            self.assertEqual(bundle.manifest['files']['asset']['retrieval'], 'existing_verified_file')
            (root / 'asset').write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError, 'Recorded asset missing or modified'):
                self.module.Bundle(args)


if __name__ == '__main__':
    unittest.main()
