"""Snapshot catalog DDL and foreign-key contracts."""

from pathlib import Path

from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

from rock.admin.core.schema import RockSnapshotRecord, SandboxRecord, TemplateRecord

REPO_ROOT = Path(__file__).parents[4]


def _normalise(sql: str) -> str:
    return " ".join(sql.split()).lower()


def test_postgresql_orm_ddl_contains_canonical_constraints_and_indexes():
    dialect = postgresql.dialect()
    table_ddl = _normalise(str(CreateTable(RockSnapshotRecord.__table__).compile(dialect=dialect)))
    index_ddl = "\n".join(
        _normalise(str(CreateIndex(index).compile(dialect=dialect))) for index in RockSnapshotRecord.__table__.indexes
    )

    assert "check (kind in ('base_image', 'full'))" in table_ddl
    assert "storage_namespace" not in table_ddl
    assert "creating" in table_ddl and "local_ready" in table_ddl and "remote_ready" in table_ddl
    assert "deleted" in table_ddl and "failed" in table_ddl
    assert "state <> 'remote_ready' or (spec is not null and remote_ready_at is not null)" in table_ddl
    assert "state <> 'deleted' or deleted_at is not null" in table_ddl
    assert "constraint rock_snapshot_base_image_identity_ck" in table_ddl
    assert "source_epoch_generation" not in index_ddl
    assert "source_sandbox_generation" not in index_ddl
    assert "runtime_epoch" not in table_ddl
    assert "generation" not in table_ddl
    assert "operation_identity" not in table_ddl
    assert "create index rock_snapshot_state_created_ix" in index_ddl
    assert "create unique index rock_snapshot_base_image_build_key_uq" in index_ddl
    assert "state <> 'deleted'" in index_ddl
    assert "namespace_state" not in index_ddl


def test_checked_in_catalog_ddl_matches_the_orm_model():
    dialect = postgresql.dialect()
    generated_parts = []
    for table in (RockSnapshotRecord.__table__,):
        generated_parts.append(str(CreateTable(table).compile(dialect=dialect)).strip() + ";")
        generated_parts.extend(
            str(CreateIndex(index).compile(dialect=dialect)).strip() + ";"
            for index in sorted(table.indexes, key=lambda item: item.name or "")
        )
    checked_in = (REPO_ROOT / "sql/rock_snapshot.sql").read_text()

    assert _normalise(checked_in) == _normalise("\n\n".join(generated_parts))


def test_sandbox_source_snapshot_foreign_key_is_restrictive():
    column = SandboxRecord.__table__.columns.source_snapshot_id
    foreign_key = next(iter(column.foreign_keys))

    assert foreign_key.target_fullname == "rock_snapshot.snapshot_id"
    assert foreign_key.ondelete == "RESTRICT"


def test_legacy_template_has_no_snapshot_binding():
    assert TemplateRecord.__table__.columns.template_id.primary_key
    assert "snapshot_id" not in TemplateRecord.__table__.columns
    assert not TemplateRecord.__table__.foreign_keys
    assert all(index.name != "template_snapshot_ix" for index in TemplateRecord.__table__.indexes)
