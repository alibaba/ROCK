from __future__ import annotations

import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Engine, create_engine, func, text
from sqlalchemy.orm import Session

from rock.sdk.envhub.datasets.database import AuditEvent, Base, Dataset, DatasetPermission, Image, Instance
from rock.logger import init_logger
from rock.sdk.envhub.datasets.models import (
    AuditEventInfo,
    DatasetInfo,
    ImageInfo,
    PageResult,
    PermissionInfo,
    TaskEntry,
)

logger = init_logger(__name__)

_engine_lock = threading.Lock()
_engine_cache: dict[str, Engine] = {}
_initialized_engines: set[str] = set()

_BATCH_CHUNK_SIZE = 500


@dataclass(frozen=True)
class _EngineParams:
    pool_size: int
    max_overflow: int
    pool_timeout: int
    pool_recycle: int
    pool_pre_ping: bool


_engine_params: dict[str, _EngineParams] = {}


def _get_shared_engine(
    db_url: str,
    pool_size: int = 10,
    max_overflow: int = 20,
    pool_timeout: int = 30,
    pool_recycle: int = 1800,
    pool_pre_ping: bool = True,
) -> Engine:
    with _engine_lock:
        if db_url in _engine_cache:
            requested = _EngineParams(pool_size, max_overflow, pool_timeout, pool_recycle, pool_pre_ping)
            created_with = _engine_params.get(db_url)
            if created_with and created_with != requested:
                logger.warning(
                    "Reusing engine for %s with pool params from first caller %s; "
                    "requested params %s are ignored",
                    db_url.split("@")[-1] if "@" in db_url else db_url,
                    created_with,
                    requested,
                )
            return _engine_cache[db_url]
        kwargs: dict[str, Any] = {"echo": False, "pool_pre_ping": pool_pre_ping}
        if not db_url.startswith("sqlite"):
            kwargs.update(
                pool_size=pool_size,
                max_overflow=max_overflow,
                pool_timeout=pool_timeout,
                pool_recycle=pool_recycle,
            )
        engine = create_engine(db_url, **kwargs)
        _engine_cache[db_url] = engine
        _engine_params[db_url] = _EngineParams(pool_size, max_overflow, pool_timeout, pool_recycle, pool_pre_ping)
        return engine


class DbDatasetRegistry:
    def __init__(
        self,
        db_url: str,
        pool_size: int = 10,
        max_overflow: int = 20,
        pool_timeout: int = 30,
        pool_recycle: int = 1800,
        pool_pre_ping: bool = True,
    ) -> None:
        self._engine = _get_shared_engine(
            db_url,
            pool_size=pool_size,
            max_overflow=max_overflow,
            pool_timeout=pool_timeout,
            pool_recycle=pool_recycle,
            pool_pre_ping=pool_pre_ping,
        )
        with _engine_lock:
            if db_url not in _initialized_engines:
                Base.metadata.create_all(self._engine)
                _initialized_engines.add(db_url)

    @contextmanager
    def _session(self):
        session = Session(self._engine, expire_on_commit=False)
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    # ── helpers ──

    def _get_dataset(self, session: Session, org: str, name: str) -> Dataset | None:
        return session.query(Dataset).filter(Dataset.org == org, Dataset.name == name).first()

    def _get_instance(self, session: Session, dataset_id: int, split: str, name: str) -> Instance | None:
        return (
            session.query(Instance)
            .filter(Instance.dataset_id == dataset_id, Instance.split == split, Instance.name == name)
            .first()
        )

    def _resolve_instance(
        self, session: Session, org: str, dataset: str, split: str, instance_name: str
    ) -> Instance | None:
        ds = self._get_dataset(session, org, dataset)
        if ds is None:
            return None
        return self._get_instance(session, ds.id, split, instance_name)

    def _increment_task_count(self, session: Session, dataset_id: int, split: str, delta: int = 1) -> None:
        if self._engine.dialect.name == "sqlite":
            self._update_task_count_python(session, dataset_id, split, delta)
        else:
            session.execute(
                text("""
                    UPDATE datasets SET task_counts = jsonb_set(
                        COALESCE(task_counts, '{}')::jsonb,
                        ARRAY[:split],
                        to_jsonb(COALESCE((task_counts->>:split)::int, 0) + :delta)
                    ) WHERE id = :dataset_id
                """),
                {"split": split, "delta": delta, "dataset_id": dataset_id},
            )

    def _decrement_task_count(self, session: Session, dataset_id: int, split: str, delta: int = 1) -> None:
        if self._engine.dialect.name == "sqlite":
            self._update_task_count_python(session, dataset_id, split, -delta)
        else:
            session.execute(
                text("""
                    UPDATE datasets SET task_counts =
                        CASE
                            WHEN COALESCE((task_counts->>:split)::int, 0) - :delta <= 0
                            THEN (COALESCE(task_counts, '{}')::jsonb - :split)
                            ELSE jsonb_set(
                                COALESCE(task_counts, '{}')::jsonb,
                                ARRAY[:split],
                                to_jsonb((task_counts->>:split)::int - :delta)
                            )
                        END
                    WHERE id = :dataset_id
                """),
                {"split": split, "delta": delta, "dataset_id": dataset_id},
            )

    def _update_task_count_python(self, session: Session, dataset_id: int, split: str, delta: int) -> None:
        ds = session.query(Dataset).filter(Dataset.id == dataset_id).first()
        if ds is None:
            return
        counts = dict(ds.task_counts or {})
        new_val = counts.get(split, 0) + delta
        if new_val <= 0:
            counts.pop(split, None)
        else:
            counts[split] = new_val
        ds.task_counts = counts

    # ── Registration ──

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
        with self._session() as session:
            ds = self._get_dataset(session, org, name)
            kwargs = dict(
                description=description,
                tags=tags or [],
                owner=owner,
                homepage=homepage,
                repo=repo,
                paper=paper,
                leaderboard=leaderboard,
                logo_url=logo_url,
                os=os,
                version=version,
            )
            if ds is not None:
                for key, value in kwargs.items():
                    setattr(ds, key, value)
            else:
                ds = Dataset(org=org, name=name, **kwargs)
                session.add(ds)
            session.flush()
            session.refresh(ds)
            session.expunge(ds)
            return ds

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
        with self._session() as session:
            ds = self._get_dataset(session, org, dataset)
            if ds is None:
                ds = Dataset(org=org, name=dataset)
                session.add(ds)
                session.flush()

            inst = self._get_instance(session, ds.id, split, instance_name)
            kwargs = dict(
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
            is_new = inst is None
            if inst is not None:
                for key, value in kwargs.items():
                    setattr(inst, key, value)
            else:
                inst = Instance(dataset_id=ds.id, split=split, name=instance_name, **kwargs)
                session.add(inst)
            session.flush()
            if is_new:
                self._increment_task_count(session, ds.id, split)
            session.refresh(inst)
            session.expunge(inst)
            return inst

    _INSTANCE_FIELDS = (
        "description",
        "type",
        "size",
        "file_count",
        "etag",
        "format",
        "repo",
        "language",
        "difficulty",
        "base_commit",
        "image_uris",
        "raw",
        "source_revision",
        "imported_from",
        "created_by",
    )

    def register_instances_batch(self, org: str, dataset: str, split: str, instances: list[dict[str, Any]]) -> int:
        with self._session() as session:
            ds = self._get_dataset(session, org, dataset)
            if ds is None:
                ds = Dataset(org=org, name=dataset)
                session.add(ds)
                session.flush()

            names = [item["name"] for item in instances]
            existing_map: dict[str, Instance] = {}
            for i in range(0, len(names), _BATCH_CHUNK_SIZE):
                chunk = names[i : i + _BATCH_CHUNK_SIZE]
                rows = (
                    session.query(Instance)
                    .filter(Instance.dataset_id == ds.id, Instance.split == split, Instance.name.in_(chunk))
                    .all()
                )
                for inst in rows:
                    existing_map[inst.name] = inst

            new_count = 0
            for item in instances:
                inst_name = item["name"]
                inst = existing_map.get(inst_name)
                if inst is not None:
                    for key in self._INSTANCE_FIELDS:
                        if key in item:
                            setattr(inst, key, item[key])
                else:
                    inst = Instance(
                        dataset_id=ds.id,
                        split=split,
                        name=inst_name,
                        **{k: item.get(k) for k in self._INSTANCE_FIELDS if k in item},
                    )
                    if inst.description is None:
                        inst.description = ""
                    if inst.type is None:
                        inst.type = "directory"
                    session.add(inst)
                    new_count += 1
            session.flush()
            if new_count > 0:
                self._increment_task_count(session, ds.id, split, delta=new_count)
            return len(instances)

    # ── Listing ──

    def list_datasets(
        self,
        org: str | None = None,
        *,
        query: str | None = None,
        offset: int = 0,
        limit: int | None = None,
    ) -> PageResult[DatasetInfo]:
        with self._session() as session:
            q = session.query(Dataset)
            if org is not None:
                q = q.filter(Dataset.org == org)
            if query:
                pattern = f"%{query}%"
                q = q.filter((Dataset.org.ilike(pattern)) | (Dataset.name.ilike(pattern)))
            q = q.order_by(Dataset.org, Dataset.name)

            total = q.count()
            q = q.offset(offset)
            if limit is not None:
                q = q.limit(limit)

            items: list[DatasetInfo] = []
            for ds in q.all():
                tc = ds.task_counts or {}
                items.append(
                    DatasetInfo(
                        id=ds.full_name,
                        description=ds.description or "",
                        tags=ds.tags or [],
                        owner=ds.owner or "",
                        homepage=ds.homepage,
                        repo=ds.repo,
                        paper=ds.paper,
                        leaderboard=ds.leaderboard,
                        logo_url=ds.logo_url,
                        os=ds.os,
                        version=ds.version,
                        splits=list(tc.keys()),
                        task_counts=tc,
                    )
                )

            return PageResult(items=items, total=total, offset=offset, limit=limit)

    def list_organizations(self, *, offset: int = 0, limit: int | None = None) -> PageResult[str]:
        with self._session() as session:
            q = session.query(Dataset.org).distinct().order_by(Dataset.org)
            total = q.count()
            q = q.offset(offset)
            if limit is not None:
                q = q.limit(limit)
            items = [row[0] for row in q.all()]
            return PageResult(items=items, total=total, offset=offset, limit=limit)

    def list_org_datasets(self, org: str, *, offset: int = 0, limit: int | None = None) -> PageResult[str]:
        with self._session() as session:
            q = session.query(Dataset.name).filter(Dataset.org == org).order_by(Dataset.name)
            total = q.count()
            q = q.offset(offset)
            if limit is not None:
                q = q.limit(limit)
            items = [row[0] for row in q.all()]
            return PageResult(items=items, total=total, offset=offset, limit=limit)

    def list_dataset_splits(self, org: str, dataset: str) -> list[str]:
        with self._session() as session:
            ds = self._get_dataset(session, org, dataset)
            if ds is None:
                return []
            rows = (
                session.query(Instance.split)
                .filter(Instance.dataset_id == ds.id)
                .distinct()
                .order_by(Instance.split)
                .all()
            )
            return [row[0] for row in rows]

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
        with self._session() as session:
            ds = self._get_dataset(session, org, dataset)
            if ds is None:
                return PageResult(items=[], total=0, offset=offset, limit=limit)

            q = session.query(Instance.name).filter(Instance.dataset_id == ds.id, Instance.split == split)
            if query:
                q = q.filter(Instance.name.ilike(f"%{query}%"))
            q = q.order_by(Instance.name)

            total = q.count()
            q = q.offset(offset)
            if limit is not None:
                q = q.limit(limit)

            items = [row[0] for row in q.all()]
            return PageResult(items=items, total=total, offset=offset, limit=limit)

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
        with self._session() as session:
            ds = self._get_dataset(session, org, dataset)
            if ds is None:
                return PageResult(items=[], total=0, offset=offset, limit=limit)

            q = session.query(Instance).filter(Instance.dataset_id == ds.id, Instance.split == split)
            if query:
                q = q.filter(Instance.name.ilike(f"%{query}%"))
            q = q.order_by(Instance.name)

            total = q.count()
            q = q.offset(offset)
            if limit is not None:
                q = q.limit(limit)

            items = [
                TaskEntry(
                    name=inst.name,
                    path=inst.name,
                    type=inst.type or "directory",
                    size=inst.size,
                    file_count=inst.file_count,
                    etag=inst.etag,
                    description=inst.description or "",
                    format=inst.format,
                    repo=inst.repo,
                    language=inst.language,
                    difficulty=inst.difficulty,
                    base_commit=inst.base_commit,
                    image_uris=inst.image_uris,
                    raw=inst.raw,
                    source_revision=inst.source_revision,
                    imported_from=inst.imported_from,
                    created_by=inst.created_by,
                    updated_at=inst.updated_at.isoformat() if inst.updated_at else None,
                )
                for inst in q.all()
            ]
            return PageResult(items=items, total=total, offset=offset, limit=limit)

    # ── Query ──

    def get_dataset(self, org: str, dataset: str) -> DatasetInfo | None:
        with self._session() as session:
            ds = self._get_dataset(session, org, dataset)
            if ds is None:
                return None
            tc = ds.task_counts or {}
            return DatasetInfo(
                id=ds.full_name,
                description=ds.description or "",
                tags=ds.tags or [],
                owner=ds.owner or "",
                homepage=ds.homepage,
                repo=ds.repo,
                paper=ds.paper,
                leaderboard=ds.leaderboard,
                logo_url=ds.logo_url,
                os=ds.os,
                version=ds.version,
                splits=list(tc.keys()),
                task_counts=tc,
            )

    def get_instance(self, org: str, dataset: str, split: str, instance_name: str) -> Instance | None:
        with self._session() as session:
            inst = self._resolve_instance(session, org, dataset, split, instance_name)
            if inst is not None:
                session.expunge(inst)
            return inst

    # ── Delete ──

    def delete_dataset(self, org: str, dataset: str) -> bool:
        with self._session() as session:
            ds = self._get_dataset(session, org, dataset)
            if ds is None:
                return False
            session.delete(ds)
            return True

    def delete_instance(self, org: str, dataset: str, split: str, instance_name: str) -> bool:
        with self._session() as session:
            ds = self._get_dataset(session, org, dataset)
            if ds is None:
                return False
            inst = self._get_instance(session, ds.id, split, instance_name)
            if inst is None:
                return False
            session.delete(inst)
            session.flush()
            self._decrement_task_count(session, ds.id, split)
            return True

    # ── Maintenance ──

    def recalculate_task_counts(self, org: str, dataset: str) -> dict[str, int]:
        with self._session() as session:
            ds = self._get_dataset(session, org, dataset)
            if ds is None:
                return {}
            rows = (
                session.query(Instance.split, func.count(Instance.id))
                .filter(Instance.dataset_id == ds.id)
                .group_by(Instance.split)
                .all()
            )
            counts = {split_name: cnt for split_name, cnt in rows}
            ds.task_counts = counts
            return counts

    # ── Image CRUD ──

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
        with self._session() as session:
            img = session.query(Image).filter(Image.source_image_uri == source_image_uri).first()
            if img is not None:
                img.image_uri_sg = image_uri_sg
                img.image_uri_sh = image_uri_sh
                img.image_hash = image_hash
                img.status = status
            else:
                img = Image(
                    source_image_uri=source_image_uri,
                    image_uri_sg=image_uri_sg,
                    image_uri_sh=image_uri_sh,
                    image_hash=image_hash,
                    status=status,
                    created_by=created_by,
                )
                session.add(img)
            session.flush()
            session.refresh(img)
            session.expunge(img)
            return img

    def get_image(self, source_image_uri: str) -> Image | None:
        with self._session() as session:
            img = session.query(Image).filter(Image.source_image_uri == source_image_uri).first()
            if img is not None:
                session.expunge(img)
            return img

    def list_images(
        self,
        *,
        status: str | None = None,
        offset: int = 0,
        limit: int | None = None,
    ) -> PageResult[ImageInfo]:
        with self._session() as session:
            q = session.query(Image)
            if status is not None:
                q = q.filter(Image.status == status)
            q = q.order_by(Image.created_at.desc())

            total = q.count()
            q = q.offset(offset)
            if limit is not None:
                q = q.limit(limit)

            items = [
                ImageInfo(
                    source_image_uri=img.source_image_uri,
                    image_uri_sg=img.image_uri_sg,
                    image_uri_sh=img.image_uri_sh,
                    image_hash=img.image_hash,
                    status=img.status,
                    last_error=img.last_error,
                    last_job_id=img.last_job_id,
                    created_by=img.created_by,
                    created_at=img.created_at.isoformat() if img.created_at else None,
                    updated_at=img.updated_at.isoformat() if img.updated_at else None,
                )
                for img in q.all()
            ]
            return PageResult(items=items, total=total, offset=offset, limit=limit)

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
        with self._session() as session:
            img = session.query(Image).filter(Image.source_image_uri == source_image_uri).first()
            if img is None:
                return None
            if status is not None:
                img.status = status
            if image_uri_sg is not None:
                img.image_uri_sg = image_uri_sg
            if image_uri_sh is not None:
                img.image_uri_sh = image_uri_sh
            if image_hash is not None:
                img.image_hash = image_hash
            if last_error is not None:
                img.last_error = last_error
            if last_job_id is not None:
                img.last_job_id = last_job_id
            session.flush()
            session.refresh(img)
            session.expunge(img)
            return img

    def delete_image(self, source_image_uri: str) -> bool:
        with self._session() as session:
            img = session.query(Image).filter(Image.source_image_uri == source_image_uri).first()
            if img is None:
                return False
            session.delete(img)
            return True

    # ── Permission CRUD ──

    def grant_permission(
        self,
        org: str,
        dataset: str,
        user_id: str,
        role: str = "viewer",
        *,
        granted_by: str | None = None,
    ) -> DatasetPermission:
        with self._session() as session:
            ds = self._get_dataset(session, org, dataset)
            if ds is None:
                raise ValueError(f"Dataset {org}/{dataset} not found")
            perm = (
                session.query(DatasetPermission)
                .filter(DatasetPermission.dataset_id == ds.id, DatasetPermission.user_id == user_id)
                .first()
            )
            if perm is not None:
                perm.role = role
                perm.granted_by = granted_by
            else:
                perm = DatasetPermission(
                    dataset_id=ds.id,
                    user_id=user_id,
                    role=role,
                    granted_by=granted_by,
                )
                session.add(perm)
            session.flush()
            session.refresh(perm)
            session.expunge(perm)
            return perm

    def revoke_permission(self, org: str, dataset: str, user_id: str) -> bool:
        with self._session() as session:
            ds = self._get_dataset(session, org, dataset)
            if ds is None:
                return False
            perm = (
                session.query(DatasetPermission)
                .filter(DatasetPermission.dataset_id == ds.id, DatasetPermission.user_id == user_id)
                .first()
            )
            if perm is None:
                return False
            session.delete(perm)
            return True

    def get_permission(self, org: str, dataset: str, user_id: str) -> PermissionInfo | None:
        with self._session() as session:
            ds = self._get_dataset(session, org, dataset)
            if ds is None:
                return None
            perm = (
                session.query(DatasetPermission)
                .filter(DatasetPermission.dataset_id == ds.id, DatasetPermission.user_id == user_id)
                .first()
            )
            if perm is None:
                return None
            return PermissionInfo(
                dataset_id=ds.full_name,
                user_id=perm.user_id,
                role=perm.role,
                granted_by=perm.granted_by,
                created_at=perm.created_at.isoformat() if perm.created_at else None,
                updated_at=perm.updated_at.isoformat() if perm.updated_at else None,
            )

    def list_dataset_permissions(
        self,
        org: str,
        dataset: str,
        *,
        offset: int = 0,
        limit: int | None = None,
    ) -> PageResult[PermissionInfo]:
        with self._session() as session:
            ds = self._get_dataset(session, org, dataset)
            if ds is None:
                return PageResult(items=[], total=0, offset=offset, limit=limit)
            q = session.query(DatasetPermission).filter(DatasetPermission.dataset_id == ds.id)
            q = q.order_by(DatasetPermission.user_id)
            total = q.count()
            q = q.offset(offset)
            if limit is not None:
                q = q.limit(limit)
            items = [
                PermissionInfo(
                    dataset_id=ds.full_name,
                    user_id=p.user_id,
                    role=p.role,
                    granted_by=p.granted_by,
                    created_at=p.created_at.isoformat() if p.created_at else None,
                    updated_at=p.updated_at.isoformat() if p.updated_at else None,
                )
                for p in q.all()
            ]
            return PageResult(items=items, total=total, offset=offset, limit=limit)

    def list_user_permissions(
        self,
        user_id: str,
        *,
        offset: int = 0,
        limit: int | None = None,
    ) -> PageResult[PermissionInfo]:
        with self._session() as session:
            q = session.query(DatasetPermission).filter(DatasetPermission.user_id == user_id)
            q = q.order_by(DatasetPermission.dataset_id)
            total = q.count()
            q = q.offset(offset)
            if limit is not None:
                q = q.limit(limit)
            items = []
            for p in q.all():
                ds = session.query(Dataset).filter(Dataset.id == p.dataset_id).first()
                items.append(
                    PermissionInfo(
                        dataset_id=ds.full_name if ds else str(p.dataset_id),
                        user_id=p.user_id,
                        role=p.role,
                        granted_by=p.granted_by,
                        created_at=p.created_at.isoformat() if p.created_at else None,
                        updated_at=p.updated_at.isoformat() if p.updated_at else None,
                    )
                )
            return PageResult(items=items, total=total, offset=offset, limit=limit)

    # ── Audit ──

    def log_event(
        self,
        target_type: str,
        target_id: str,
        event_type: str,
        operator: str,
        changes: dict | None = None,
    ) -> AuditEvent:
        with self._session() as session:
            event = AuditEvent(
                target_type=target_type,
                target_id=target_id,
                event_type=event_type,
                operator=operator,
                changes=changes,
            )
            session.add(event)
            session.flush()
            session.refresh(event)
            session.expunge(event)
            return event

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
        with self._session() as session:
            q = session.query(AuditEvent)
            if target_type is not None:
                q = q.filter(AuditEvent.target_type == target_type)
            if target_id is not None:
                q = q.filter(AuditEvent.target_id == target_id)
            if event_type is not None:
                q = q.filter(AuditEvent.event_type == event_type)
            if operator is not None:
                q = q.filter(AuditEvent.operator == operator)
            q = q.order_by(AuditEvent.created_at.desc())

            total = q.count()
            q = q.offset(offset)
            if limit is not None:
                q = q.limit(limit)

            items = [
                AuditEventInfo(
                    id=e.id,
                    target_type=e.target_type,
                    target_id=e.target_id,
                    event_type=e.event_type,
                    operator=e.operator,
                    changes=e.changes,
                    created_at=e.created_at.isoformat() if e.created_at else None,
                )
                for e in q.all()
            ]
            return PageResult(items=items, total=total, offset=offset, limit=limit)
