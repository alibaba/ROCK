import pytest

from rock.sdk.envhub.datasets.metadata_client import DatasetMetadataClient
from rock.sdk.envhub.datasets.models import PageResult, TaskEntry


@pytest.fixture()
def client(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'test.db'}"
    return DatasetMetadataClient(db_url)


class TestDatasetCRUD:
    def test_register_and_get_dataset(self, client):
        ds = client.register_dataset("org1", "bench", description="A benchmark", tags=["nlp"])
        assert ds.org == "org1"
        assert ds.name == "bench"

        info = client.get_dataset("org1", "bench")
        assert info is not None
        assert info.id == "org1/bench"
        assert info.description == "A benchmark"
        assert info.tags == ["nlp"]

    def test_get_dataset_not_found(self, client):
        assert client.get_dataset("no", "exist") is None

    def test_list_datasets(self, client):
        client.register_dataset("org1", "ds1")
        client.register_dataset("org2", "ds2")

        page = client.list_datasets()
        assert isinstance(page, PageResult)
        assert page.total == 2
        assert len(page.items) == 2

    def test_list_datasets_with_org_filter(self, client):
        client.register_dataset("org1", "ds1")
        client.register_dataset("org2", "ds2")

        page = client.list_datasets("org1")
        assert page.total == 1
        assert page.items[0].id == "org1/ds1"

    def test_list_datasets_with_query(self, client):
        client.register_dataset("org1", "swe-bench")
        client.register_dataset("org1", "humaneval")

        page = client.list_datasets(query="swe")
        assert page.total == 1

    def test_list_datasets_with_pagination(self, client):
        for i in range(5):
            client.register_dataset("org", f"ds{i}")

        page = client.list_datasets(offset=2, limit=2)
        assert page.total == 5
        assert len(page.items) == 2
        assert page.offset == 2

    def test_delete_dataset(self, client):
        client.register_dataset("org1", "bench")
        assert client.delete_dataset("org1", "bench") is True
        assert client.get_dataset("org1", "bench") is None

    def test_delete_dataset_not_found(self, client):
        assert client.delete_dataset("no", "exist") is False

    def test_register_dataset_upsert(self, client):
        client.register_dataset("org1", "bench", description="v1")
        client.register_dataset("org1", "bench", description="v2")

        info = client.get_dataset("org1", "bench")
        assert info.description == "v2"


class TestInstanceCRUD:
    def test_register_and_get_instance(self, client):
        client.register_dataset("org1", "bench")
        inst = client.register_instance("org1", "bench", "test", "task-001", description="first task")
        assert inst.name == "task-001"

        got = client.get_instance("org1", "bench", "test", "task-001")
        assert got is not None
        assert got.name == "task-001"

    def test_get_instance_not_found(self, client):
        assert client.get_instance("no", "exist", "test", "task") is None

    def test_register_instance_auto_creates_dataset(self, client):
        inst = client.register_instance("org1", "newds", "test", "task-001")
        assert inst.name == "task-001"
        info = client.get_dataset("org1", "newds")
        assert info is not None

    def test_register_instances_batch(self, client):
        client.register_dataset("org1", "bench")
        count = client.register_instances_batch(
            "org1",
            "bench",
            "test",
            [
                {"name": "t1", "type": "directory"},
                {"name": "t2", "type": "file"},
            ],
        )
        assert count == 2

    def test_delete_instance(self, client):
        client.register_dataset("org1", "bench")
        client.register_instance("org1", "bench", "test", "task-001")
        assert client.delete_instance("org1", "bench", "test", "task-001") is True
        assert client.get_instance("org1", "bench", "test", "task-001") is None

    def test_delete_instance_not_found(self, client):
        assert client.delete_instance("no", "exist", "test", "task") is False

    def test_recalculate_task_counts(self, client):
        client.register_dataset("org1", "bench")
        client.register_instance("org1", "bench", "test", "t1")
        client.register_instance("org1", "bench", "test", "t2")
        client.register_instance("org1", "bench", "train", "t3")

        counts = client.recalculate_task_counts("org1", "bench")
        assert counts == {"test": 2, "train": 1}


class TestListing:
    def test_list_organizations(self, client):
        client.register_dataset("alpha", "ds1")
        client.register_dataset("beta", "ds2")

        page = client.list_organizations()
        assert set(page.items) == {"alpha", "beta"}
        assert page.total == 2

    def test_list_org_datasets(self, client):
        client.register_dataset("org1", "a")
        client.register_dataset("org1", "b")
        client.register_dataset("org2", "c")

        page = client.list_org_datasets("org1")
        assert sorted(page.items) == ["a", "b"]

    def test_list_dataset_splits(self, client):
        client.register_dataset("org1", "bench")
        client.register_instance("org1", "bench", "test", "t1")
        client.register_instance("org1", "bench", "train", "t2")

        splits = client.list_dataset_splits("org1", "bench")
        assert sorted(splits) == ["test", "train"]

    def test_list_dataset_splits_empty(self, client):
        assert client.list_dataset_splits("no", "exist") == []

    def test_list_dataset_tasks(self, client):
        client.register_dataset("org1", "bench")
        client.register_instance("org1", "bench", "test", "task-001")
        client.register_instance("org1", "bench", "test", "task-002")

        page = client.list_dataset_tasks("org1", "bench", "test")
        assert page.total == 2
        assert sorted(page.items) == ["task-001", "task-002"]

    def test_list_dataset_tasks_with_query(self, client):
        client.register_dataset("org1", "bench")
        client.register_instance("org1", "bench", "test", "django__django-001")
        client.register_instance("org1", "bench", "test", "flask__flask-001")

        page = client.list_dataset_tasks("org1", "bench", "test", query="django")
        assert page.total == 1
        assert page.items == ["django__django-001"]

    def test_list_dataset_tasks_empty(self, client):
        page = client.list_dataset_tasks("no", "exist", "test")
        assert page.total == 0
        assert page.items == []

    def test_list_dataset_task_entries(self, client):
        client.register_dataset("org1", "bench")
        client.register_instance("org1", "bench", "test", "t1", type="directory", language="python")

        page = client.list_dataset_task_entries("org1", "bench", "test")
        assert page.total == 1
        entry = page.items[0]
        assert isinstance(entry, TaskEntry)
        assert entry.name == "t1"
        assert entry.language == "python"

    def test_list_dataset_task_entries_with_query(self, client):
        client.register_dataset("org1", "bench")
        client.register_instance("org1", "bench", "test", "abc")
        client.register_instance("org1", "bench", "test", "xyz")

        page = client.list_dataset_task_entries("org1", "bench", "test", query="abc")
        assert page.total == 1


class TestImageCRUD:
    def test_register_and_get_image(self, client):
        img = client.register_image("docker.io/org/img:v1", status="ready")
        assert img.source_image_uri == "docker.io/org/img:v1"

        got = client.get_image("docker.io/org/img:v1")
        assert got is not None
        assert got.status == "ready"

    def test_get_image_not_found(self, client):
        assert client.get_image("nonexistent") is None

    def test_list_images(self, client):
        client.register_image("img1")
        client.register_image("img2", status="ready")

        page = client.list_images()
        assert page.total == 2

        page = client.list_images(status="ready")
        assert page.total == 1

    def test_update_image(self, client):
        client.register_image("img1")
        updated = client.update_image("img1", status="ready", image_uri_sg="sg-uri")
        assert updated is not None
        assert updated.status == "ready"
        assert updated.image_uri_sg == "sg-uri"

    def test_update_image_not_found(self, client):
        assert client.update_image("nonexistent", status="ready") is None

    def test_delete_image(self, client):
        client.register_image("img1")
        assert client.delete_image("img1") is True
        assert client.get_image("img1") is None

    def test_delete_image_not_found(self, client):
        assert client.delete_image("nonexistent") is False


class TestPermissionCRUD:
    def test_grant_and_get_permission(self, client):
        client.register_dataset("org1", "bench")
        perm = client.grant_permission("org1", "bench", "user1", "editor", granted_by="admin")
        assert perm.user_id == "user1"
        assert perm.role == "editor"

        info = client.get_permission("org1", "bench", "user1")
        assert info is not None
        assert info.role == "editor"

    def test_get_permission_not_found(self, client):
        assert client.get_permission("no", "exist", "user1") is None

    def test_revoke_permission(self, client):
        client.register_dataset("org1", "bench")
        client.grant_permission("org1", "bench", "user1")
        assert client.revoke_permission("org1", "bench", "user1") is True
        assert client.get_permission("org1", "bench", "user1") is None

    def test_list_dataset_permissions(self, client):
        client.register_dataset("org1", "bench")
        client.grant_permission("org1", "bench", "user1")
        client.grant_permission("org1", "bench", "user2")

        page = client.list_dataset_permissions("org1", "bench")
        assert page.total == 2

    def test_list_user_permissions(self, client):
        client.register_dataset("org1", "bench")
        client.register_dataset("org2", "ds2")
        client.grant_permission("org1", "bench", "user1")
        client.grant_permission("org2", "ds2", "user1")

        page = client.list_user_permissions("user1")
        assert page.total == 2

    def test_grant_permission_dataset_not_found(self, client):
        with pytest.raises(ValueError, match="not found"):
            client.grant_permission("no", "exist", "user1")


class TestAudit:
    def test_log_and_list_events(self, client):
        event = client.log_event("dataset", "org1/bench", "create", "admin", {"action": "registered"})
        assert event.target_type == "dataset"

        page = client.list_audit_events(target_type="dataset")
        assert page.total == 1
        assert page.items[0].operator == "admin"

    def test_list_audit_events_filters(self, client):
        client.log_event("dataset", "org1/bench", "create", "admin")
        client.log_event("image", "img1", "sync", "system")

        page = client.list_audit_events(target_type="image")
        assert page.total == 1
        assert page.items[0].target_id == "img1"

    def test_list_audit_events_empty(self, client):
        page = client.list_audit_events()
        assert page.total == 0
