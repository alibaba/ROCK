"""Generate DDL SQL from SQLAlchemy ORM schema definitions.

Usage:
    uv run python scripts/gen_ddl.py                  # default: postgresql
    uv run python scripts/gen_ddl.py --dialect sqlite
    uv run python scripts/gen_ddl.py --table sandbox_record --out sql/sandbox_record.sql
    uv run python scripts/gen_ddl.py --alter-from sql/sandbox_record.sql   # diff against old DDL file
    uv run python scripts/gen_ddl.py --alter-from HEAD~1                   # diff against git commit/tag
    uv run python scripts/gen_ddl.py --alter-from v1.3.0                   # diff against git tag
"""

import argparse
import re
import sys

from sqlalchemy.schema import CreateIndex, CreateTable


def get_dialect(name: str):
    if name == "postgresql":
        from sqlalchemy.dialects import postgresql

        return postgresql.dialect()
    if name == "sqlite":
        from sqlalchemy.dialects import sqlite

        return sqlite.dialect()
    print(f"Unsupported dialect: {name}", file=sys.stderr)
    sys.exit(1)


def gen_ddl(dialect, table_filter: str | None = None) -> str:
    # Import here so all models are registered onto Base.metadata
    from rock.admin.core.schema import Base  # noqa: F401 (side-effect: registers SandboxRecord)

    tables = Base.metadata.sorted_tables
    if table_filter:
        names = {n.strip() for n in table_filter.split(",")}
        tables = [t for t in tables if t.name in names]
        if not tables:
            print(f"No tables matched: {table_filter}", file=sys.stderr)
            sys.exit(1)

    lines: list[str] = []
    for table in tables:
        lines.append(str(CreateTable(table).compile(dialect=dialect)).strip() + ";")
        # table.indexes has set-like semantics; sort for deterministic output.
        for index in sorted(table.indexes, key=lambda idx: idx.name or ""):
            lines.append(str(CreateIndex(index).compile(dialect=dialect)).strip() + ";")

    return "\n\n".join(lines)


def _parse_columns_from_sql(sql: str) -> dict[str, str]:
    """Parse column definitions from a CREATE TABLE statement. Returns {column_name: full_definition}."""
    match = re.search(r"CREATE TABLE (\w+)\s*\((.*)\)", sql, re.DOTALL)
    if not match:
        return {}
    body = match.group(2)
    columns: dict[str, str] = {}
    for line in body.split("\n"):
        line = line.strip().rstrip(",")
        if not line or line.startswith("PRIMARY KEY") or line.startswith("CONSTRAINT") or line.startswith(")"):
            continue
        parts = line.split()
        if parts:
            columns[parts[0].lower()] = line
    return columns


def _parse_indexes_from_sql(sql: str) -> dict[str, str]:
    """Parse CREATE INDEX statements. Returns {index_name: full_statement}."""
    indexes: dict[str, str] = {}
    for match in re.finditer(r"(CREATE\s+(?:UNIQUE\s+)?INDEX\s+(\w+)\s+[^;]+);", sql, re.IGNORECASE):
        indexes[match.group(2).lower()] = match.group(1).strip() + ";"
    return indexes


def _read_old_sql(source: str) -> str:
    """Read old DDL from a file path or git ref (commit/tag).

    If source looks like a file path (contains '/' or '.sql'), read it directly.
    Otherwise treat it as a git ref and read sql/sandbox_record.sql from that ref.
    """
    if "/" in source or source.endswith(".sql"):
        with open(source) as f:
            return f.read()
    import subprocess

    # Try each known SQL path under the git ref
    for sql_path in ["sql/sandbox_record.sql"]:
        try:
            return subprocess.check_output(
                ["git", "show", f"{source}:{sql_path}"], stderr=subprocess.DEVNULL, text=True
            )
        except subprocess.CalledProcessError:
            continue
    print(f"Cannot find DDL file in git ref '{source}'", file=sys.stderr)
    sys.exit(1)


def gen_alter(dialect, source: str, table_filter: str | None = None) -> str:
    """Compare old DDL (file or git ref) against current ORM and generate ALTER TABLE statements."""
    from rock.admin.core.schema import Base

    old_sql = _read_old_sql(source)

    old_columns = _parse_columns_from_sql(old_sql)
    old_indexes = _parse_indexes_from_sql(old_sql)

    tables = Base.metadata.sorted_tables
    if table_filter:
        names = {n.strip() for n in table_filter.split(",")}
        tables = [t for t in tables if t.name in names]
    else:
        table_match = re.search(r"CREATE TABLE (\w+)", old_sql)
        if table_match:
            target = table_match.group(1).lower()
            tables = [t for t in tables if t.name == target]

    if not tables:
        print("No matching table found", file=sys.stderr)
        sys.exit(1)

    lines: list[str] = []
    for table in tables:
        new_ddl = str(CreateTable(table).compile(dialect=dialect)).strip()
        new_columns = _parse_columns_from_sql(new_ddl)

        for col_name, col_def in new_columns.items():
            if col_name not in old_columns:
                lines.append(f"ALTER TABLE {table.name} ADD COLUMN {col_def};")

        for idx in sorted(table.indexes, key=lambda i: i.name or ""):
            idx_name = (idx.name or "").lower()
            if idx_name and idx_name not in old_indexes:
                lines.append(str(CreateIndex(idx).compile(dialect=dialect)).strip() + ";")

    return "\n\n".join(lines) if lines else "-- No changes detected"


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate DDL from ORM schema")
    parser.add_argument("--dialect", default="postgresql", choices=["postgresql", "sqlite"])
    parser.add_argument("--table", default=None, help="Comma-separated table names to generate (default: all)")
    parser.add_argument(
        "--alter-from",
        default=None,
        dest="alter_from",
        help="Old DDL file path or git ref (commit/tag) to diff against",
    )
    parser.add_argument("--out", default=None, help="Output file path (default: stdout)")
    args = parser.parse_args()

    dialect = get_dialect(args.dialect)

    if args.alter_from:
        result = gen_alter(dialect, args.alter_from, table_filter=args.table)
    else:
        result = gen_ddl(dialect, table_filter=args.table)

    if args.out:
        with open(args.out, "w") as f:
            f.write(result + "\n")
        print(f"Written to {args.out}")
    else:
        print(result)


if __name__ == "__main__":
    main()
