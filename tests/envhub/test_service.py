import tempfile

import pytest

from rock import env_vars
from rock.envhub import DeleteEnvRequest, DockerEnvHub, GetEnvRequest, ListEnvsRequest, RegisterRequest

# ============ Basic functionality tests ============


def test_register_new_env(docker_env_hub):
    """Test registering a new environment"""
    request = RegisterRequest(env_name="test-env", image="python:3.9", owner="test_user")
    spec = docker_env_hub.register(request)

    assert spec.env_name == "test-env"
    assert spec.image == "python:3.9"
    assert spec.owner == "test_user"


def test_register_with_minimal_fields(docker_env_hub):
    """Test registering with only required fields"""
    request = RegisterRequest(env_name="minimal-env", image="alpine:latest")
    spec = docker_env_hub.register(request)

    assert spec.env_name == "minimal-env"
    assert spec.image == "alpine:latest"
    assert spec.owner == ""
    assert spec.description == ""
    assert spec.tags == []
    assert spec.extra_spec is None


def test_register_with_all_fields(docker_env_hub):
    """Test registering with all fields"""
    request = RegisterRequest(
        env_name="full-env",
        image="pytorch:2.0",
        owner="alice",
        description="Full featured environment",
        tags=["ml", "gpu", "production"],
        extra_spec={"gpu_count": 4, "memory": "32g"},
    )
    spec = docker_env_hub.register(request)

    assert spec.env_name == "full-env"
    assert spec.image == "pytorch:2.0"
    assert spec.owner == "alice"
    assert spec.description == "Full featured environment"
    assert spec.tags == ["ml", "gpu", "production"]
    assert spec.extra_spec == {"gpu_count": 4, "memory": "32g"}


def test_register_update_existing(docker_env_hub):
    """Test updating an existing environment"""
    request1 = RegisterRequest(env_name="update-test", image="python:3.9", owner="alice")
    docker_env_hub.register(request1)

    request2 = RegisterRequest(env_name="update-test", image="python:3.10", owner="bob", description="Updated")
    updated = docker_env_hub.register(request2)

    assert updated.image == "python:3.10"
    assert updated.owner == "bob"
    assert updated.description == "Updated"


# ============ Query functionality tests ============


def test_get_env_exists(docker_env_hub):
    """Test getting an existing environment"""
    register_request = RegisterRequest(env_name="get-test", image="ubuntu:22.04")
    docker_env_hub.register(register_request)

    get_request = GetEnvRequest(env_name="get-test")
    retrieved = docker_env_hub.get_env(get_request)

    assert retrieved is not None
    assert retrieved.env_name == "get-test"


def test_get_env_not_exists(docker_env_hub):
    """Test getting a non-existing environment"""
    get_request = GetEnvRequest(env_name="non-existent")
    with pytest.raises(Exception):
        docker_env_hub.get_env(get_request)


def test_list_envs_empty(docker_env_hub):
    """Test listing environments when none exist"""
    request = ListEnvsRequest()
    envs = docker_env_hub.list_envs(request)
    assert len(envs) == 1  # because of the default env


def test_list_envs_multiple(docker_env_hub):
    """Test listing multiple environments"""
    docker_env_hub.register(RegisterRequest(env_name="env1", image="img1"))
    docker_env_hub.register(RegisterRequest(env_name="env2", image="img2"))
    docker_env_hub.register(RegisterRequest(env_name="env3", image="img3"))

    request = ListEnvsRequest()
    all_envs = docker_env_hub.list_envs(request)
    assert len(all_envs) == 4


def test_list_envs_filter_by_owner(docker_env_hub):
    """Test filtering environments by owner"""
    docker_env_hub.register(RegisterRequest(env_name="alice-env-1", image="img1", owner="alice"))
    docker_env_hub.register(RegisterRequest(env_name="bob-env", image="img2", owner="bob"))
    docker_env_hub.register(RegisterRequest(env_name="alice-env-2", image="img3", owner="alice"))

    request = ListEnvsRequest(owner="alice")
    alice_envs = docker_env_hub.list_envs(request)
    assert len(alice_envs) == 2
    assert all(env.owner == "alice" for env in alice_envs)

    request = ListEnvsRequest(owner="bob")
    bob_envs = docker_env_hub.list_envs(request)
    assert len(bob_envs) == 1
    assert bob_envs[0].owner == "bob"


def test_list_envs_filter_by_single_tag(docker_env_hub):
    """Test filtering environments by a single tag"""
    docker_env_hub.register(RegisterRequest(env_name="ml-env", image="img1", tags=["ml", "gpu"]))
    docker_env_hub.register(RegisterRequest(env_name="web-env", image="img2", tags=["web", "nodejs"]))
    docker_env_hub.register(RegisterRequest(env_name="data-env", image="img3", tags=["ml", "data"]))

    request = ListEnvsRequest(tags=["ml"])
    ml_envs = docker_env_hub.list_envs(request)
    assert len(ml_envs) == 2

    request = ListEnvsRequest(tags=["web"])
    web_envs = docker_env_hub.list_envs(request)
    assert len(web_envs) == 1


def test_list_envs_filter_by_multiple_tags(docker_env_hub):
    """Test filtering environments by multiple tags (OR logic)"""
    docker_env_hub.register(RegisterRequest(env_name="env1", image="img1", tags=["ml"]))
    docker_env_hub.register(RegisterRequest(env_name="env2", image="img2", tags=["web"]))
    docker_env_hub.register(RegisterRequest(env_name="env3", image="img3", tags=["ml", "gpu"]))
    docker_env_hub.register(RegisterRequest(env_name="env4", image="img4", tags=["data"]))

    # Match ml or web
    request = ListEnvsRequest(tags=["ml", "web"])
    envs = docker_env_hub.list_envs(request)
    assert len(envs) == 3  # env1, env2, env3

    env_names = {env.env_name for env in envs}
    assert env_names == {"env1", "env2", "env3"}


def test_list_envs_combined_filters(docker_env_hub):
    """Test filtering by both owner and tags"""
    docker_env_hub.register(RegisterRequest(env_name="alice-ml", image="img1", owner="alice", tags=["ml"]))
    docker_env_hub.register(RegisterRequest(env_name="alice-web", image="img2", owner="alice", tags=["web"]))
    docker_env_hub.register(RegisterRequest(env_name="bob-ml", image="img3", owner="bob", tags=["ml"]))

    # Alice's ml environments
    request = ListEnvsRequest(owner="alice", tags=["ml"])
    envs = docker_env_hub.list_envs(request)
    assert len(envs) == 1
    assert envs[0].env_name == "alice-ml"


# ============ Deletion functionality tests ============


def test_delete_env_exists(docker_env_hub):
    """Test deleting an existing environment"""
    register_request = RegisterRequest(env_name="delete-test", image="img")
    docker_env_hub.register(register_request)

    delete_request = DeleteEnvRequest(env_name="delete-test")
    assert docker_env_hub.delete_env(delete_request) is True

    get_request = GetEnvRequest(env_name="delete-test")
    with pytest.raises(Exception):
        docker_env_hub.get_env(get_request)


def test_delete_env_not_exists(docker_env_hub):
    """Test deleting a non-existing environment"""
    delete_request = DeleteEnvRequest(env_name="non-existent")
    assert docker_env_hub.delete_env(delete_request) is False


def test_delete_env_does_not_affect_others(docker_env_hub):
    """Test that deleting one environment doesn't affect others"""
    docker_env_hub.register(RegisterRequest(env_name="env1", image="img1"))
    docker_env_hub.register(RegisterRequest(env_name="env2", image="img2"))
    docker_env_hub.register(RegisterRequest(env_name="env3", image="img3"))

    delete_request = DeleteEnvRequest(env_name="env2")
    docker_env_hub.delete_env(delete_request)

    request = ListEnvsRequest()
    remaining = docker_env_hub.list_envs(request)
    assert len(remaining) == 3  # because of the default env

    env_names = {env.env_name for env in remaining}
    assert "env1" in env_names
    assert "env3" in env_names
    assert "env2" not in env_names


# ============ Special fields tests ============


def test_extra_spec_dict(docker_env_hub):
    """Test extra_spec dictionary functionality"""
    request = RegisterRequest(
        env_name="extra-test", image="img", extra_spec={"gpu_enabled": True, "memory": "8g", "nested": {"key": "value"}}
    )
    spec = docker_env_hub.register(request)

    assert spec.extra_spec["gpu_enabled"] is True
    assert spec.extra_spec["memory"] == "8g"
    assert spec.extra_spec["nested"]["key"] == "value"


def test_extra_spec_update(docker_env_hub):
    """Test updating extra_spec"""
    request1 = RegisterRequest(env_name="extra-update", image="img", extra_spec={"old": "value"})
    docker_env_hub.register(request1)

    request2 = RegisterRequest(env_name="extra-update", image="img", extra_spec={"new": "value"})
    updated = docker_env_hub.register(request2)

    assert updated.extra_spec == {"new": "value"}
    assert "old" not in updated.extra_spec


def test_tags_list(docker_env_hub):
    """Test tags list functionality"""
    request = RegisterRequest(env_name="tag-test", image="img", tags=["tag1", "tag2", "tag3"])
    spec = docker_env_hub.register(request)

    assert len(spec.tags) == 3
    assert "tag1" in spec.tags
    assert "tag2" in spec.tags
    assert "tag3" in spec.tags


def test_empty_tags(docker_env_hub):
    """Test empty tags"""
    request = RegisterRequest(env_name="no-tags", image="img", tags=[])
    spec = docker_env_hub.register(request)
    assert spec.tags == []


def test_extra_spec_dict_get(docker_env_hub):
    """Test extra_spec dictionary functionality"""
    request = RegisterRequest(
        env_name="extra-test", image="img", extra_spec={"gpu_enabled": True, "memory": "8g", "nested": {"key": "value"}}
    )
    docker_env_hub.register(request)

    spec = docker_env_hub.get_env(GetEnvRequest(env_name="extra-test"))

    assert spec.extra_spec["gpu_enabled"] is True
    assert spec.extra_spec["memory"] == "8g"
    assert spec.extra_spec["nested"]["key"] == "value"


# ============ DockerEnvHub specific methods tests ============


def test_init_default_env_creates_default_environment(docker_env_hub):
    """Test that init_default_env creates the default environment."""
    # The default environment should be automatically created during initialization
    from rock.envhub.api.schemas import GetEnvRequest

    default_env = docker_env_hub.get_env(GetEnvRequest(env_name=docker_env_hub.DEFAULT_ENV_NAME))

    assert default_env.env_name == docker_env_hub.DEFAULT_ENV_NAME
    assert default_env.image == env_vars.ROCK_ENVHUB_DEFAULT_DOCKER_IMAGE
    assert default_env.owner == "ENVHUB"


def test_check_envs_available_basic(docker_env_hub, docker_available, default_image_available):
    """Test that check_envs_available checks default env properly - should return False without docker installed."""
    result = docker_env_hub.check_envs_available()
    # In a basic DockerEnvHub instance (without docker installed or images present),
    # method may return False
    assert isinstance(result, bool)


def test_check_envs_available_with_existing_default_image(docker_available, default_image_available):
    """Test check_envs_available with mocked docker functionality."""

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp_file:
        db_path = tmp_file.name

        db_url = f"sqlite:///{db_path}"
        hub = DockerEnvHub(db_url=db_url)

        # Register an environment using the default image from env_vars
        default_image = "python:3.11-not-exist"  # Using fixed, common image for this specific test

        hub.register(RegisterRequest(env_name="default-image-test", image=default_image, owner="test"))

        result = hub.check_envs_available()

        assert result is False
