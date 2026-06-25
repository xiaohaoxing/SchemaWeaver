"""Tests for SQLite store and FTS5 search."""
import pytest
import tempfile
from pathlib import Path
from schemaweaver.store import SchemaStore
from schemaweaver.model import Table, Column, EnumDef, EnumValue, EnumParadigm, ColType, I18nKey


@pytest.fixture
def store(tmp_path):
    db = tmp_path / "test.db"
    s = SchemaStore(db)
    yield s
    s.close()


@pytest.fixture
def sample_tables():
    return [
        Table(
            name="pcl_order",
            entity_class="com.example.PclOrder",
            purpose="订单主表",
            domain="01-pcl",
            soft_delete_column=None,
            dimension_fields=["aggregator", "shipper"],
            columns=[
                Column(name="id", java_field="id", type=ColType.LONG, is_primary_key=True, comment="主键"),
                Column(name="status", java_field="status", type=ColType.INTEGER, comment="状态"),
                Column(name="shipper", java_field="shipper", type=ColType.INTEGER, comment="发件人ID"),
            ],
        ),
        Table(
            name="act_bill",
            entity_class="com.example.ActBill",
            purpose="账单主表",
            domain="02-act",
            soft_delete_column=None,
            dimension_fields=["aggregator"],
            columns=[
                Column(name="id", java_field="id", type=ColType.LONG, is_primary_key=True),
                Column(name="bill_no", java_field="billNo", type=ColType.STRING, comment="账单编号"),
            ],
        ),
    ]


@pytest.fixture
def sample_enums():
    return [
        EnumDef(
            enum_class="com.example.OrderStatus",
            short_name="OrderStatus",
            paradigm=EnumParadigm.DIMENSIONAL,
            values=[
                EnumValue(code=0, name="CREATED", i18n_zh="已创建", aggregators=None, is_default_meaning=True),
                EnumValue(code=133, name="WAREHOUSED", i18n_zh="已入库", aggregators=None, is_default_meaning=True),
                EnumValue(code=133, name="WAREHOUSED_18325", i18n_zh="已下单", aggregators=[18325],
                          is_default_meaning=False, warning="同 code=133 对 aggregator=[18325] 含义不同"),
            ],
        )
    ]


def test_save_and_list_repos(store, sample_tables, sample_enums):
    store.save_repo("cfs", Path("/repo/cfs"), sample_tables, sample_enums, {}, "0.1.0")
    row = store.conn.execute("SELECT repo_id, table_count FROM repos WHERE repo_id='cfs'").fetchone()
    assert row is not None
    assert row[1] == 2


def test_table_saved(store, sample_tables, sample_enums):
    store.save_repo("cfs", Path("/repo/cfs"), sample_tables, sample_enums, {}, "0.1.0")
    row = store.conn.execute("SELECT name, purpose, domain FROM tables WHERE name='pcl_order'").fetchone()
    assert row is not None
    assert row[1] == "订单主表"
    assert row[2] == "01-pcl"


def test_enum_paradigm_b_values(store, sample_tables, sample_enums):
    store.save_repo("cfs", Path("/repo/cfs"), sample_tables, sample_enums, {}, "0.1.0")
    rows = store.conn.execute(
        "SELECT code, i18n_zh, aggregators, warning FROM enum_values "
        "WHERE enum_class='com.example.OrderStatus' AND code=133"
    ).fetchall()
    assert len(rows) == 2
    agg_row = next((r for r in rows if r[2] is not None), None)
    assert agg_row is not None
    assert "18325" in agg_row[2]
    assert agg_row[3] is not None


def test_fts5_search_chinese(store, sample_tables, sample_enums):
    store.save_repo("cfs", Path("/repo/cfs"), sample_tables, sample_enums, {}, "0.1.0")
    rows = store.conn.execute(
        "SELECT table_name FROM tables_fts WHERE tables_fts MATCH '订单'"
    ).fetchall()
    table_names = {r[0] for r in rows}
    assert "pcl_order" in table_names


def test_fts5_search_english(store, sample_tables, sample_enums):
    store.save_repo("cfs", Path("/repo/cfs"), sample_tables, sample_enums, {}, "0.1.0")
    rows = store.conn.execute(
        "SELECT table_name FROM tables_fts WHERE tables_fts MATCH 'pcl_order'"
    ).fetchall()
    assert any(r[0] == "pcl_order" for r in rows)


def test_replace_on_re_extract(store, sample_tables, sample_enums):
    store.save_repo("cfs", Path("/repo/cfs"), sample_tables, sample_enums, {}, "0.1.0")
    store.save_repo("cfs", Path("/repo/cfs"), sample_tables, sample_enums, {}, "0.1.0")
    count = store.conn.execute("SELECT COUNT(*) FROM tables WHERE repo_id='cfs'").fetchone()[0]
    assert count == 2  # No duplicates
