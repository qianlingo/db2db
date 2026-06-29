# Db2Db

[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE) [![Python](https://img.shields.io/badge/python-%3E%3D3.8-brightgreen.svg)](https://www.python.org/)

轻量级数据库表同步与迁移工具，基于 SQLAlchemy，可在不同数据库之间可靠地同步表结构与数据。

## Supported databases

- MySQL / MariaDB (driver: pymysql)
- PostgreSQL (driver: psycopg2)
- SQLite (built-in, via SQLAlchemy)

## Key features

- 跨数据库表数据同步（MySQL ⇄ PostgreSQL ⇄ SQLite）
- 自动建表与类型映射（--create-table / --drop-table）
- 批量插入与批次控制（--batch-size）
- 导出/导入为文件夹（schema.sql + data.csv）
- 支持增量/条件同步（--where / --columns / --column-mapping）
- 可在本地导出为单文件 SQL 以便离线迁移

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

同步单表示例（MySQL → PostgreSQL）：

```bash
python sync_data.py \
  --source-url "mysql+pymysql://user:pass@host:3306/source_db" \
  --target-url "postgresql+psycopg2://user:pass@host:5432/target_db" \
  --source-table "users" \
  --target-table "users" \
  --create-table
```

导出为文件夹示例：

```bash
python sync_data.py --source-url "sqlite:///my.db" --export-to-folder ./backup --all-tables
```

查看帮助：

```bash
python sync_data.py --help
```

## Contributing

欢迎提交 issue 或 PR。请在 PR 中描述变更与测试步骤。

## License

MIT
