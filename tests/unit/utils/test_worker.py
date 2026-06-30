import importlib

from rock.utils.worker import resolve_workers


def test_rock_proxy_workers_defaults_to_zero(monkeypatch):
    monkeypatch.delenv("ROCK_PROXY_WORKERS", raising=False)
    import rock.env_vars as env_vars

    importlib.reload(env_vars)
    assert env_vars.ROCK_PROXY_WORKERS == 0


def test_rock_proxy_workers_reads_env(monkeypatch):
    monkeypatch.setenv("ROCK_PROXY_WORKERS", "6")
    import rock.env_vars as env_vars

    importlib.reload(env_vars)
    assert env_vars.ROCK_PROXY_WORKERS == 6


def test_resolve_workers_admin_always_one():
    assert resolve_workers("admin", override=None, env_workers=8, env="prod") == 1
    assert resolve_workers("admin", override=10, env_workers=8, env="prod") == 1


def test_resolve_workers_proxy_override_wins():
    assert resolve_workers("proxy", override=4, env_workers=8, env="prod") == 4


def test_resolve_workers_proxy_env_when_no_override():
    assert resolve_workers("proxy", override=None, env_workers=8, env="prod") == 8


def test_resolve_workers_proxy_defaults_to_one_when_unset():
    assert resolve_workers("proxy", override=None, env_workers=0, env="prod") == 1


def test_resolve_workers_local_envs_force_one_over_override():
    for env in ("local", "test", "dev"):
        assert resolve_workers("proxy", override=8, env_workers=8, env=env) == 1


def test_resolve_workers_unknown_env_defaults_param_none():
    # env defaults to None (treated as non-local) — preserves explicit override
    assert resolve_workers("proxy", override=4, env_workers=0) == 4
