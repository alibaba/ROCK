"""Public operator regressions for the original cookbook."""
import asyncio
import importlib.util
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import pytest

from rock.sdk.bench.models.job.config import HarborJobConfig

# ROLL is installed only in the separate Tinker runtime environment.
if importlib.util.find_spec("roll") is None:
    pytest.skip("Requires the optional ROLL Tinker runtime", allow_module_level=True)

from examples.tinker_quick_start import harbor_pull_runner as module


class PublicOperatorTests(unittest.TestCase):
    def make_runner(self, directory):
        return self

    def _build_job_config(self, task, name):
        return HarborJobConfig.model_validate({
            "job_name": name, "experiment_id": "public-test",
            "environment": {"image": "public-harbor:local", "cluster": "local"},
            "agents": [{"import_path": "public_swe_agent:PreinstalledSweAgent",
                        "kwargs": {"model_name": "openai/local-qwen"}}],
            "datasets": [{"path": "/opt/swe/harbor-tasks", "task_names": ["sympy__sympy-19637"]}],
        })

    def test_operator_keeps_original_trial_and_custom_port(self):
        config = self._build_job_config({}, "test")
        trials = module.PublicModelServiceOperator(port=28765).apply(config)
        self.assertEqual(len(trials), 1)
        self.assertIsInstance(trials[0], module.ModelServiceHarborTrial)
        self.assertIs(trials[0]._config, config)
        self.assertEqual(trials[0]._model_service_port, 28765)

    def test_preinstalled_runtime_has_no_download_or_pip_mirror(self):
        config = module.PythonRuntimeEnvConfig(version='3.12', pip_index_url=None)
        runtime = module.PreinstalledPythonRuntimeEnv(SimpleNamespace(), config)
        command = runtime._get_install_cmd()
        self.assertIn('ln -s /opt/rocklet runtime-env', command)
        self.assertNotIn('http', command)
        self.assertNotIn('pip', command)
        self.assertIsNot(module.PythonRuntimeEnv._REGISTRY['python'], module.PreinstalledPythonRuntimeEnv)

    def test_harbor_builds_original_task_environment_without_image_substitution(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self._build_job_config({}, 'test')
            config.datasets[0].task_names = ['django__django-12345']
            trial = module.PublicModelServiceHarborTrial(config)
            script = trial.build()
            self.assertIn('harbor jobs start -c', script)
            self.assertNotIn('inner-image.tar', script)
            public = module.public_harbor_config(config)
            self.assertEqual(public['datasets'][0]['task_names'], ['django__django-12345'])
            self.assertEqual(public['environment']['type'], 'docker')
            self.assertNotIn('override_timeout_sec', public['verifier'])
            self.assertEqual(public['verifier']['env']['UV_PYTHON'], '/opt/python312/bin/python3.12')
            config.verifier.env['UV_PYTHON'] = '/custom/python'
            self.assertEqual(module.public_harbor_config(config)['verifier']['env']['UV_PYTHON'], '/custom/python')

    def test_trial_registers_enabled_service_on_sandbox_before_install(self):
        async def run():
            with tempfile.TemporaryDirectory() as directory:
                runner = self.make_runner(directory)
                config = runner._build_job_config({'seed': 1}, 'test')
                trial = module.PublicModelServiceHarborTrial(config)
                sandbox = SimpleNamespace(arun=AsyncMock(return_value=SimpleNamespace(output='10.0.0.2')))
                service = SimpleNamespace(start=AsyncMock())
                async def install():
                    self.assertIs(sandbox.model_service, service)
                    self.assertTrue(service.config.enabled)
                service.install = AsyncMock(side_effect=install)
                def construct(bound_sandbox, service_config):
                    self.assertIs(bound_sandbox, sandbox)
                    service.config = service_config
                    return service
                with patch.object(module, 'PreinstalledModelService', side_effect=construct):
                    await trial.on_sandbox_ready(sandbox)
                self.assertIs(trial.model_service, sandbox.model_service)
                service.install.assert_awaited_once()
                service.start.assert_awaited_once()
                self.assertEqual(config.agents[0].env['OPENAI_BASE_URL'], 'http://10.0.0.2:28080/v1')
        asyncio.run(run())

    def test_backend_connection_kwargs_removed_and_model_service_env_retained(self):
        async def run():
            with tempfile.TemporaryDirectory() as directory:
                runner = self.make_runner(directory)
                config = runner._build_job_config({'seed': 0}, 'test')
                agent = config.agents[0]
                kept_kwargs = dict(agent.kwargs)
                agent.kwargs.update(api_base='http://stale-backend/v1', api_key='env-fixture')
                agent.env['OPENAI_BASE_URL'] = 'http://stale-backend/v1'
                trial = module.PublicModelServiceHarborTrial(config, model_service_port=28765)
                sandbox = SimpleNamespace(_oss=None,
                    arun=AsyncMock(return_value=SimpleNamespace(output='172.26.1.2')))
                async def install():
                    self.assertNotIn('api_base', agent.kwargs)
                    self.assertNotIn('api_key', agent.kwargs)
                    self.assertEqual(agent.kwargs, kept_kwargs)
                    self.assertEqual(agent.env['OPENAI_BASE_URL'], 'http://172.26.1.2:28765/v1')
                    self.assertEqual(agent.env['OPENAI_API_KEY'], 'public-local')
                service = SimpleNamespace(install=AsyncMock(side_effect=install), start=AsyncMock())
                with patch.object(module, 'PreinstalledModelService', return_value=service):
                    await trial.on_sandbox_ready(sandbox)
                service.install.assert_awaited_once()
                service.start.assert_awaited_once()
                self.assertIs(sandbox.model_service, service)
                self.assertEqual(agent.env['OPENAI_BASE_URL'], 'http://172.26.1.2:28765/v1')
        asyncio.run(run())

    def test_local_oss_disabled_before_model_service_install(self):
        async def run():
            with tempfile.TemporaryDirectory() as directory:
                runner = self.make_runner(directory)
                trial = module.PublicModelServiceHarborTrial(runner._build_job_config({'seed': 0}, 'test'))
                original = SimpleNamespace(ensure_setup=AsyncMock(side_effect=AssertionError('must not fetch STS')))
                sandbox = SimpleNamespace(_oss=original, arun=AsyncMock(return_value=SimpleNamespace(output='10.0.0.2')))
                service = SimpleNamespace(start=AsyncMock())
                async def install():
                    self.assertFalse(await sandbox._oss.ensure_setup())
                    self.assertFalse(sandbox._oss.is_available)
                    await sandbox._oss.close()
                service.install = AsyncMock(side_effect=install)
                with patch.object(module, 'PreinstalledModelService', return_value=service):
                    await trial.on_sandbox_ready(sandbox)
                original.ensure_setup.assert_not_awaited()
        asyncio.run(run())
