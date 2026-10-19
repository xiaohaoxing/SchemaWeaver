"""4 个工具实现的回归测试（``schemaweaver.tools``）。

## 为什么需要

2026-10 把工具实现从 ``build_server()`` 的闭包抽到模块级函数 —— 抽取的**唯一**正确性
标准是「行为不变」。而本包原有的 20 个测试没有一条真正调用过这 4 个工具（只检查了
docstring 文本），所以抽取后哪怕逻辑写错也全绿。

这里用**最小合成库**把 4 个工具的典型路径各打一遍：检索、表详情、枚举解析、SQL 预检。
库里刻意包含范式 B 的枚举、软删除列、维度字段、人工 trap —— 正是这 4 个工具存在的理由。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from schemaweaver.store import SchemaStore
from schemaweaver.tools import (
    enum_resolve_impl,
    schema_search_impl,
    sql_grounding_check_impl,
    table_detail_impl,
)

REPO = "test-repo"


def _seed(db_path) -> str:
    """建库 + 灌一条最小但「像真的」的 schema。返回 repo_id。"""
    SchemaStore(db_path)        # 复用生产的 DDL，避免测试里手抄一份表结构
    conn = sqlite3.connect(str(db_path))
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO repos (repo_id, root_path, orm_primary, extracted_at, extractor_ver,"
        " table_count, enum_count, base_entity, domain_map)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (REPO, "/tmp/repo", "tkmybatis", now, "test", 1, 1, "BaseEntity", "{}"),
    )
    conn.execute(
        "INSERT INTO tables (repo_id, name, entity_class, source_file, purpose, domain,"
        " extends_base, soft_delete_column, dimension_fields)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (REPO, "pcl_order", "PclOrder", "src/PclOrder.java", "订单主表", "01-pcl",
         1, "deleted", '["aggregator"]'),
    )
    columns = [
        ("id", "BIGINT", 1, None),
        ("order_no", "String", 0, None),
        ("status", "Short", 0, "com.example.OrderStatus"),
        ("aggregator", "BIGINT", 0, None),
        ("deleted", "BIGINT", 0, None),
    ]
    for ordinal, (name, type_raw, is_pk, enum_class) in enumerate(columns, 1):
        conn.execute(
            "INSERT INTO columns (repo_id, table_name, name, java_field, type_raw, type,"
            " is_pk, is_nullable, comment, name_inferred, from_base_entity, enum_class, ordinal)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (REPO, "pcl_order", name, name, type_raw, type_raw.lower(), is_pk, 1,
             f"{name} 注释", 0, 0, enum_class, ordinal),
        )
    conn.execute(
        "INSERT INTO enums (repo_id, enum_class, short_name, paradigm, source_file, java_type)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (REPO, "com.example.OrderStatus", "OrderStatus", "A", "OrderStatus.java", "Short"),
    )
    for code, name, i18n in ((100, "DRAFT", "草稿"), (400, "VOID", "废弃")):
        conn.execute(
            "INSERT INTO enum_values (repo_id, enum_class, code, name, message_key,"
            " i18n_zh, aggregators, is_default, warning) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (REPO, "com.example.OrderStatus", code, name, None, i18n, None, 1, None),
        )
    conn.execute(
        "INSERT INTO relations (repo_id, from_table, from_column, to_table, to_column,"
        " kind, confidence) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (REPO, "pcl_order", "aggregator", "sys_aggregator", "id", "many_to_one", "declared"),
    )
    conn.execute(
        "INSERT INTO table_meta (repo_id, table_name, kind, content, source, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (REPO, "pcl_order", "trap", "status=400 是废弃，不是删除", "human", now),
    )
    conn.commit()
    conn.close()
    return REPO


@pytest.fixture()
def seeded(tmp_path):
    db = tmp_path / "schema.db"
    _seed(db)
    return db


def test_schema_search_hits_table(seeded):
    out = schema_search_impl(seeded, "订单")
    assert out["repo"] == REPO
    assert out["total"] == 1
    assert out["candidates"][0]["table"] == "pcl_order"
    assert out["candidates"][0]["dimensions"] == ["aggregator"]


def test_schema_search_missing_repo_is_reported_not_raised(seeded):
    out = schema_search_impl(seeded, "订单", repo="nope")
    # 不存在 repo 时 FTS/LIKE 都命中 0 行；这里只断言不抛异常且 total=0
    assert out["total"] == 0


def test_table_detail_includes_columns_guidance_and_traps(seeded):
    out = table_detail_impl(seeded, "pcl_order")
    assert out["repo"] == REPO
    assert len(out["columns"]) == 5
    assert out["soft_delete_column"] == "deleted"
    assert "WHERE deleted = 0" in out["soft_delete_guidance"]
    assert out["dimension_fields"] == ["aggregator"]
    assert out["relations"][0]["to_table"] == "sys_aggregator"
    assert out["traps"] == ["status=400 是废弃，不是删除"]
    assert out["coverage"] == "human_curated"


def test_table_detail_unknown_table_returns_error_dict(seeded):
    out = table_detail_impl(seeded, "nope")
    assert "error" in out and "not found" in out["error"]


def test_enum_resolve_by_class_and_by_column(seeded):
    by_class = enum_resolve_impl(seeded, enum_class="OrderStatus")
    assert by_class["enum_class"] == "com.example.OrderStatus"
    assert [v["code"] for v in by_class["values"]] == [100, 400]

    by_column = enum_resolve_impl(seeded, table="pcl_order", column="status")
    assert by_column["enum_class"] == by_class["enum_class"]


def test_enum_resolve_requires_arguments(seeded):
    out = enum_resolve_impl(seeded)
    assert "error" in out


def test_grounding_passes_when_columns_and_dimension_are_present(seeded):
    out = sql_grounding_check_impl(
        seeded,
        "SELECT id FROM pcl_order WHERE aggregator = 1 AND status = 100",
    )
    assert out["verdict"] == "PASS", out["checks"]
    assert out["parsed_tables"] == ["pcl_order"]


def test_grounding_fails_on_missing_column_and_soft_delete_misuse(seeded):
    missing = sql_grounding_check_impl(seeded, "SELECT nope FROM pcl_order")
    assert missing["verdict"] == "FAIL"
    assert any(c["kind"] == "column_missing" for c in missing["checks"])

    misuse = sql_grounding_check_impl(seeded, "SELECT id FROM pcl_order WHERE is_deleted = 0")
    assert misuse["verdict"] == "FAIL"
    assert any(c["kind"] == "soft_delete_misuse" for c in misuse["checks"])


def test_grounding_warns_when_dimension_filter_missing(seeded):
    out = sql_grounding_check_impl(seeded, "SELECT id FROM pcl_order WHERE status = 100")
    assert out["verdict"] == "WARN"
    assert any(c["kind"] == "dimension_missing" for c in out["checks"])
