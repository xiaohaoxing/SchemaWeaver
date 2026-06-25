"""Tests for tkmybatis adapter."""
import pytest
from pathlib import Path
from textwrap import dedent
from schemaweaver.adapters.tkmybatis import TkMybatisAdapter
from schemaweaver.model import EnumParadigm

ADAPTER = TkMybatisAdapter()

SAMPLE_ENTITY = dedent("""
    package com.example.order.entity;

    import javax.persistence.Table;
    import javax.persistence.Column;
    import javax.persistence.Id;
    import io.swagger.annotations.ApiModel;
    import io.swagger.annotations.ApiModelProperty;
    import com.walltech.common.model.entity.BaseEntity;

    @ApiModel("测试订单")
    @Table(name = "test_order")
    public class TestOrder extends BaseEntity {

        @ApiModelProperty("状态")
        @Column(name = "status")
        private Short status;

        @ApiModelProperty("删除标志")
        @Column(name = "deleted")
        private Integer deleted;

        @ApiModelProperty("发件人ID")
        @Column(name = "shipper")
        private Integer shipper;

        @ApiModelProperty("渠道ID")
        @Column(name = "channel_id")
        private Integer channelId;
    }
""")

SAMPLE_ENUM_A = dedent("""
    package com.example.order.dict;

    public enum BillingStatus {
        TO_BE_CONFIRMED((short) 1, "order.bill.status.created"),
        INVOICED((short) 7, "order.bill.status.invoiced");

        private Short status;
        private String messageKey;

        BillingStatus(Short status, String messageKey) {
            this.status = status;
            this.messageKey = messageKey;
        }
    }
""")

SAMPLE_ENUM_B = dedent("""
    package com.example.order.dict;

    import com.google.common.collect.Sets;
    import java.util.Set;

    public enum OrderStatus {
        CREATED((short) 0, "created", "OrderStatus.CREATED", true, "stat.CREATED", null),
        WAREHOUSED((short) 133, "WAREHOUSED", "OrderStatus.WAREHOUSED", true, "stat.WAREHOUSED", null),
        WAREHOUSED_18325((short) 133, "WAREHOUSED", "OrderStatus.WAREHOUSED_18325", true, "stat.WAREHOUSED", Sets.newHashSet(18325));

        private Short status;
        private String name;
        private String statusMessageKey;
        private boolean isInTab;
        private String statisticStatus;
        private Set<Integer> aggregator;

        OrderStatus(Short status, String name, String statusMessageKey, boolean isInTab, String statisticStatus, Set<Integer> aggregator) {
            this.status = status; this.name = name; this.statusMessageKey = statusMessageKey;
            this.isInTab = isInTab; this.statisticStatus = statisticStatus; this.aggregator = aggregator;
        }
    }
""")


def test_supports_file():
    path = Path("TestOrder.java")
    assert ADAPTER.supports_file(path, SAMPLE_ENTITY)
    assert not ADAPTER.supports_file(path, "public class Foo {}")


def test_parse_table_name():
    path = Path("TestOrder.java")
    table = ADAPTER.parse_table(path, SAMPLE_ENTITY, [], Path("."))
    assert table is not None
    assert table.name == "test_order"


def test_parse_table_purpose():
    path = Path("TestOrder.java")
    table = ADAPTER.parse_table(path, SAMPLE_ENTITY, [], Path("."))
    assert table.purpose == "测试订单"


def test_parse_table_soft_delete():
    path = Path("TestOrder.java")
    table = ADAPTER.parse_table(path, SAMPLE_ENTITY, [], Path("."))
    assert table.soft_delete_column == "deleted"


def test_parse_table_dimensions():
    path = Path("TestOrder.java")
    table = ADAPTER.parse_table(path, SAMPLE_ENTITY, [], Path("."))
    assert "shipper" in table.dimension_fields


def test_parse_table_columns():
    path = Path("TestOrder.java")
    table = ADAPTER.parse_table(path, SAMPLE_ENTITY, [], Path("."))
    col_names = {c.name for c in table.columns}
    assert "status" in col_names
    assert "deleted" in col_names
    assert "channel_id" in col_names


def test_parse_enum_paradigm_a():
    path = Path("BillingStatus.java")
    enum = ADAPTER.parse_enum(path, SAMPLE_ENUM_A, {}, Path("."))
    assert enum is not None
    assert enum.short_name == "BillingStatus"
    assert enum.paradigm == EnumParadigm.SIMPLE
    codes = {v.code for v in enum.values}
    assert 1 in codes
    assert 7 in codes


def test_parse_enum_paradigm_b():
    path = Path("OrderStatus.java")
    enum = ADAPTER.parse_enum(path, SAMPLE_ENUM_B, {}, Path("."))
    assert enum is not None
    assert enum.paradigm == EnumParadigm.DIMENSIONAL
    # code=133 should have two entries
    code133 = [v for v in enum.values if v.code == 133]
    assert len(code133) == 2
    # One with aggregators=[18325] and warning
    agg_entry = next((v for v in code133 if v.aggregators), None)
    assert agg_entry is not None
    assert 18325 in agg_entry.aggregators
    assert agg_entry.warning is not None


def test_parse_enum_i18n():
    i18n = {"order.bill.status.created": "待确认", "order.bill.status.invoiced": "已开票"}
    path = Path("BillingStatus.java")
    enum = ADAPTER.parse_enum(path, SAMPLE_ENUM_A, i18n, Path("."))
    confirmed = next(v for v in enum.values if v.code == 1)
    assert confirmed.i18n_zh == "待确认"
