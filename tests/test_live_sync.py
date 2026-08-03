from datetime import datetime, timedelta, timezone

from schemaweaver.live_sync import LiveColumn, LiveTable, stale_warning
from schemaweaver.model import ColType, Column, Table
from schemaweaver.store import SchemaStore


def test_stale_warning_uses_configured_day_threshold():
    extracted_at = datetime.now(timezone.utc) - timedelta(days=8)

    warning = stale_warning(
        extracted_at.isoformat(), stale_days=7, now=datetime.now(timezone.utc)
    )

    assert warning is not None
    assert warning["stale_days"] == 7
    assert "SCHEMAWEAVER_STALE_DAYS" in warning["message"]


def test_recent_schema_has_no_stale_warning():
    extracted_at = datetime.now(timezone.utc) - timedelta(days=6)

    assert (
        stale_warning(
            extracted_at.isoformat(), stale_days=7, now=datetime.now(timezone.utc)
        )
        is None
    )


def test_sync_live_schema_overlays_physical_columns_and_preserves_semantics(tmp_path):
    store = SchemaStore(tmp_path / "schema.db")
    try:
        store.save_repo(
            "cfs-jiayou",
            tmp_path / "source",
            [
                Table(
                    name="pcl_order",
                    purpose="订单主表",
                    domain="01-pcl",
                    columns=[
                        Column(
                            name="id",
                            java_field="id",
                            type=ColType.LONG,
                            comment="主键",
                            is_primary_key=True,
                        ),
                        Column(
                            name="obsolete",
                            java_field="obsolete",
                            type=ColType.STRING,
                        ),
                    ],
                    dimension_fields=["aggregator"],
                )
            ],
            enums=[],
            i18n_keys={},
            extractor_ver="0.1.0",
        )
        store.sync_live_schema(
            "cfs-jiayou",
            [
                LiveTable(
                    name="pcl_order",
                    comment="数据库订单表",
                    columns=[
                        LiveColumn(
                            name="id",
                            column_type="bigint",
                            data_type="bigint",
                            is_nullable=False,
                            is_primary_key=True,
                            comment="数据库主键",
                            ordinal=1,
                        ),
                        LiveColumn(
                            name="status",
                            column_type="smallint",
                            data_type="smallint",
                            is_nullable=True,
                            comment="数据库状态",
                            ordinal=2,
                        ),
                    ],
                )
            ],
            source_env="test",
        )

        row = store.conn.execute(
            "SELECT purpose, domain, dimension_fields FROM tables WHERE repo_id=? AND name=?",
            ("cfs-jiayou", "pcl_order"),
        ).fetchone()
        assert row[0] == "订单主表"
        assert row[1] == "01-pcl"
        assert "aggregator" in row[2]

        columns = store.conn.execute(
            "SELECT name, type_raw, type, is_pk, is_nullable, comment FROM columns "
            "WHERE repo_id=? AND table_name=? ORDER BY ordinal",
            ("cfs-jiayou", "pcl_order"),
        ).fetchall()
        assert [column[0] for column in columns] == ["id", "status"]
        assert columns[0][1:6] == ("bigint", "long", 1, 0, "数据库主键")
        assert columns[1][1:6] == ("smallint", "integer", 0, 1, "数据库状态")
    finally:
        store.close()
