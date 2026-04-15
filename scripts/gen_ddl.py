"""Generate DDL SQL from SQLAlchemy ORM schema definitions.

Usage:
    uv run python scripts/gen_ddl.py                  # default: postgresql
    uv run python scripts/gen_ddl.py --dialect sqlite
    uv run python scripts/gen_ddl.py --dialect postgresql --out ddl.sql
"""

import argparse
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


def gen_ddl(dialect) -> str:
    # Import here so all models are registered onto Base.metadata
    from rock.admin.core.schema import Base  # noqa: F401 (side-effect: registers SandboxRecord)

    lines: list[str] = []
    for table in Base.metadata.sorted_tables:
        lines.append(str(CreateTable(table).compile(dialect=dialect)).strip() + ";")
        # table.indexes has set-like semantics; sort for deterministic output.
        for index in sorted(table.indexes, key=lambda idx: idx.name or ""):
            lines.append(str(CreateIndex(index).compile(dialect=dialect)).strip() + ";")

    return "\n\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate DDL from ORM schema")
    parser.add_argument("--dialect", default="postgresql", choices=["postgresql", "sqlite"])
    parser.add_argument("--out", default=None, help="Output file path (default: stdout)")
    args = parser.parse_args()

    dialect = get_dialect(args.dialect)
    ddl = gen_ddl(dialect)

    if args.out:
        with open(args.out, "w") as f:
            f.write(ddl + "\n")
        print(f"Written to {args.out}")
    else:
        print(ddl)


if __name__ == "__main__":
    main()
