# Db2Db

Supported databases: MySQL (pymysql), PostgreSQL (psycopg2), SQLite (via SQLAlchemy)


数据库表同步工具，支持将数据从一个数据库的表同步到另一个数据库的表。基于 SQLAlchemy 构建，支持 MySQL、PostgreSQL、SQLite 之间的跨库同步。

## 安装

```bash
# 创建虚拟环境
python -m venv venv

# 激活虚拟环境
# Windows PowerShell
venv\Scripts\Activate.ps1
# Windows CMD
venv\Scripts\activate.bat
# Linux / macOS
source venv/bin/activate

# 安装依赖
pip install -r requirements.txt
```

依赖项：

- `sqlalchemy >= 2.0`
- `pymysql >= 1.1`（MySQL/MariaDB）
- `psycopg2-binary >= 2.9`（PostgreSQL）

## 快速开始

```bash
python sync_data.py \
  --source-url "mysql+pymysql://user:pass@host:3306/source_db" \
  --target-url "postgresql+psycopg2://user:pass@host:5432/target_db" \
  --source-table "users" \
  --target-table "users"
```

## 参数说明

### 模式参数

| 参数 | 说明 |
|---|---|
| 无特殊参数 | **同步模式**：需要 `--source-url` + `--target-url` |
| `--export-to-folder <path>` | **导出模式**：需要 `--source-url` + 导出路径 |
| `--import-from-folder <path>` | **导入模式**：需要 `--target-url` + 导入路径 |
| `--import2signle` | **单文件导出模式**：需要 `--source-url`，导出所有表为 `data/import/import.sql`（含 DDL + INSERT） |

### 数据库连接参数

| 参数 | 说明 |
|---|---|
| `--source-url` | 源数据库连接 URL（同步/导出模式必需） |
| `--target-url` | 目标数据库连接 URL（同步/导入模式必需） |
| `--source-schema` | 源 schema 名称 |
| `--target-schema` | 目标 schema 名称 |

### 表选择参数

| 参数 | 说明 |
|---|---|
| `--source-table` | 源表名（同步/导出单表时使用） |
| `--target-table` | 目标表名（同步/导入单表时使用） |
| `--all-tables` | 处理所有表（同步/导出/导入整库） |
| `--exclude-tables` | 排除的表名，逗号分隔（配合 `--all-tables` 使用） |
| `--skip2tables` | 跳过的表名，逗号分隔（配合 `--all-tables` 或 `--import2signle` 使用） |

### 数据处理参数（仅同步模式）

| 参数 | 说明 |
|---|---|
| `--where` | SQL WHERE 过滤条件，如 `"id > 100"` |
| `--columns` | 指定同步的列，逗号分隔 |
| `--column-mapping` | 列名映射，格式为 `"src_col1=tgt_col1,src_col2=tgt_col2"` |

### 表创建参数（仅同步模式）

| 参数 | 说明 |
|---|---|
| `--create-table` | 自动根据源表结构创建目标表 |
| `--drop-table` | 配合 `--create-table` 使用，先删除目标表再重建 |
| `--no-indexes` | 配合 `--create-table` 使用，跳过创建索引 |
| `--no-fk` | 配合 `--create-table` 使用，跳过创建外键 |

### 通用参数

| 参数 | 说明 |
|---|---|
| `--batch-size` | 每批插入的行数，默认 `1000` |
| `--truncate-target` | 导入/同步前清空目标表数据 |
| `--dry-run` | 预览模式，仅显示将要执行的操作，不实际执行 |

## 功能模式

EasyDBSync 支持三种工作模式：

1. **同步模式**（数据库 → 数据库）- 将数据从一个数据库表同步到另一个数据库表
2. **导出模式**（数据库 → 文件夹）- 将数据库表导出为文件（表结构 + 数据）
3. **导入模式**（文件夹 → 数据库）- 从文件导入数据到数据库

---

## 使用场景

### 同步模式

#### 1. 同步到已存在的表

```bash
python sync_data.py \
  --source-url "mysql+pymysql://root:123@localhost:3306/mydb" \
  --target-url "postgresql+psycopg2://postgres:123@localhost:5432/newdb" \
  --source-table "orders" \
  --target-table "orders"
```

### 2. 自动建表并同步

自动从源表结构创建目标表，支持跨数据库类型（如 MySQL → PostgreSQL）的类型映射：

```bash
python sync_data.py \
  --source-url "mysql+pymysql://root:123@localhost:3306/mydb" \
  --target-url "postgresql+psycopg2://postgres:123@localhost:5432/newdb" \
  --source-table "orders" \
  --target-table "orders" \
  --create-table --drop-table
```

### 3. 同步部分数据

通过 `--where` 过滤行，通过 `--columns` 选择列：

```bash
python sync_data.py \
  --source-url "mysql+pymysql://root:123@localhost:3306/mydb" \
  --target-url "postgresql+psycopg2://postgres:123@localhost:5432/newdb" \
  --source-table "logs" \
  --target-table "logs" \
  --where "created_at > '2025-01-01'" \
  --columns "id,message,created_at"
```

### 4. 列名映射

当源表和目标表的列名不一致时，使用 `--column-mapping` 建立映射关系：

```bash
python sync_data.py \
  --source-url "..." --target-url "..." \
  --source-table "old_users" \
  --target-table "new_users" \
  --column-mapping "uid=user_id,uname=username"
```

#### 5. 先预览再执行

使用 `--dry-run` 查看将要执行的操作，确认无误后再正式运行：

```bash
# 预览
python sync_data.py ... --dry-run

# 确认后执行
python sync_data.py ...
```

---

### 导出模式（数据库 → 文件夹）

将数据库表导出为文件，便于备份、迁移或版本控制。

**文件格式：**
- 表结构：`schema.sql`（CREATE TABLE 语句）
- 表数据：`data.csv`（UTF-8 with BOM 编码，Excel 友好）

**文件夹结构：**
```
backup/
├── users/
│   ├── schema.sql
│   └── data.csv
├── orders/
│   ├── schema.sql
│   └── data.csv
└── products/
    ├── schema.sql
    └── data.csv
```

#### 1. 导出单个表

```bash
python sync_data.py \
  --source-url "mysql+pymysql://root:123@localhost:3306/mydb" \
  --export-to-folder ./backup \
  --source-table users
```

#### 2. 导出整个数据库

```bash
python sync_data.py \
  --source-url "mysql+pymysql://root:123@localhost:3306/mydb" \
  --export-to-folder ./backup \
  --all-tables
```

#### 3. 导出时排除某些表

```bash
python sync_data.py \
  --source-url "mysql+pymysql://root:123@localhost:3306/mydb" \
  --export-to-folder ./backup \
  --all-tables \
  --exclude-tables "temp_table,log_table"
```

---

### 单文件导出模式（数据库 → data/import/import.sql）

将整库所有表导出为单个 SQL 文件，包含表结构 DDL 与 INSERT 数据，便于一次性导入。

**输出文件：**
- `data/import/import.sql`

#### 1. 导出所有表到单个 SQL 文件

```bash
python sync_data.py \
  --source-url "mysql+pymysql://root:123@localhost:3306/mydb" \
  --import2signle
```

#### 2. 跳过指定表

```bash
python sync_data.py \
  --source-url "mysql+pymysql://root:123@localhost:3306/mydb" \
  --import2signle \
  --skip2tables "temp_table,log_table"
```

---

### 导入模式（文件夹 → 数据库）

从导出的文件夹恢复数据到数据库。

**特性：**
- 自动从 `schema.sql` 创建表（如果表不存在）
- 支持 `--truncate-target` 清空现有数据后导入
- 批量插入，默认每批 1000 行

#### 1. 从 CSV 文件夹导入到指定表

```bash
python sync_data.py \
  --target-url "postgresql+psycopg2://postgres:123@localhost:5432/newdb" \
  --import-from-folder ./csv_files \
  --target-table users
```

`./csv_files` 目录下的所有 `*.csv` 会按文件名排序导入到 `users` 表。目标表需要已存在，CSV 第一行需要是字段名。

#### 2. 导入整个数据库

```bash
python sync_data.py \
  --target-url "postgresql+psycopg2://postgres:123@localhost:5432/newdb" \
  --import-from-folder ./backup \
  --all-tables
```

#### 3. 导入前清空表数据

```bash
python sync_data.py \
  --target-url "postgresql+psycopg2://postgres:123@localhost:5432/newdb" \
  --import-from-folder ./backup \
  --all-tables \
  --truncate-target
```

#### 4. 预览导入操作

```bash
python sync_data.py \
  --target-url "postgresql+psycopg2://postgres:123@localhost:5432/newdb" \
  --import-from-folder ./backup \
  --all-tables \
  --dry-run
```

## 连接 URL 格式

连接 URL 遵循 [SQLAlchemy 数据库 URL](https://docs.sqlalchemy.org/en/20/core/engines.html#database-urls) 格式：

```
dialect+driver://username:password@host:port/database
```

常用示例：

```
# MySQL / MariaDB
mysql+pymysql://user:pass@localhost:3306/mydb

# PostgreSQL
postgresql+psycopg2://user:pass@localhost:5432/mydb

# SQLite
sqlite:///path/to/db.sqlite
```

密码中的特殊字符（如 `#`、`@`）会自动进行 URL 编码，无需手动处理。

## 跨库类型映射

使用 `--create-table` 时，工具会自动处理不同数据库之间的类型差异：

| MySQL | PostgreSQL | SQLite |
|---|---|---|
| `AUTO_INCREMENT` | `SERIAL` | `AUTOINCREMENT` |
| `TINYINT` | `SMALLINT` | - |
| `MEDIUMINT` | `INTEGER` | - |
| `DATETIME` | `TIMESTAMP` | `TEXT` |
| `TINYTEXT/MEDIUMTEXT/LONGTEXT` | `TEXT` | - |
| `TINYBLOB/MEDIUMBLOB/LONGBLOB` | `BYTEA` | `BLOB` |
| `ENUM(...)` | `VARCHAR(255)` | - |
| `UNSIGNED` | 移除 | - |
| `BOOLEAN` | - | `TINYINT(1)`（PG→MySQL） |

## 注意事项

- 同步过程使用批量插入，默认每批 1000 行，可通过 `--batch-size` 调整
- 使用 `--truncate-target` 会清空目标表所有数据，请谨慎使用
- 使用 `--dry-run` 可以在执行前预览操作，避免误操作
- 对于大表同步，建议配合 `--where` 分批同步，避免内存溢出

