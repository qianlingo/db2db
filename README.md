# Db2Db

[English](README_EN.md) | [中文](README_CN.md)

[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE) [![Python](https://img.shields.io/badge/python-%3E%3D3.8-brightgreen.svg)](https://www.python.org/)

Lightweight table sync and migration tool built on SQLAlchemy. Db2Db helps reliably sync schema and data between different relational databases.

## Supported databases

- MySQL / MariaDB (driver: pymysql)
- PostgreSQL (driver: psycopg2)
- SQLite (via SQLAlchemy)

## Key features

- Cross-database table data sync (MySQL ⇄ PostgreSQL ⇄ SQLite)
- Automatic table creation and type mapping (`--create-table`, `--drop-table`)
- Batch insert with configurable `--batch-size`
- Export/import to folder (schema.sql + data.csv)
- Incremental/filtered sync (`--where`, `--columns`, `--column-mapping`)
- Single-file SQL export for offline migration

## Installation

```bash
python -m venv venv
# Windows PowerShell
venv\Scripts\Activate.ps1
# or Linux/macOS
# source venv/bin/activate
pip install -r requirements.txt
```

## Quick start

Sync a single table (MySQL → PostgreSQL):

```bash
python sync_data.py \
  --source-url "mysql+pymysql://user:pass@host:3306/source_db" \
  --target-url "postgresql+psycopg2://user:pass@host:5432/target_db" \
  --source-table "users" \
  --target-table "users" \
  --create-table
```

Export database to a folder:

```bash
python sync_data.py --source-url "sqlite:///my.db" --export-to-folder ./backup --all-tables
```

See full help:

```bash
python sync_data.py --help
```

## Command-line parameters (overview)

- `--source-url` : Source database URL (required for sync/export)
- `--target-url` : Target database URL (required for sync/import)
- `--source-table`, `--target-table` : Single-table sync
- `--all-tables` : Operate on all tables
- `--exclude-tables` / `--skip2tables` : Exclude or skip specified tables (comma-separated)
- `--where` : SQL WHERE clause to filter rows
- `--columns` : Comma-separated list of columns to transfer
- `--column-mapping` : `src_col=tgt_col,...` mapping for mismatched column names
- `--create-table` / `--drop-table` : Auto create or recreate the target table
- `--batch-size` : Rows per insert batch (default 1000)
- `--truncate-target` : Truncate target table before importing
- `--dry-run` : Preview operations without executing

## Modes

1. Sync mode (DB → DB) — requires both `--source-url` and `--target-url`.
2. Export mode (DB → folder) — `--source-url` + `--export-to-folder`.
3. Import mode (folder → DB) — `--target-url` + `--import-from-folder`.

## File formats for export

- `schema.sql` — CREATE TABLE statements
- `data.csv` — CSV data (UTF-8 with BOM for Excel compatibility)

## Connection URL examples

```
# MySQL / MariaDB
mysql+pymysql://user:pass@localhost:3306/mydb

# PostgreSQL
postgresql+psycopg2://user:pass@localhost:5432/mydb

# SQLite
sqlite:///path/to/db.sqlite
```

## Type mapping notes

When using `--create-table`, Db2Db will attempt to map common types between databases. Examples:

| MySQL | PostgreSQL | SQLite |
|---|---|---|
| `AUTO_INCREMENT` | `SERIAL` | `AUTOINCREMENT` |
| `DATETIME` | `TIMESTAMP` | `TEXT` |
| `TEXT`/`BLOB` | `TEXT`/`BYTEA` | `TEXT`/`BLOB` |

## Use cases and examples

1) Sync existing table to another database:

```bash
python sync_data.py \
  --source-url "mysql+pymysql://root:123@localhost:3306/mydb" \
  --target-url "postgresql+psycopg2://postgres:123@localhost:5432/newdb" \
  --source-table "orders" \
  --target-table "orders"
```

2) Auto-create target table and sync:

```bash
python sync_data.py \
  --source-url "mysql+pymysql://root:123@localhost:3306/mydb" \
  --target-url "postgresql+psycopg2://postgres:123@localhost:5432/newdb" \
  --source-table "orders" \
  --target-table "orders" \
  --create-table --drop-table
```

3) Export whole DB to folder:

```bash
python sync_data.py --source-url "mysql+pymysql://root:123@localhost:3306/mydb" --export-to-folder ./backup --all-tables
```

4) Export as single SQL file:

```bash
python sync_data.py --source-url "mysql+pymysql://root:123@localhost:3306/mydb" --import2signle
```

## Notes and best practices

- Use `--dry-run` to preview changes before execution.
- For large tables, use `--where` to sync in batches to avoid memory issues.
- Be careful with `--truncate-target` and `--drop-table` as they will remove data.

## License

MIT

