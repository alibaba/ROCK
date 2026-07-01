from __future__ import annotations

from typing import TYPE_CHECKING, Any

from rock.sdk.envhub.datasets.models import (
    AuditEventInfo,
    DatasetInfo,
    ImageInfo,
    PageResult,
    PermissionInfo,
    TaskEntry,
)

if TYPE_CHECKING:
    from rock.sdk.envhub.datasets.database import Dataset, DatasetPermission, Image, Instance
    from rock.sdk.envhub.datasets.registry.db import DbDatasetRegistry


class DatasetMetadataClient:
    """Pure DB-backed metadata client for dataset management.

    Handles all metadata CRUD (datasets, instances, images, permissions, audit)
    via PostgreSQL. File operations (browse, read, download, upload, sync)
    should use ``OssDatasetRegistry`` separately.
    """

    def __init__(
        self,
        db_url: str,
        pool_size: int = 10,
        max_overflow: int = 20,
        pool_timeout: int = 30,
        pool_recycle: int = 1800,
        pool_pre_ping: bool = True,
    ) -> None:
        from rock.sdk.envhub.datasets.registry.db import DbDatasetRegistry

        self._db = DbDatasetRegistry(
            db_url,
            pool_size=pool_size,
            max_overflow=max_overflow,
            pool_timeout=pool_timeout,
            pool_recycle=pool_recycle,
            pool_pre_ping=pool_pre_ping,
        )

    # ── Dataset ──

    def register_dataset(
        self,
        org: str,
        name: str,
        *,
        description: str = "",
        tags: list[str] | None = None,
        owner: str = "",
        homepage: str | None = None,
        repo: str | None = None,
        paper: str | None = None,
        leaderboard: str | None = None,
        logo_url: str | None = None,
        os: str | None = None,
        version: str | None = None,
    ) -> Dataset:
        return self._db.register_dataset(
            org,
            name,
            description=description,
            tags=tags,
            owner=owner,
            homepage=homepage,
            repo=repo,
            paper=paper,
            leaderboard=leaderboard,
            logo_url=logo_url,
            os=os,
            version=version,
        )

    def list_datasets(
        self,
        org: str | None = None,
        *,
        query: str | None = None,
        offset: int = 0,
        limit: int | None = None,
    ) -> PageResult[DatasetInfo]:
        return self._db.list_datasets(org, query=query, offset=offset, limit=limit)

    def get_dataset(self, org: str, dataset: str) -> DatasetInfo | None:
        return self._db.get_dataset(org, dataset)

    def delete_dataset(self, org: str, dataset: str) -> bool:
        return self._db.delete_dataset(org, dataset)

    # ── Instance ──

    def register_instance(
        self,
        org: str,
        dataset: str,
        split: str,
        instance_name: str,
        *,
        description: str = "",
        type: str = "directory",
        format: str | None = None,
        repo: str | None = None,
        language: str | None = None,
        difficulty: str | None = None,
        base_commit: str | None = None,
        image_uris: list[str] | None = None,
        raw: str | None = None,
        source_revision: str | None = None,
        imported_from: str | None = None,
        created_by: str | None = None,
    ) -> Instance:
        return self._db.register_instance(
            org,
            dataset,
            split,
            instance_name,
            description=description,
            type=type,
            format=format,
            repo=repo,
            language=language,
            difficulty=difficulty,
            base_commit=base_commit,
            image_uris=image_uris,
            raw=raw,
            source_revision=source_revision,
            imported_from=imported_from,
            created_by=created_by,
        )

    def register_instances_batch(self, org: str, dataset: str, split: str, instances: list[dict[str, Any]]) -> int:
        return self._db.register_instances_batch(org, dataset, split, instances)

    def get_instance(self, org: str, dataset: str, split: str, instance_name: str) -> Instance | None:
        return self._db.get_instance(org, dataset, split, instance_name)

    def delete_instance(self, org: str, dataset: str, split: str, instance_name: str) -> bool:
        return self._db.delete_instance(org, dataset, split, instance_name)

    def recalculate_task_counts(self, org: str, dataset: str) -> dict[str, int]:
        return self._db.recalculate_task_counts(org, dataset)

    # ── Listing ──

    def list_organizations(self, *, offset: int = 0, limit: int | None = None) -> PageResult[str]:
        return self._db.list_organizations(offset=offset, limit=limit)

    def list_org_datasets(self, org: str, *, offset: int = 0, limit: int | None = None) -> PageResult[str]:
        return self._db.list_org_datasets(org, offset=offset, limit=limit)

    def list_dataset_splits(self, org: str, dataset: str) -> list[str]:
        return self._db.list_dataset_splits(org, dataset)

    def list_dataset_tasks(
        self,
        org: str,
        dataset: str,
        split: str,
        *,
        query: str | None = None,
        offset: int = 0,
        limit: int | None = None,
    ) -> PageResult[str]:
        return self._db.list_dataset_tasks(org, dataset, split, query=query, offset=offset, limit=limit)

    def list_dataset_task_entries(
        self,
        org: str,
        dataset: str,
        split: str,
        *,
        query: str | None = None,
        offset: int = 0,
        limit: int | None = None,
    ) -> PageResult[TaskEntry]:
        return self._db.list_dataset_task_entries(org, dataset, split, query=query, offset=offset, limit=limit)

    # ── Image ──

    def register_image(
        self,
        source_image_uri: str,
        *,
        image_uri_sg: str | None = None,
        image_uri_sh: str | None = None,
        image_hash: str | None = None,
        status: str = "pending",
        created_by: str = "system",
    ) -> Image:
        return self._db.register_image(
            source_image_uri,
            image_uri_sg=image_uri_sg,
            image_uri_sh=image_uri_sh,
            image_hash=image_hash,
            status=status,
            created_by=created_by,
        )

    def get_image(self, source_image_uri: str) -> Image | None:
        return self._db.get_image(source_image_uri)

    def list_images(
        self,
        *,
        status: str | None = None,
        offset: int = 0,
        limit: int | None = None,
    ) -> PageResult[ImageInfo]:
        return self._db.list_images(status=status, offset=offset, limit=limit)

    def update_image(
        self,
        source_image_uri: str,
        *,
        status: str | None = None,
        image_uri_sg: str | None = None,
        image_uri_sh: str | None = None,
        image_hash: str | None = None,
        last_error: str | None = None,
        last_job_id: str | None = None,
    ) -> Image | None:
        return self._db.update_image(
            source_image_uri,
            status=status,
            image_uri_sg=image_uri_sg,
            image_uri_sh=image_uri_sh,
            image_hash=image_hash,
            last_error=last_error,
            last_job_id=last_job_id,
        )

    def delete_image(self, source_image_uri: str) -> bool:
        return self._db.delete_image(source_image_uri)

    # ── Permission ──

    def grant_permission(
        self,
        org: str,
        dataset: str,
        user_id: str,
        role: str = "viewer",
        *,
        granted_by: str | None = None,
    ) -> DatasetPermission:
        return self._db.grant_permission(org, dataset, user_id, role, granted_by=granted_by)

    def revoke_permission(self, org: str, dataset: str, user_id: str) -> bool:
        return self._db.revoke_permission(org, dataset, user_id)

    def get_permission(self, org: str, dataset: str, user_id: str) -> PermissionInfo | None:
        return self._db.get_permission(org, dataset, user_id)

    def list_dataset_permissions(
        self,
        org: str,
        dataset: str,
        *,
        offset: int = 0,
        limit: int | None = None,
    ) -> PageResult[PermissionInfo]:
        return self._db.list_dataset_permissions(org, dataset, offset=offset, limit=limit)

    def list_user_permissions(
        self,
        user_id: str,
        *,
        offset: int = 0,
        limit: int | None = None,
    ) -> PageResult[PermissionInfo]:
        return self._db.list_user_permissions(user_id, offset=offset, limit=limit)

    # ── Audit ──

    def log_event(
        self,
        target_type: str,
        target_id: str,
        event_type: str,
        operator: str,
        changes: dict | None = None,
    ):
        return self._db.log_event(target_type, target_id, event_type, operator, changes)

    def list_audit_events(
        self,
        *,
        target_type: str | None = None,
        target_id: str | None = None,
        event_type: str | None = None,
        operator: str | None = None,
        offset: int = 0,
        limit: int | None = None,
    ) -> PageResult[AuditEventInfo]:
        return self._db.list_audit_events(
            target_type=target_type,
            target_id=target_id,
            event_type=event_type,
            operator=operator,
            offset=offset,
            limit=limit,
        )
