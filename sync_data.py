"""Sync data from database A table A to database B table B."""

import argparse
import ast
import csv
import json
import os
import re
import sys
import urllib.parse
from contextlib import contextmanager
from pathlib import Path

csv.field_size_limit(100*1024*1024)

from sqlalchemy import create_engine, MetaData, Table, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.schema import CreateTable, CreateIndex, DropTable


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync data from db_A.table_A to db_B.table_B")
    parser.add_argument("--source-url", required=False,
                        help="Source DB URL, e.g. mysql+pymysql://user:pass@host:3306/db_A")
    parser.add_argument("--target-url", required=False,
                        help="Target DB URL, e.g. postgresql+psycopg2://user:pass@host:5432/db_B")
    parser.add_argument("--source-table", default=None, help="Source table name")
    parser.add_argument("--target-table", default=None, help="Target table name")
    parser.add_argument("--all-tables", action="store_true",
                        help="Sync all tables from source database (source-table/target-table will be ignored)")
    parser.add_argument("--exclude-tables", default=None,
                        help="Comma-separated table names to exclude when using --all-tables")
    parser.add_argument("--source-schema", default=None, help="Source schema name (optional)")
    parser.add_argument("--target-schema", default=None, help="Target schema name (optional)")
    parser.add_argument("--batch-size", type=int, default=1000, help="Rows per batch insert")
    parser.add_argument("--truncate-target", action="store_true", help="Truncate target table before insert")
    parser.add_argument("--where", default=None, help="SQL WHERE clause for source query, e.g. 'id > 100'")
    parser.add_argument("--columns", default=None,
                        help="Comma-separated column names to sync (default: all common columns)")
    parser.add_argument("--column-mapping", default=None,
                        help="Column mapping: 'src_col1=tgt_col1,src_col2=tgt_col2'")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be done without executing")
    parser.add_argument("--create-table", action="store_true",
                        help="Create target table from source structure before syncing")
    parser.add_argument("--drop-table", action="store_true",
                        help="Drop target table before creating (only with --create-table)")
    parser.add_argument("--no-indexes", action="store_true",
                        help="Skip indexes when creating table (only with --create-table)")
    parser.add_argument("--no-fk", action="store_true",
                        help="Skip foreign keys when creating table (only with --create-table)")
    parser.add_argument("--export-to-folder", default=None,
                        help="Export database/table to folder (schema as SQL, data as CSV)")
    parser.add_argument("--import-from-folder", default=None,
                        help="Import database/table from folder")
    parser.add_argument("--import2signle", action="store_true",
                        help="Export all tables to a single import SQL file under data/import")
    parser.add_argument("--skip2tables", default=None,
                        help="Comma-separated table names to skip (used with --all-tables or --import2signle)")
    args = parser.parse_args()

    # Validate arguments - determine mode
    is_export = args.export_to_folder is not None or args.import2signle
    is_import = args.import_from_folder is not None
    is_sync = not is_export and not is_import

    if is_export and is_import:
        parser.error("Cannot use --export-to-folder and --import-from-folder together")

    if is_export:
        if not args.source_url:
            parser.error("--source-url is required for export mode")
        if args.target_url:
            parser.error("--target-url is not used in export mode")
        if args.import2signle:
            if args.export_to_folder:
                parser.error("--import2signle cannot be used with --export-to-folder")
            if args.source_table or args.target_table:
                parser.error("--source-table and --target-table are not used with --import2signle")
        elif not args.all_tables and not args.source_table:
            parser.error("--source-table is required for export mode (or use --all-tables)")
    elif is_import:
        if not args.target_url:
            parser.error("--target-url is required for import mode")
        if args.source_url:
            parser.error("--source-url is not used in import mode")
        if not args.all_tables and not args.target_table:
            parser.error("--target-table is required for import mode (or use --all-tables)")
    else:  # sync mode
        if not args.source_url or not args.target_url:
            parser.error("Both --source-url and --target-url are required for sync mode")
        if args.all_tables:
            if args.source_table or args.target_table:
                parser.error("--source-table and --target-table are ignored when --all-tables is specified")
        else:
            if not args.source_table or not args.target_table:
                parser.error("--source-table and --target-table are required (or use --all-tables)")

    return args


def _safe_url(url: str) -> str:
    """URL-encode the password portion so special chars like #, @ don't break parsing."""
    # Split scheme:// + rest
    m = re.match(r'^([^:]+://)(.+)$', url)
    if not m:
        return url
    scheme, rest = m.groups()
    if '@' not in rest:
        return url
    # Split at the LAST @ to separate userinfo from host
    last_at = rest.rindex('@')
    userinfo = rest[:last_at]
    hostpart = rest[last_at:]
    if ':' not in userinfo:
        return url
    user, password = userinfo.split(':', 1)
    encoded = urllib.parse.quote(password, safe="")
    if encoded == password:
        return url
    return f"{scheme}{user}:{encoded}{hostpart}"


def create_engines(source_url: str, target_url: str) -> tuple[Engine, Engine]:
    source_url = _safe_url(source_url)
    target_url = _safe_url(target_url)
    try:
        src = create_engine(source_url, connect_args={"connect_timeout": 10} if "mysql" in source_url else {})
    except Exception as e:
        _handle_engine_error(e, source_url, "source")
    try:
        tgt = create_engine(target_url, connect_args={"connect_timeout": 10} if "mysql" in target_url else {})
    except Exception as e:
        _handle_engine_error(e, target_url, "target")
    return src, tgt


def _handle_connection_error(e: Exception, source_url: str, target_url: str):
    err = str(e)
    hint = f"\n[Error] 数据库连接失败: {e}\n\n  常见原因：\n"
    if "Connection refused" in err or "could not connect" in err.lower():
        hint += "    - 数据库服务未启动\n    - 主机地址或端口错误\n    - 防火墙阻止连接\n"
    elif "authentication failed" in err.lower() or "password" in err.lower() or "Access denied" in err:
        hint += "    - 用户名或密码错误\n    - 用户没有访问权限\n"
    elif "does not exist" in err.lower() or "Unknown database" in err:
        hint += "    - 数据库名称不存在\n"
    else:
        hint += "    - 请检查连接参数是否正确\n"
    hint += f"\n  源 URL: {source_url}\n  目标 URL: {target_url}\n"
    print(hint)
    sys.exit(1)


def _handle_engine_error(e: Exception, url: str, label: str):
    err = str(e)
    if "NoSuchModuleError" in type(e).__name__ or "Can't load plugin" in err:
        # Extract dialect from URL
        dialect = url.split("+")[0].split("://")[0] if "://" in url else url
        hint = (
            f"\n[Error] 无法加载 {label} 数据库驱动: {e}\n\n"
            f"  常见原因：连接 URL 的 dialect 写错了\n\n"
            f"  正确格式：\n"
            f"    MySQL:      mysql+pymysql://user:pass@host:3306/dbname\n"
            f"    PostgreSQL: postgresql+psycopg2://user:pass@host:5432/dbname\n"
            f"    SQLite:     sqlite:///path/to/db.sqlite\n\n"
            f"  你当前的 URL: {url}\n"
        )
        print(hint)
        sys.exit(1)
    raise e


@contextmanager
def get_connection(engine: Engine):
    conn = engine.connect()
    try:
        yield conn
    finally:
        conn.close()


def table_exists(engine: Engine, schema: str | None, table_name: str) -> bool:
    """Check if a table exists in the target database."""
    dialect = engine.dialect.name
    if dialect == "sqlite":
        sql = "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = :name"
        params = {"name": table_name}
    elif schema:
        sql = "SELECT 1 FROM information_schema.tables WHERE table_schema = :schema AND table_name = :name"
        params = {"schema": schema, "name": table_name}
    else:
        sql = "SELECT 1 FROM information_schema.tables WHERE table_name = :name"
        params = {"name": table_name}
    with get_connection(engine) as conn:
        return conn.execute(text(sql), params).scalar() == 1


def list_all_tables(engine: Engine, schema: str | None) -> list[str]:
    """Get all table names from the database."""
    metadata = MetaData()
    try:
        metadata.reflect(bind=engine, schema=schema)
    except Exception as e:
        print(f"\n[Error] 无法获取表列表: {e}")
        sys.exit(1)
    # Filter tables by schema if specified
    tables = []
    for key in metadata.tables:
        if schema:
            # key format: "schema.table_name"
            if key.startswith(f"{schema}."):
                tables.append(key[len(schema) + 1:])
        else:
            # No schema specified, include all tables
            # For databases with default schema, key might be "schema.table" or just "table"
            if "." in key:
                # Skip tables from other schemas
                continue
            tables.append(key)
    return sorted(tables)


def reflect_table(engine: Engine, schema: str | None, table_name: str) -> Table:
    metadata = MetaData()
    try:
        metadata.reflect(bind=engine, schema=schema, only=[table_name])
    except Exception as e:
        _handle_reflect_error(e, engine, schema, table_name)
    key = f"{schema}.{table_name}" if schema else table_name
    if key not in metadata.tables:
        _handle_table_not_found(engine, schema, table_name)
    return metadata.tables[key]


def _handle_reflect_error(e: Exception, engine: Engine, schema: str | None, table_name: str):
    err = str(e)
    dialect = engine.dialect.name
    url = str(engine.url).replace(engine.url.password or "", "***") if engine.url.password else str(engine.url)
    hint = (
        f"\n[Error] 无法反射表 '{table_name}': {e}\n\n"
        f"  可能原因：\n"
        f"    1. 表不存在\n"
        f"    2. 缺少 schema 参数（PostgreSQL 常见，尝试加 --source-schema public 或 --target-schema public）\n"
        f"    3. 数据库连接问题\n\n"
        f"  数据库: {url}\n"
        f"  表名:   {table_name}\n"
        f"  Schema: {schema or '(未指定)'}\n"
    )
    print(hint)
    sys.exit(1)


def _handle_table_not_found(engine: Engine, schema: str | None, table_name: str):
    dialect = engine.dialect.name
    url = str(engine.url).replace(engine.url.password or "", "***") if engine.url.password else str(engine.url)
    # Try to list available tables
    metadata = MetaData()
    try:
        metadata.reflect(bind=engine, schema=schema)
        available = sorted(metadata.tables.keys())
    except Exception:
        available = []
    hint = (
        f"\n[Error] 表 '{table_name}' 不存在\n\n"
        f"  数据库: {url}\n"
        f"  Schema: {schema or '(未指定)'}\n"
    )
    if available:
        hint += f"  可用的表: {available[:20]}\n"
        if len(available) > 20:
            hint += f"  ... 共 {len(available)} 张表\n"
    else:
        hint += "  (无法获取可用表列表)\n"
    hint += (
        f"\n  建议：\n"
        f"    - 检查表名是否正确\n"
        f"    - 如果是 PostgreSQL，尝试加 --source-schema public 或 --target-schema public\n"
        f"    - 如果目标表不存在，加 --create-table 自动创建\n"
    )
    print(hint)
    sys.exit(1)


def resolve_columns(source_table: Table, target_table: Table,
                    columns_arg: str | None, column_mapping_arg: str | None) -> tuple[list[str], dict[str, str]]:
    src_cols = {c.name for c in source_table.columns}
    tgt_cols = {c.name for c in target_table.columns}

    # Parse column mapping: "src_col=tgt_col,..."
    mapping: dict[str, str] = {}
    if column_mapping_arg:
        for pair in column_mapping_arg.split(","):
            s, t = pair.strip().split("=")
            mapping[s.strip()] = t.strip()

    if columns_arg:
        col_list = [c.strip() for c in columns_arg.split(",")]
    else:
        col_list = sorted(src_cols & tgt_cols)
        if not col_list:
            print("Warning: No common columns found between source and target tables.")
            print(f"  Source columns: {sorted(src_cols)}")
            print(f"  Target columns: {sorted(tgt_cols)}")
            sys.exit(1)

    # Apply mapping: column names in source → target
    final_mapping: dict[str, str] = {}
    for c in col_list:
        final_mapping[c] = mapping.get(c, c)

    return col_list, final_mapping


def row_count(engine: Engine, schema: str | None, table_name: str, where_clause: str | None) -> int:
    schema_prefix = f"{schema}." if schema else ""
    qualified = f"{schema_prefix}{table_name}"
    query = f"SELECT COUNT(*) FROM {qualified}"
    if where_clause:
        query += f" WHERE {where_clause}"
    with get_connection(engine) as conn:
        return conn.execute(text(query)).scalar()


def _col_type_name(col) -> str:
    """Return a compact string representation of a column type."""
    t = col.type
    if hasattr(t, "length") and t.length:
        if hasattr(t, "precision") and t.precision:
            return f"{t.__class__.__name__}({t.precision},{t.scale})"
        return f"{t.__class__.__name__}({t.length})"
    if hasattr(t, "precision") and t.precision:
        return f"{t.__class__.__name__}({t.precision},{t.scale})"
    return str(t)


def _map_column_names(src_cols: list, column_mapping: str | None) -> dict[str, str]:
    """Build a src_col -> tgt_col name mapping, applying --column-mapping if given."""
    mapping: dict[str, str] = {}
    if column_mapping:
        for pair in column_mapping.split(","):
            s, t = pair.strip().split("=")
            mapping[s.strip()] = t.strip()
    result: dict[str, str] = {}
    for c in src_cols:
        result[c] = mapping.get(c, c)
    return result


def create_target_table(src_engine: Engine, tgt_engine: Engine,
                        source_table: Table, target_table_name: str,
                        target_schema: str | None,
                        column_mapping: str | None,
                        columns_arg: str | None,
                        drop_first: bool,
                        no_indexes: bool, no_fk: bool,
                        dry_run: bool) -> Table:
    """Reflect source table structure, generate target-compatible DDL, and execute it.

    Returns a reflected handle to the newly created target table.
    """
    metadata = MetaData()

    # Determine which source columns to include
    if columns_arg:
        col_names = [c.strip() for c in columns_arg.split(",")]
    else:
        col_names = [c.name for c in source_table.columns]

    name_map = _map_column_names(col_names, column_mapping)

    # Build a new Table that mirrors source structure but for the target
    new_table = Table(
        target_table_name, metadata,
        schema=target_schema,
    )
    for col in source_table.columns:
        if col.name not in col_names:
            continue
        tgt_name = name_map[col.name]
        new_col = col.copy()
        new_col.name = tgt_name
        new_table.append_column(new_col)

    # Generate CREATE TABLE DDL compiled against the target dialect
    create_ddl = str(CreateTable(new_table).compile(dialect=tgt_engine.dialect))

    # Convert AUTO_INCREMENT to target-dialect equivalents in the generated DDL
    create_ddl = _fix_autoincrement_ddl(create_ddl, tgt_engine.dialect.name, src_engine.dialect.name)

    schema_prefix = f"{target_schema}." if target_schema else ""
    qualified_name = f"{schema_prefix}{target_table_name}"
    quoter = tgt_engine.dialect.identifier_preparer
    quoted_tgt_name = quoter.quote(target_table_name)
    quoted_name = f"{quoter.quote(target_schema)}.{quoted_tgt_name}" if target_schema else quoted_tgt_name

    print(f"Generated DDL:\n{create_ddl}\n")

    if dry_run:
        print(f"[DRY RUN] Would execute the above DDL on target")
        return new_table

    # Drop existing if requested
    if drop_first:
        print(f"Dropping table {quoted_name} if exists...")
        with get_connection(tgt_engine) as conn:
            drop_ddl = str(DropTable(new_table, if_exists=True).compile(dialect=tgt_engine.dialect))
            conn.execute(text(drop_ddl))
            conn.commit()

    print(f"Creating table {quoted_name} ...")
    with get_connection(tgt_engine) as conn:
        conn.execute(text(create_ddl))
        conn.commit()

    # Verify the created table has all expected columns
    created = reflect_table(tgt_engine, target_schema, target_table_name)
    created_cols = {c.name for c in created.columns}
    expected_cols = {name_map[c] for c in col_names}
    missing = expected_cols - created_cols
    extra = created_cols - expected_cols
    if missing:
        print(f"Warning: columns missing in created table: {missing}")
    if extra:
        print(f"Info: extra columns in target table: {extra}")

    # Create indexes (skip the ones matching PK — already created inline)
    if not no_indexes:
        pk_cols = {c.name for c in source_table.primary_key.columns}
        for idx in source_table.indexes:
            idx_cols = [c.name for c in idx.columns]
            if set(idx_cols) == pk_cols:
                continue  # already created by PRIMARY KEY in CREATE TABLE
            mapped_cols = [name_map.get(c, c) for c in idx_cols]
            quoted_cols = ", ".join(quoter.quote(c) for c in mapped_cols)
            idx_name = quoter.quote(f"idx_{target_table_name}_" + "_".join(mapped_cols))
            idx_ddl = f"CREATE INDEX {idx_name} ON {quoted_name} ({quoted_cols})"
            if idx.unique:
                idx_ddl = f"CREATE UNIQUE INDEX {idx_name} ON {quoted_name} ({quoted_cols})"
            if dry_run:
                print(f"[DRY RUN] Would execute: {idx_ddl}")
            else:
                with get_connection(tgt_engine) as conn:
                    conn.execute(text(idx_ddl))
                    conn.commit()
                print(f"  Created index: {idx_name}")

    # Create foreign keys
    if not no_fk:
        for fk in source_table.foreign_keys:
            fk_col = fk.parent.name
            if fk_col not in name_map:
                continue
            tgt_fk_col = name_map[fk_col]
            ref_table = fk.column.table.name
            ref_col = fk.column.name
            fk_name = quoter.quote(f"fk_{target_table_name}_{tgt_fk_col}")
            fk_ddl = (
                f"ALTER TABLE {quoted_name} "
                f"ADD CONSTRAINT {fk_name} FOREIGN KEY ({quoter.quote(tgt_fk_col)}) "
                f"REFERENCES {quoter.quote(ref_table)} ({quoter.quote(ref_col)})"
            )
            if dry_run:
                print(f"[DRY RUN] Would execute: {fk_ddl}")
            else:
                with get_connection(tgt_engine) as conn:
                    conn.execute(text(fk_ddl))
                    conn.commit()
                print(f"  Created FK: {fk_name}")

    # Return the already-verified table handle
    return created


def _fix_autoincrement_ddl(ddl: str, target_dialect: str, source_dialect: str) -> str:
    """Post-process DDL for auto-increment compatibility across dialects."""
    if target_dialect == source_dialect:
        return ddl

    if target_dialect == "postgresql":
        # MySQL AUTO_INCREMENT → SERIAL / BIGSERIAL
        # SQLAlchemy compiles Integer+autoincrement to SERIAL for PG already,
        # but raw AUTO_INCREMENT fragments shouldn't appear; strip them if they do
        ddl = re.sub(r'\bAUTO_INCREMENT\b', '', ddl, flags=re.IGNORECASE)
        # Replace TINYINT with SMALLINT (PG doesn't have TINYINT)
        ddl = re.sub(r'\bTINYINT\b', 'SMALLINT', ddl, flags=re.IGNORECASE)
        # MEDIUMINT → INTEGER
        ddl = re.sub(r'\bMEDIUMINT\b', 'INTEGER', ddl, flags=re.IGNORECASE)
        # DATETIME with fractional seconds → TIMESTAMP
        ddl = re.sub(r'\bDATETIME(\([^)]+\))?\b', 'TIMESTAMP', ddl, flags=re.IGNORECASE)
        # TINYTEXT/MEDIUMTEXT/LONGTEXT → TEXT
        ddl = re.sub(r'\bTINYTEXT\b', 'TEXT', ddl, flags=re.IGNORECASE)
        ddl = re.sub(r'\bMEDIUMTEXT\b', 'TEXT', ddl, flags=re.IGNORECASE)
        ddl = re.sub(r'\bLONGTEXT\b', 'TEXT', ddl, flags=re.IGNORECASE)
        # BLOB variants → BYTEA
        ddl = re.sub(r'\bTINYBLOB\b', 'BYTEA', ddl, flags=re.IGNORECASE)
        ddl = re.sub(r'\bMEDIUMBLOB\b', 'BYTEA', ddl, flags=re.IGNORECASE)
        ddl = re.sub(r'\bLONGBLOB\b', 'BYTEA', ddl, flags=re.IGNORECASE)
        ddl = re.sub(r'\bBLOB\b', 'BYTEA', ddl, flags=re.IGNORECASE)
        # ENUM → VARCHAR(255), keep it simple
        ddl = re.sub(r'\bENUM\([^)]+\)', 'VARCHAR(255)', ddl, flags=re.IGNORECASE)
        # UNSIGNED → remove (PG doesn't have it)
        ddl = re.sub(r'\bUNSIGNED\b', '', ddl, flags=re.IGNORECASE)
        # Strip ON UPDATE CURRENT_TIMESTAMP (MySQL-only, PG doesn't support)
        ddl = re.sub(r'\bON\s+UPDATE\s+CURRENT_TIMESTAMP(\s*\([^)]*\))?', '', ddl, flags=re.IGNORECASE)
        # Strip DEFAULT_GENERATED (MySQL expression defaults)
        ddl = re.sub(r'\bDEFAULT_GENERATED\b', '', ddl, flags=re.IGNORECASE)
        # CHARACTER SET + COLLATE → remove (PG uses per-database encoding)
        ddl = re.sub(r'\bCHARACTER\s+SET\s+\w+', '', ddl, flags=re.IGNORECASE)
        ddl = re.sub(r'\bCOLLATE\s+\w+', '', ddl, flags=re.IGNORECASE)

    elif target_dialect == "mysql":
        # PG SERIAL → INTEGER AUTO_INCREMENT
        ddl = re.sub(r'\bSERIAL\b', 'INTEGER AUTO_INCREMENT', ddl, flags=re.IGNORECASE)
        ddl = re.sub(r'\bBIGSERIAL\b', 'BIGINT AUTO_INCREMENT', ddl, flags=re.IGNORECASE)
        # BYTEA → LONGBLOB
        ddl = re.sub(r'\bBYTEA\b', 'LONGBLOB', ddl, flags=re.IGNORECASE)
        # TEXT (unbounded) is fine on both
        # BOOLEAN → TINYINT(1)
        ddl = re.sub(r'\bBOOLEAN\b', 'TINYINT(1)', ddl, flags=re.IGNORECASE)
        # TIMESTAMP WITH TIME ZONE / TIMESTAMPTZ → DATETIME
        ddl = re.sub(r'\bTIMESTAMPTZ\b', 'DATETIME', ddl, flags=re.IGNORECASE)
        ddl = re.sub(r'\bTIMESTAMP WITH(OUT)? TIME ZONE\b', 'DATETIME', ddl, flags=re.IGNORECASE)

    elif target_dialect == "sqlite":
        # Strip AUTO_INCREMENT (SQLite uses AUTOINCREMENT with INTEGER PRIMARY KEY)
        ddl = re.sub(r'\bAUTO_INCREMENT\b', 'AUTOINCREMENT', ddl, flags=re.IGNORECASE)
        ddl = re.sub(r'\bSERIAL\b', 'INTEGER', ddl, flags=re.IGNORECASE)
        ddl = re.sub(r'\bBIGSERIAL\b', 'INTEGER', ddl, flags=re.IGNORECASE)
        # Most types just map to affinity; strip lengths from non-string types roughly
        ddl = re.sub(r'\bBYTEA\b', 'BLOB', ddl, flags=re.IGNORECASE)
        ddl = re.sub(r'\bTIMESTAMP(TZ)?\b', 'TEXT', ddl, flags=re.IGNORECASE)
        ddl = re.sub(r'\bJSON\b', 'TEXT', ddl, flags=re.IGNORECASE)
        ddl = re.sub(r'\bARRAY\b', 'TEXT', ddl, flags=re.IGNORECASE)

    return ddl


def sync(src_engine: Engine, tgt_engine: Engine,
         source_table: Table, target_table: Table,
         src_cols: list[str], col_mapping: dict[str, str],
         batch_size: int, truncate: bool, where_clause: str | None,
         source_schema: str | None, target_schema: str | None,
         dry_run: bool) -> dict:
    stats = {"total": 0, "inserted": 0, "errors": 0}

    # Count source rows
    src_total = row_count(src_engine, source_schema, source_table.name, where_clause)
    stats["total"] = src_total
    print(f"Source rows: {src_total}")

    if dry_run:
        print(f"[DRY RUN] Would insert {src_total} rows into target table '{target_table.name}'")
        if truncate:
            print("[DRY RUN] Would truncate target table first")
        return stats

    # Truncate target if requested
    if truncate:
        print(f"Truncating target table {tgt_quoted_table}...")
        with get_connection(tgt_engine) as conn:
            conn.execute(text(truncate_sql))
            conn.commit()

    # Build SELECT query with source-dialect-quoted column names
    src_quoter = src_engine.dialect.identifier_preparer
    schema_prefix = f"{source_schema}." if source_schema else ""
    src_quoted_cols = ", ".join(src_quoter.quote(c) for c in src_cols)
    src_quoted_table = src_quoter.quote(source_table.name)
    if source_schema:
        src_quoted_table = f"{src_quoter.quote(source_schema)}.{src_quoted_table}"
    query = f"SELECT {src_quoted_cols} FROM {src_quoted_table}"
    if where_clause:
        query += f" WHERE {where_clause}"

    tgt_quoter = tgt_engine.dialect.identifier_preparer
    tgt_schema_prefix = f"{target_schema}." if target_schema else ""
    tgt_cols = [col_mapping[c] for c in src_cols]
    tgt_quoted_cols = [tgt_quoter.quote(c) for c in tgt_cols]
    tgt_quoted_table = tgt_quoter.quote(target_table.name)
    if target_schema:
        tgt_quoted_table = f"{tgt_quoter.quote(target_schema)}.{tgt_quoted_table}"
    placeholders = ", ".join(f":{c}" for c in tgt_cols)
    insert_sql = f"INSERT INTO {tgt_quoted_table} ({', '.join(tgt_quoted_cols)}) VALUES ({placeholders})"
    # DELETE truncation also needs quoting
    truncate_sql = f"DELETE FROM {tgt_quoted_table}"

    print(f"Syncing {src_total} rows in batches of {batch_size}...")
    with get_connection(tgt_engine) as tgt_conn:
        offset = 0
        with get_connection(src_engine) as src_conn:
            src_conn = src_conn.execution_options(stream_results=True, yield_per=batch_size)
            while True:
                batch_query = f"{query} LIMIT {batch_size} OFFSET {offset}"
                result = src_conn.execute(text(batch_query))
                rows = [dict(row._mapping) for row in result.fetchall()]
                if not rows:
                    break

                # Map source column names to target column names
                mapped_rows = []
                for row in rows:
                    mapped = {}
                    for src_col, tgt_col in col_mapping.items():
                        mapped[tgt_col] = row[src_col]
                    mapped_rows.append(mapped)

                tgt_conn.execute(text(insert_sql), mapped_rows)
                tgt_conn.commit()
                stats["inserted"] += len(rows)
                offset += batch_size
                print(f"  Progress: {stats['inserted']}/{src_total} rows")

    print(f"Done. Inserted {stats['inserted']} rows.")
    if stats["total"] != stats["inserted"]:
        print(f"Warning: expected {stats['total']} rows but inserted {stats['inserted']}")
    return stats


def export_schema_to_sql(engine: Engine, table: Table, output_path: str):
    """Export table schema as CREATE TABLE statement to SQL file."""
    create_ddl = str(CreateTable(table).compile(dialect=engine.dialect))
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(create_ddl)
        f.write(';\n')


def export_data_to_csv(engine: Engine, schema: str | None, table_name: str, output_path: str, batch_size: int = 1000):
    """Export table data to CSV file with UTF-8 BOM encoding."""
    src_quoter = engine.dialect.identifier_preparer
    src_quoted_table = src_quoter.quote(table_name)
    if schema:
        src_quoted_table = f"{src_quoter.quote(schema)}.{src_quoted_table}"

    query = f"SELECT * FROM {src_quoted_table}"

    with open(output_path, 'w', encoding='utf-8-sig', newline='') as csvfile:
        writer = None
        offset = 0
        with get_connection(engine) as conn:
            conn = conn.execution_options(stream_results=True, yield_per=batch_size)
            while True:
                batch_query = f"{query} LIMIT {batch_size} OFFSET {offset}"
                result = conn.execute(text(batch_query))
                rows = result.fetchall()
                if not rows:
                    break

                if writer is None:
                    fieldnames = list(rows[0]._mapping.keys())
                    writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
                    writer.writeheader()

                for row in rows:
                    writer.writerow(dict(row._mapping))

                offset += batch_size


def export_all_to_single_sql(engine: Engine, schema: str | None, table_names: list[str],
                             output_folder: str, dry_run: bool):
    """Export all tables to a single SQL file containing DDL and INSERT statements."""
    folder = Path(output_folder)
    output_file = folder / "import.sql"

    print(f"\nExporting {len(table_names)} tables to single SQL file: {output_file}")

    if dry_run:
        print(f"[DRY RUN] Would create {output_file}")
        return

    folder.mkdir(parents=True, exist_ok=True)

    with open(output_file, 'w', encoding='utf-8') as f:
        for table_name in table_names:
            print(f"\n  Processing table '{table_name}'...")
            table = reflect_table(engine, schema, table_name)

            # Write DDL
            create_ddl = str(CreateTable(table).compile(dialect=engine.dialect))
            f.write(f"-- DDL for table: {table_name}\n")
            f.write(create_ddl)
            f.write(";\n\n")

            # Write data as INSERT statements
            row_total = row_count(engine, schema, table_name, None)
            if row_total == 0:
                print(f"    Table {table_name} has 0 rows")
                continue

            print(f"    Exporting {row_total} rows...")
            f.write(f"-- Data for table: {table_name}\n")

            src_quoter = engine.dialect.identifier_preparer
            src_quoted_table = src_quoter.quote(table_name)
            if schema:
                src_quoted_table = f"{src_quoter.quote(schema)}.{src_quoted_table}"

            col_names = [c.name for c in table.columns]
            src_quoted_cols = [src_quoter.quote(c) for c in col_names]

            batch_size = 1000
            offset = 0
            with get_connection(engine) as conn:
                conn = conn.execution_options(stream_results=True, yield_per=batch_size)
                while True:
                    query = f"SELECT {', '.join(src_quoted_cols)} FROM {src_quoted_table} LIMIT {batch_size} OFFSET {offset}"
                    result = conn.execute(text(query))
                    rows = [dict(row._mapping) for row in result.fetchall()]
                    if not rows:
                        break

                    for row in rows:
                        stmt = table.insert().values(**row)
                        compiled = stmt.compile(dialect=engine.dialect, compile_kwargs={"literal_binds": True})
                        f.write(str(compiled))
                        f.write(";\n")

                    offset += batch_size
                    print(f"      Progress: {min(offset, row_total)}/{row_total} rows")

            f.write("\n")

    print(f"\n  ✓ Exported single SQL file: {output_file}")


def export_table_to_folder(engine: Engine, schema: str | None, table_name: str,
                           output_folder: str, dry_run: bool) -> dict:
    """Export a single table to folder (schema.sql + data.csv)."""
    stats = {"table": table_name, "rows": 0}

    table_folder = Path(output_folder) / table_name
    schema_file = table_folder / "schema.sql"
    data_file = table_folder / "data.csv"

    print(f"\nExporting table '{table_name}'...")

    if dry_run:
        print(f"[DRY RUN] Would create folder: {table_folder}")
        print(f"[DRY RUN] Would export schema to: {schema_file}")
        print(f"[DRY RUN] Would export data to: {data_file}")
        return stats

    table_folder.mkdir(parents=True, exist_ok=True)

    table = reflect_table(engine, schema, table_name)

    print(f"  Exporting schema to {schema_file}...")
    export_schema_to_sql(engine, table, str(schema_file))

    row_total = row_count(engine, schema, table_name, None)
    stats["rows"] = row_total
    print(f"  Exporting {row_total} rows to {data_file}...")
    export_data_to_csv(engine, schema, table_name, str(data_file))

    print(f"  ✓ Exported {table_name} ({row_total} rows)")
    return stats


def detect_folder_tables(folder_path: str) -> list[str]:
    """Scan folder and return list of importable tables (folders with schema.sql)."""
    folder = Path(folder_path)
    if not folder.exists():
        return []

    tables = []
    for item in folder.iterdir():
        if item.is_dir() and (item / "schema.sql").exists():
            tables.append(item.name)
    return sorted(tables)


def import_schema_from_sql(engine: Engine, schema_file_path: str, dry_run: bool):
    """Execute schema.sql to create table."""
    with open(schema_file_path, 'r', encoding='utf-8') as f:
        ddl = f.read()

    # Clean up PostgreSQL-specific syntax for compatibility
    dialect = engine.dialect.name
    if dialect == "postgresql":
        # Remove NULLS DISTINCT (PostgreSQL 15+ feature, not supported in older versions)
        ddl = re.sub(r'\bNULLS\s+DISTINCT\b', '', ddl, flags=re.IGNORECASE)
        # Remove NULLS NOT DISTINCT
        ddl = re.sub(r'\bNULLS\s+NOT\s+DISTINCT\b', '', ddl, flags=re.IGNORECASE)

    if dry_run:
        print(f"[DRY RUN] Would execute DDL from {schema_file_path}")
        return

    with get_connection(engine) as conn:
        conn.execute(text(ddl))
        conn.commit()


def _fix_json_field(value: str) -> str | None:
    """Convert Python dict/list string format to standard JSON format and handle empty values."""
    if not isinstance(value, str):
        return value

    stripped = value.strip()

    # Handle empty strings or strings with only quotes → None (will be inserted as NULL)
    if not stripped or stripped in ("''", '""', "''''", '""""'):
        return None

    # Check if it looks like a Python dict or list (starts with { or [ and contains single quotes)
    if (stripped.startswith('{') or stripped.startswith('[')) and "'" in stripped:
        try:
            # Try to parse as Python literal and convert to JSON
            parsed = ast.literal_eval(stripped)
            return json.dumps(parsed, ensure_ascii=False)
        except (ValueError, SyntaxError):
            # If parsing fails, return original value
            return value
    return value


def import_data_from_csv(engine: Engine, schema: str | None, table_name: str,
                        csv_path: str, truncate: bool, batch_size: int, dry_run: bool) -> int:
    """Import data from CSV file to table."""
    tgt_quoter = engine.dialect.identifier_preparer
    tgt_quoted_table = tgt_quoter.quote(table_name)
    if schema:
        tgt_quoted_table = f"{tgt_quoter.quote(schema)}.{tgt_quoted_table}"

    if truncate:
        truncate_sql = f"DELETE FROM {tgt_quoted_table}"
        if dry_run:
            print(f"[DRY RUN] Would truncate table")
        else:
            with get_connection(engine) as conn:
                conn.execute(text(truncate_sql))
                conn.commit()

    with open(csv_path, 'r', encoding='utf-8-sig', newline='') as csvfile:
        reader = csv.DictReader(csvfile)
        fieldnames = reader.fieldnames

        if not fieldnames:
            return 0

        quoted_cols = [tgt_quoter.quote(c) for c in fieldnames]
        placeholders = ", ".join(f":{c}" for c in fieldnames)
        insert_sql = f"INSERT INTO {tgt_quoted_table} ({', '.join(quoted_cols)}) VALUES ({placeholders})"

        rows = list(reader)
        total = len(rows)

        if dry_run:
            print(f"[DRY RUN] Would insert {total} rows")
            return total

        # Fix JSON fields: convert Python dict format to standard JSON
        for row in rows:
            for key in row:
                row[key] = _fix_json_field(row[key])

        inserted = 0
        with get_connection(engine) as conn:
            for i in range(0, total, batch_size):
                batch = rows[i:i + batch_size]
                conn.execute(text(insert_sql), batch)
                conn.commit()
                inserted += len(batch)

        return inserted


def import_table_from_folder(engine: Engine, schema: str | None, table_folder: str,
                             table_name: str, truncate: bool, batch_size: int, dry_run: bool) -> dict:
    """Import a single table from folder (schema.sql + data.csv)."""
    stats = {"table": table_name, "rows": 0}

    folder = Path(table_folder)
    schema_file = folder / "schema.sql"
    data_file = folder / "data.csv"

    print(f"\nImporting table '{table_name}'...")

    if not schema_file.exists():
        print(f"  [Error] schema.sql not found in {folder}")
        return stats

    if not data_file.exists():
        print(f"  [Error] data.csv not found in {folder}")
        return stats

    if not table_exists(engine, schema, table_name):
        print(f"  Creating table from {schema_file}...")
        import_schema_from_sql(engine, str(schema_file), dry_run)
    else:
        print(f"  Table already exists")

    print(f"  Importing data from {data_file}...")
    rows = import_data_from_csv(engine, schema, table_name, str(data_file), truncate, batch_size, dry_run)
    stats["rows"] = rows

    print(f"  ✓ Imported {table_name} ({rows} rows)")
    return stats


def list_csv_files(folder_path: str) -> list[Path]:
    """Return CSV files directly under a folder, sorted by file name."""
    folder = Path(folder_path)
    if not folder.exists() or not folder.is_dir():
        return []
    return sorted(
        (item for item in folder.iterdir() if item.is_file() and item.suffix.lower() == ".csv"),
        key=lambda item: item.name.lower(),
    )


def import_csv_folder_to_table(engine: Engine, schema: str | None, csv_folder: str,
                               table_name: str, truncate: bool, batch_size: int,
                               dry_run: bool) -> dict:
    """Import all CSV files in one folder into a single existing table."""
    stats = {"table": table_name, "files": 0, "rows": 0}
    folder = Path(csv_folder)

    print(f"\nImporting CSV folder '{folder}' → table '{table_name}'...")

    if not folder.exists() or not folder.is_dir():
        print(f"  [Error] CSV folder not found: {folder}")
        return stats

    csv_files = list_csv_files(str(folder))
    if not csv_files:
        print(f"  [Warning] No CSV files found in {folder}")
        return stats

    if not table_exists(engine, schema, table_name):
        print(f"  [Error] Target table '{table_name}' does not exist")
        return stats

    for index, csv_file in enumerate(csv_files):
        should_truncate = truncate and index == 0
        print(f"  Importing data from {csv_file}...")
        rows = import_data_from_csv(
            engine,
            schema,
            table_name,
            str(csv_file),
            should_truncate,
            batch_size,
            dry_run,
        )
        stats["files"] += 1
        stats["rows"] += rows

    print(f"  ✓ Imported {stats['files']} CSV file(s), {stats['rows']} rows into {table_name}")
    return stats


def sync_single_table(src_engine: Engine, tgt_engine: Engine,
                      args: argparse.Namespace,
                      source_table_name: str, target_table_name: str):
    """Sync a single table from source to target."""
    print(f"\n{'='*60}")
    print(f"Syncing: {source_table_name} → {target_table_name}")
    print(f"{'='*60}")

    source_table = reflect_table(src_engine, args.source_schema, source_table_name)

    if args.create_table:
        need_create = args.drop_table or not table_exists(tgt_engine, args.target_schema, target_table_name)

        if need_create:
            if args.drop_table:
                print("--drop-table specified, recreating target table...")
            else:
                print("Target table does not exist, creating from source structure...")
            target_table = create_target_table(
                src_engine, tgt_engine,
                source_table, target_table_name, args.target_schema,
                args.column_mapping, args.columns,
                args.drop_table, args.no_indexes, args.no_fk,
                args.dry_run,
            )
            if args.dry_run:
                return
            if args.columns:
                src_cols = [c.strip() for c in args.columns.split(",")]
            else:
                src_cols = [c.name for c in source_table.columns]
            col_mapping = _map_column_names(src_cols, args.column_mapping)
        else:
            print("Target table already exists, skipping CREATE TABLE.")
            target_table = reflect_table(tgt_engine, args.target_schema, target_table_name)
            src_cols, col_mapping = resolve_columns(
                source_table, target_table, args.columns, args.column_mapping
            )
    else:
        target_table = reflect_table(tgt_engine, args.target_schema, target_table_name)
        src_cols, col_mapping = resolve_columns(
            source_table, target_table, args.columns, args.column_mapping
        )

    print(f"Columns to sync: {src_cols}")
    if args.column_mapping:
        print(f"Column mapping: {col_mapping}")

    sync(
        src_engine, tgt_engine,
        source_table, target_table,
        src_cols, col_mapping,
        args.batch_size, args.truncate_target, args.where,
        args.source_schema, args.target_schema,
        args.dry_run,
    )


def main():
    args = parse_args()

    # Export mode
    if args.export_to_folder:
        try:
            src_engine, _ = create_engines(args.source_url, "sqlite:///:memory:")
        except Exception as e:
            print(f"\n[Error] 无法连接源数据库: {e}")
            sys.exit(1)

        try:
            if args.all_tables:
                print(f"Source: {args.source_url} (all tables)")
                print(f"Export to: {args.export_to_folder}")

                source_tables = list_all_tables(src_engine, args.source_schema)

                if args.exclude_tables:
                    exclude_set = {t.strip() for t in args.exclude_tables.split(",")}
                    source_tables = [t for t in source_tables if t not in exclude_set]

                if args.skip2tables:
                    skip_set = {t.strip() for t in args.skip2tables.split(",")}
                    source_tables = [t for t in source_tables if t not in skip_set]

                if not source_tables:
                    print("\n[Warning] 没有找到可导出的表")
                    return

                print(f"\n找到 {len(source_tables)} 张表:")
                for i, table in enumerate(source_tables, 1):
                    print(f"  {i}. {table}")

                if not args.dry_run:
                    confirm = input(f"\n确认导出这 {len(source_tables)} 张表? [y/N] ").strip().lower()
                    if confirm != 'y':
                        print("已取消导出")
                        return

                success_count = 0
                total_rows = 0
                for table_name in source_tables:
                    try:
                        stats = export_table_to_folder(src_engine, args.source_schema, table_name,
                                                      args.export_to_folder, args.dry_run)
                        success_count += 1
                        total_rows += stats["rows"]
                    except Exception as e:
                        print(f"\n[Error] 导出表 '{table_name}' 失败: {e}")
                        continue

                print(f"\n{'='*60}")
                print(f"导出完成!")
                print(f"  成功: {success_count}/{len(source_tables)} 张表")
                print(f"  总行数: {total_rows}")
                print(f"{'='*60}")

            else:
                print(f"Source: {args.source_url}  →  table '{args.source_table}'")
                print(f"Export to: {args.export_to_folder}")
                export_table_to_folder(src_engine, args.source_schema, args.source_table,
                                      args.export_to_folder, args.dry_run)

        finally:
            src_engine.dispose()
        return

    # Import mode
    if args.import_from_folder:
        try:
            _, tgt_engine = create_engines("sqlite:///:memory:", args.target_url)
        except Exception as e:
            print(f"\n[Error] 无法连接目标数据库: {e}")
            sys.exit(1)

        try:
            if args.all_tables:
                print(f"Import from: {args.import_from_folder}")
                print(f"Target: {args.target_url}")

                available_tables = detect_folder_tables(args.import_from_folder)

                if not available_tables:
                    print("\n[Warning] 没有找到可导入的表")
                    return

                print(f"\n找到 {len(available_tables)} 张表:")
                for i, table in enumerate(available_tables, 1):
                    print(f"  {i}. {table}")

                if not args.dry_run:
                    confirm = input(f"\n确认导入这 {len(available_tables)} 张表? [y/N] ").strip().lower()
                    if confirm != 'y':
                        print("已取消导入")
                        return

                success_count = 0
                total_rows = 0
                for table_name in available_tables:
                    try:
                        table_folder = Path(args.import_from_folder) / table_name
                        stats = import_table_from_folder(tgt_engine, args.target_schema, str(table_folder),
                                                        table_name, args.truncate_target, args.batch_size,
                                                        args.dry_run)
                        success_count += 1
                        total_rows += stats["rows"]
                    except Exception as e:
                        print(f"\n[Error] 导入表 '{table_name}' 失败: {e}")
                        continue

                print(f"\n{'='*60}")
                print(f"导入完成!")
                print(f"  成功: {success_count}/{len(available_tables)} 张表")
                print(f"  总行数: {total_rows}")
                print(f"{'='*60}")

            else:
                print(f"Import from: {args.import_from_folder}")
                print(f"Target: {args.target_url}  →  table '{args.target_table}'")
                if list_csv_files(args.import_from_folder):
                    import_csv_folder_to_table(tgt_engine, args.target_schema, args.import_from_folder,
                                               args.target_table, args.truncate_target, args.batch_size,
                                               args.dry_run)
                else:
                    table_folder = Path(args.import_from_folder) / args.target_table
                    import_table_from_folder(tgt_engine, args.target_schema, str(table_folder),
                                            args.target_table, args.truncate_target, args.batch_size,
                                            args.dry_run)

        finally:
            tgt_engine.dispose()
        return

    # Import2single mode
    if args.import2signle:
        try:
            src_engine, _ = create_engines(args.source_url, "sqlite:///:memory:")
        except Exception as e:
            print(f"\n[Error] 无法连接源数据库: {e}")
            sys.exit(1)

        try:
            print(f"Source: {args.source_url} (all tables)")
            print(f"Export to single import SQL: data/import")

            source_tables = list_all_tables(src_engine, args.source_schema)

            if args.skip2tables:
                skip_set = {t.strip() for t in args.skip2tables.split(",")}
                source_tables = [t for t in source_tables if t not in skip_set]

            if not source_tables:
                print("\n[Warning] 没有找到可导出的表")
                return

            print(f"\n找到 {len(source_tables)} 张表:")
            for i, table in enumerate(source_tables, 1):
                print(f"  {i}. {table}")

            if not args.dry_run:
                confirm = input(f"\n确认导出这 {len(source_tables)} 张表到 data/import? [y/N] ").strip().lower()
                if confirm != 'y':
                    print("已取消导出")
                    return

            export_all_to_single_sql(src_engine, args.source_schema, source_tables, "data/import", args.dry_run)

        finally:
            src_engine.dispose()
        return

    # Sync mode (existing logic)
    try:
        src_engine, tgt_engine = create_engines(args.source_url, args.target_url)
    except Exception as e:
        _handle_connection_error(e, args.source_url, args.target_url)

    try:
        if args.all_tables:
            # Sync all tables mode
            print(f"Source: {args.source_url} (all tables)")
            print(f"Target: {args.target_url}")

            # Get all source tables
            source_tables = list_all_tables(src_engine, args.source_schema)

            # Filter excluded tables
            if args.exclude_tables:
                exclude_set = {t.strip() for t in args.exclude_tables.split(",")}
                source_tables = [t for t in source_tables if t not in exclude_set]

            if args.skip2tables:
                skip_set = {t.strip() for t in args.skip2tables.split(",")}
                source_tables = [t for t in source_tables if t not in skip_set]

            if not source_tables:
                print("\n[Warning] 没有找到可同步的表")
                return

            print(f"\n找到 {len(source_tables)} 张表:")
            for i, table in enumerate(source_tables, 1):
                print(f"  {i}. {table}")

            if args.dry_run:
                print("\n[DRY RUN] 以上表将被同步")

            # Ask for confirmation
            if not args.dry_run:
                confirm = input(f"\n确认同步这 {len(source_tables)} 张表? [y/N] ").strip().lower()
                if confirm != 'y':
                    print("已取消同步")
                    return

            # Sync each table
            success_count = 0
            failed_tables = []
            for table_name in source_tables:
                try:
                    sync_single_table(src_engine, tgt_engine, args, table_name, table_name)
                    success_count += 1
                except Exception as e:
                    print(f"\n[Error] 同步表 '{table_name}' 失败: {e}")
                    failed_tables.append(table_name)
                    continue

            # Summary
            print(f"\n{'='*60}")
            print(f"同步完成!")
            print(f"  成功: {success_count}/{len(source_tables)} 张表")
            if failed_tables:
                print(f"  失败: {len(failed_tables)} 张表")
                for table in failed_tables:
                    print(f"    - {table}")
            print(f"{'='*60}")

        else:
            # Single table mode
            print(f"Source: {args.source_url}  →  table '{args.source_table}'")
            print(f"Target: {args.target_url}  →  table '{args.target_table}'")
            sync_single_table(src_engine, tgt_engine, args, args.source_table, args.target_table)

    finally:
        src_engine.dispose()
        tgt_engine.dispose()


if __name__ == "__main__":
    main()
