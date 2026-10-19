"""SchemaWeaver MCP server — 4 core tools via FastMCP.

工具的**实现**在 :mod:`schemaweaver.tools`（与传输无关，外部服务层也复用同一份）；
本模块只负责把它们注册到 FastMCP。``db_path`` 在 ``build_server`` 时解析一次，
所有工具共用同一个索引库。
"""
from pathlib import Path
from mcp.server.fastmcp import FastMCP
from .tools import (
    enum_resolve_impl,
    schema_search_impl,
    sql_grounding_check_impl,
    table_detail_impl,
)


def build_server(db_path: str | Path) -> FastMCP:
    db_path = Path(db_path).expanduser().resolve()
    mcp = FastMCP("schemaweaver")

    @mcp.tool()
    def schema_search(
        query: str,
        repo: str | None = None,
        domain: str | None = None,
        limit: int = 10,
    ) -> dict:
        """SchemaWeaver schema_search: search tables by natural language query (FTS5 BM25).

        Returns ranked candidate tables with purpose, domain, soft-delete flag,
        and dimension fields.

        Args:
            query: Natural language or keyword query (e.g. "订单状态", "billing")
            repo: Repo alias (default: most recently extracted repo)
            domain: Optional domain filter (e.g. "01-pcl", "02-act")
            limit: Max results (default 10)
        """
        return schema_search_impl(db_path, query, repo=repo, domain=domain, limit=limit)

    @mcp.tool()
    def table_detail(
        table: str,
        repo: str | None = None,
    ) -> dict:
        """SchemaWeaver table_detail: get full details for a single table.

        Includes columns, enum bindings, relations, traps, soft-delete guidance,
        and dimension-filter guidance.

        Args:
            table: Physical table name (e.g. "pcl_order")
            repo: Repo alias (default: most recently extracted repo)
        """
        return table_detail_impl(db_path, table, repo=repo)

    @mcp.tool()
    def enum_resolve(
        enum_class: str | None = None,
        table: str | None = None,
        column: str | None = None,
        repo: str | None = None,
    ) -> dict:
        """SchemaWeaver enum_resolve: resolve enum codes to meanings.

        Handles Paradigm B aggregator divergence.

        Provide either `enum_class` OR both `table` + `column`.

        Args:
            enum_class: Fully qualified enum class name (e.g. "com.example.OrderStatus")
                        or short name (e.g. "OrderStatus")
            table: Table name (alternative to enum_class)
            column: Column name (alternative to enum_class, requires table)
            repo: Repo alias
        """
        return enum_resolve_impl(
            db_path, enum_class=enum_class, table=table, column=column, repo=repo
        )

    @mcp.tool()
    def sql_grounding_check(
        sql: str,
        repo: str | None = None,
        aggregator_context: int | None = None,
    ) -> dict:
        """SchemaWeaver sql_grounding_check: pre-flight SQL grounding check.

        Checks column existence, enum ambiguity, and dimension filters.

        Detects:
        - Column missing (including soft-delete column misuse)
        - Enum code ambiguity (Paradigm B: same code, different meaning per aggregator)
        - Missing dimension filter (cross-tenant data leak risk)

        Args:
            sql: SQL statement to check
            repo: Repo alias
            aggregator_context: Current aggregator ID (helps clarify Paradigm B warnings)
        """
        return sql_grounding_check_impl(
            db_path, sql, repo=repo, aggregator_context=aggregator_context
        )

    return mcp
