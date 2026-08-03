"""Live MySQL schema introspection and index staleness helpers."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

from .model import ColType


class Cursor(Protocol):
    def execute(self, operation: str, params: tuple[Any, ...] = ()) -> Any: ...

    def fetchall(self) -> list[dict[str, Any]]: ...


class Connection(Protocol):
    def cursor(self) -> Cursor: ...

    def close(self) -> Any: ...


def connect_mysql(**params: Any) -> Connection:
    """Connect with PyMySQL, kept lazy so static extraction stays offline."""
    try:
        import pymysql
    except ImportError as exc:
        raise RuntimeError("live sync requires the 'pymysql' package") from exc
    return pymysql.connect(
        host=params["host"],
        port=int(params["port"]),
        user=params["user"],
        password=params["password"],
        database=params["database"],
        charset="utf8mb4",
        connect_timeout=10,
        read_timeout=60,
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=True,
    )


@dataclass
class LiveColumn:
    name: str
    column_type: str
    data_type: str
    is_nullable: bool
    is_primary_key: bool = False
    comment: str = ""
    ordinal: int = 0


@dataclass
class LiveTable:
    name: str
    comment: str = ""
    columns: list[LiveColumn] = field(default_factory=list)


def normalize_mysql_type(data_type: str) -> ColType:
    """Map MySQL information_schema data_type to SchemaWeaver's common type."""
    value = data_type.lower().split("(", 1)[0].strip()
    if value in {"char", "varchar", "enum", "set"}:
        return ColType.STRING
    if value in {"text", "tinytext", "mediumtext", "longtext"}:
        return ColType.TEXT
    if value in {"tinyint", "smallint", "mediumint", "int", "integer"}:
        return ColType.INTEGER
    if value in {"bigint"}:
        return ColType.LONG
    if value in {"decimal", "numeric", "float", "double", "real"}:
        return ColType.DECIMAL
    if value in {"boolean", "bool"}:
        return ColType.BOOLEAN
    if value in {"date"}:
        return ColType.DATE
    if value in {"datetime", "timestamp", "time", "year"}:
        return ColType.DATETIME
    if value in {"binary", "varbinary", "blob", "tinyblob", "mediumblob", "longblob"}:
        return ColType.BLOB
    return ColType.UNKNOWN


def introspect_mysql(connection: Connection, database: str) -> list[LiveTable]:
    """Read all base tables and columns from MySQL information_schema."""
    cursor = connection.cursor()
    cursor.execute(
        """
        SELECT table_name, table_comment
        FROM information_schema.tables
        WHERE table_schema = %s AND table_type = 'BASE TABLE'
        ORDER BY table_name
        """,
        (database,),
    )
    table_rows = cursor.fetchall()
    cursor.execute(
        """
        SELECT table_name, column_name, column_type, data_type, is_nullable,
               column_key, column_comment, ordinal_position
        FROM information_schema.columns
        WHERE table_schema = %s
        ORDER BY table_name, ordinal_position
        """,
        (database,),
    )
    column_rows = cursor.fetchall()

    tables = {
        _value(row, "table_name"): LiveTable(
            name=_value(row, "table_name").lower(),
            comment=_value(row, "table_comment") or "",
        )
        for row in table_rows
    }
    for row in column_rows:
        table = tables.get(_value(row, "table_name"))
        if table is None:
            continue
        table.columns.append(
            LiveColumn(
                name=_value(row, "column_name").lower(),
                column_type=_value(row, "column_type"),
                data_type=_value(row, "data_type"),
                is_nullable=_value(row, "is_nullable").upper() == "YES",
                is_primary_key=_value(row, "column_key").upper() == "PRI",
                comment=_value(row, "column_comment") or "",
                ordinal=int(_value(row, "ordinal_position")),
            )
        )
    return list(tables.values())


def _value(row: dict[str, Any], key: str) -> Any:
    """Read DictCursor results from MySQL variants with upper-case labels."""
    return row.get(key, row.get(key.upper()))


def mysql_params_from_environment(env: str) -> dict[str, Any]:
    """Read the existing Walltech test/prod MySQL settings from env vars."""
    normalized = env.lower()
    if normalized not in {"test", "prod"}:
        raise ValueError("env must be 'test' or 'prod'")
    prefix = f"MYSQL_{normalized.upper()}_"
    import os

    return {
        "host": os.environ.get(prefix + "HOST", "192.168.10.69" if normalized == "test" else "172.29.15.88"),
        "port": int(os.environ.get(prefix + "PORT", "3306")),
        "user": os.environ.get(prefix + "USER", "cfs" if normalized == "test" else "readonly"),
        "password": os.environ.get(prefix + "PASSWORD", ""),
        "database": os.environ.get(prefix + "DB", "cfs"),
    }


def stale_warning(
    extracted_at: str,
    stale_days: int = 7,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Return a user-facing warning when an index is older than the threshold."""
    try:
        indexed_at = datetime.fromisoformat(extracted_at.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if indexed_at.tzinfo is None:
        indexed_at = indexed_at.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    age_seconds = max(0, (current - indexed_at).total_seconds())
    age_days = age_seconds / 86400
    if age_days <= stale_days:
        return None
    return {
        "indexed_at": extracted_at,
        "age_days": round(age_days, 1),
        "stale_days": stale_days,
        "message": (
            f"SchemaWeaver 索引已超过 {stale_days} 天未更新（索引时间：{extracted_at}），"
            "数据库 schema 可能已发生差异，请更新索引。"
            "可通过环境变量 SCHEMAWEAVER_STALE_DAYS 调整提醒天数。"
        ),
    }
