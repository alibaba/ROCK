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


def _extract_create_table_blocks(sql: str) -> list[tuple[str, str]]:
    """Extract (table_name, body) pairs from all CREATE TABLE statements in SQL text.

    Handles nested parentheses (e.g. column type definitions containing parens).
    """
    results: list[tuple[str, str]] = []
    for header in re.finditer(r"CREATE TABLE (\w+)\s*\(", sql, re.IGNORECASE):
        table_name = header.group(1)
        start = header.end()
        depth = 1
        i = start
        while i < len(sql) and depth > 0:
            if sql[i] == "(":
                depth += 1
            elif sql[i] == ")":
                depth -= 1
            i += 1
        results.append((table_name.lower(), sql[start : i - 1]))
    return results


def _parse_columns_from_body(body: str) -> dict[str, str]:
    """Parse column definitions from a CREATE TABLE body. Returns {column_name: full_definition}."""
    columns: dict[str, str] = {}
    for line in body.split("\n"):
        line = line.strip().rstrip(",")
        if not line or line.startswith("PRIMARY KEY") or line.startswith("CONSTRAINT") or line.startswith(")"):
            continue
        parts = line.split()
        if parts:
            columns[parts[0].lower()] = line
    return columns


def _parse_columns_from_sql(sql: str, table_name: str | None = None) -> dict[str, str]:
    """Parse column definitions from a CREATE TABLE statement. Returns {column_name: full_definition}."""
    blocks = _extract_create_table_blocks(sql)
    if not blocks:
        return {}
    if table_name:
        for name, body in blocks:
            if name == table_name.lower():
                return _parse_columns_from_body(body)
        return {}
    return _parse_columns_from_body(blocks[0][1])


def _parse_all_tables_from_sql(sql: str) -> dict[str, dict[str, str]]:
    """Parse all CREATE TABLE statements. Returns {table_name: {column_name: full_definition}}."""
    return {name: _parse_columns_from_body(body) for name, body in _extract_create_table_blocks(sql)}


def _parse_indexes_from_sql(sql: str) -> dict[str, str]:
    """Parse CREATE INDEX statements. Returns {index_name: full_statement}."""
    indexes: dict[str, str] = {}
    for match in re.finditer(r"(CREATE\s+(?:UNIQUE\s+)?INDEX\s+(\w+)\s+[^;]+);", sql, re.IGNORECASE):
        indexes[match.group(2).lower()] = match.group(1).strip() + ";"
    return indexes


def _read_old_sql(source: str) -> str:
    """Read old DDL from a file path or git ref (commit/tag).

    If source looks like a file path (contains '/' or '.sql'), read it directly.
    Otherwise treat it as a git ref and concatenate all sql/*.sql files from that ref.
    """
    import os

    if os.path.isfile(source):
        with open(source) as f:
            return f.read()
    import subprocess

    # List all .sql files under sql/ in the given git ref
    try:
        tree_output = subprocess.check_output(
            ["git", "ls-tree", "--name-only", f"{source}:sql"], stderr=subprocess.DEVNULL, text=True
        )
    except subprocess.CalledProcessError:
        print(f"Cannot find sql/ directory in git ref '{source}'", file=sys.stderr)
        sys.exit(1)

    sql_files = [f for f in tree_output.strip().split("\n") if f.endswith(".sql")]
    if not sql_files:
        print(f"No .sql files found in git ref '{source}:sql/'", file=sys.stderr)
        sys.exit(1)

    parts: list[str] = []
    for sql_file in sorted(sql_files):
        try:
            content = subprocess.check_output(
                ["git", "show", f"{source}:sql/{sql_file}"], stderr=subprocess.DEVNULL, text=True
            )
            parts.append(content)
        except subprocess.CalledProcessError:
            continue

    if not parts:
        print(f"Cannot read any SQL files from git ref '{source}'", file=sys.stderr)
        sys.exit(1)
    return "\n\n".join(parts)


def gen_alter(dialect, source: str, table_filter: str | None = None) -> str:
    """Compare old DDL (file or git ref) against current ORM and generate ALTER TABLE statements."""
    from rock.admin.core.schema import Base

    old_sql = _read_old_sql(source)

    old_tables = _parse_all_tables_from_sql(old_sql)
    old_indexes = _parse_indexes_from_sql(old_sql)

    tables = Base.metadata.sorted_tables
    if table_filter:
        names = {n.strip() for n in table_filter.split(",")}
        tables = [t for t in tables if t.name in names]
    else:
        # Compare all tables that exist in old SQL
        tables = [t for t in tables if t.name.lower() in old_tables]

    if not tables:
        print("No matching table found", file=sys.stderr)
        sys.exit(1)

    lines: list[str] = []
    for table in tables:
        old_columns = old_tables.get(table.name.lower(), {})
        new_ddl = str(CreateTable(table).compile(dialect=dialect)).strip()
        new_columns = _parse_columns_from_sql(new_ddl, table.name)

        for col_name, col_def in new_columns.items():
            if col_name not in old_columns:
                lines.append(f"ALTER TABLE {table.name} ADD COLUMN {col_def};")

        new_index_names = {(idx.name or "").lower() for idx in table.indexes if idx.name}
        for idx in sorted(table.indexes, key=lambda i: i.name or ""):
            idx_name = (idx.name or "").lower()
            if idx_name and idx_name not in old_indexes:
                lines.append(str(CreateIndex(idx).compile(dialect=dialect)).strip() + ";")

        table_prefix = f"ix_{table.name}_"
        for old_idx_name in sorted(old_indexes):
            if old_idx_name.startswith(table_prefix) and old_idx_name not in new_index_names:
                lines.append(f"DROP INDEX {old_idx_name};")

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
