"""Contracts must be importable before either the actor or its consumers."""

import subprocess
import sys

import pytest


def test_result_codes_do_not_require_python_311_strenum():
    script = """
import builtins
import json
import pickle

# Reject Python 3.11 enum imports in ROCK, without breaking the host stdlib.
original_import = builtins.__import__
def python310_enum_import(name, globals=None, locals=None, fromlist=(), level=0):
    if name == 'enum' and 'StrEnum' in (fromlist or ()) and (globals or {}).get('__name__', '').startswith('rock.'):
        raise ImportError('StrEnum is unavailable on Python 3.10')
    return original_import(name, globals, locals, fromlist, level)
builtins.__import__ = python310_enum_import
from rock.sandbox.operator.snapd import OperatorResultCode
builtins.__import__ = original_import
for code in OperatorResultCode:
    assert isinstance(code, str)
    assert str(code) == code.value
    assert json.loads(json.dumps(code)) == code.value
    assert pickle.loads(pickle.dumps(code)) is code
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("first_module", [
    "rock.sandbox.operator.snapd",
    "rock.sandbox.operator.snapd_actor",
    "rock.sandbox.operator.snapshot",
    "rock.sandbox.snapshot_types",
    "rock.admin.core.ray_service",
    "rock.admin.service.snapshot_manager",
    "rock.admin.entrypoints.e2b_api",
])
def test_cold_import_and_result_serialization(first_module):
    script = """
import importlib
import pickle
import sys

importlib.import_module(sys.argv[1])
from rock.sandbox.operator.snapd import (
    OperatorLifecycleError, OperatorResultCode, OperatorSandboxResult,
    OperatorTarget, operator_result_from_info, sandbox_info_from_result,
)
assert importlib.util.find_spec('rock.sandbox.operator.context') is None
assert OperatorSandboxResult.__module__ == 'rock.sandbox.operator.snapd'
result = OperatorSandboxResult(
    sandbox_id='cold-import', code=OperatorResultCode.RESULT_UNKNOWN,
    state='PENDING', target=OperatorTarget(node_id='worker'),
    last_error='reply lost',
)
info = sandbox_info_from_result(result)
assert operator_result_from_info(pickle.loads(pickle.dumps(info))) == result
error = pickle.loads(pickle.dumps(OperatorLifecycleError(info)))
assert str(error) == 'reply lost'
assert operator_result_from_info(error.sandbox_info) == result
"""
    result = subprocess.run(
        [sys.executable, "-c", script, first_module], capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
