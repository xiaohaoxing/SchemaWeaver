"""SchemaWeaver MCP server — 4 core tools via FastMCP."""
import json
import sqlite3
from pathlib import Path
from mcp.server.fastmcp import FastMCP
from .sql_check import SqlGroundingChecker


def build_server(db_path: str | Path) -> FastMCP:
    db_path = Path(db_path).expanduser().resolve()
    mcp = FastMCP("schemaweaver")

    def _conn() -> sqlite3.Connection:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _default_repo(conn: sqlite3.Connection, repo: str | None) -> str | None:
        if repo:
            return repo
        row = conn.execute(
            "SELECT repo_id FROM repos ORDER BY extracted_at DESC LIMIT 1"
        ).fetchone()
        return row["repo_id"] if row else None

    @mcp.tool()
    def schema_search(
        query: str,
        repo: str | None = None,
        domain: str | None = None,
        limit: int = 10,
    ) -> dict:
        """Search tables by natural language query (FTS5 BM25).

        Returns ranked candidate tables with purpose, domain, soft-delete flag,
        and dimension fields.

        Args:
            query: Natural language or keyword query (e.g. "订单状态", "billing")
            repo: Repo alias (default: most recently extracted repo)
            domain: Optional domain filter (e.g. "01-pcl", "02-act")
            limit: Max results (default 10)
        """
        conn = _conn()
        try:
            repo_id = _default_repo(conn, repo)
            if not repo_id:
                return {"error": "No repos indexed. Run `schemaweaver extract` first."}

            # FTS5 search on tables_fts
            rows = conn.execute("""
                SELECT f.table_name, t.purpose, t.domain,
                       t.soft_delete_column, t.dimension_fields
                FROM tables_fts f
                JOIN tables t ON t.repo_id = ? AND t.name = f.table_name
                WHERE f.repo_id = ? AND tables_fts MATCH ?
                ORDER BY rank
                LIMIT ?
            """, (repo_id, repo_id, query, limit)).fetchall()

            candidates = []
            for r in rows:
                if domain and r["domain"] != domain:
                    continue
                candidates.append({
                    "table": r["table_name"],
                    "purpose": r["purpose"],
                    "domain": r["domain"],
                    "soft_delete": r["soft_delete_column"],
                    "dimensions": json.loads(r["dimension_fields"]) if r["dimension_fields"] else [],
                })

            # Fallback: LIKE search if FTS returns nothing
            if not candidates:
                rows = conn.execute("""
                    SELECT name, purpose, domain, soft_delete_column, dimension_fields
                    FROM tables
                    WHERE repo_id = ? AND (name LIKE ? OR purpose LIKE ?)
                    LIMIT ?
                """, (repo_id, f"%{query}%", f"%{query}%", limit)).fetchall()
                for r in rows:
                    if domain and r["domain"] != domain:
                        continue
                    candidates.append({
                        "table": r["name"],
                        "purpose": r["purpose"],
                        "domain": r["domain"],
                        "soft_delete": r["soft_delete_column"],
                        "dimensions": json.loads(r["dimension_fields"]) if r["dimension_fields"] else [],
                    })

            return {"candidates": candidates, "total": len(candidates), "repo": repo_id}
        finally:
            conn.close()

    @mcp.tool()
    def table_detail(
        table: str,
        repo: str | None = None,
    ) -> dict:
        """Get full details for a single table: columns, enum bindings, relations, traps.

        Args:
            table: Physical table name (e.g. "pcl_order")
            repo: Repo alias (default: most recently extracted repo)
        """
        conn = _conn()
        try:
            repo_id = _default_repo(conn, repo)
            if not repo_id:
                return {"error": "No repos indexed."}

            t = conn.execute("""
                SELECT name, entity_class, source_file, purpose, domain,
                       extends_base, soft_delete_column, dimension_fields
                FROM tables WHERE repo_id = ? AND name = ?
            """, (repo_id, table)).fetchone()
            if not t:
                return {"error": f"Table '{table}' not found in repo '{repo_id}'."}

            # Columns
            cols = conn.execute("""
                SELECT name, java_field, type_raw, type, is_pk, is_nullable,
                       comment, name_inferred, from_base_entity, enum_class, ordinal
                FROM columns WHERE repo_id = ? AND table_name = ?
                ORDER BY ordinal
            """, (repo_id, table)).fetchall()

            columns = []
            enum_bindings = []
            for c in cols:
                columns.append({
                    "name": c["name"],
                    "type": c["type_raw"],
                    "is_pk": bool(c["is_pk"]),
                    "is_nullable": bool(c["is_nullable"]),
                    "comment": c["comment"],
                    "from_base_entity": bool(c["from_base_entity"]),
                    "name_inferred": bool(c["name_inferred"]),
                })
                if c["enum_class"]:
                    enum_bindings.append({
                        "column": c["name"],
                        "enum_class": c["enum_class"],
                        "bound": True,
                        "confidence": "declared",
                    })

            # Enum candidates (columns with Short/Integer type and status/type in name)
            for c in cols:
                if not c["enum_class"] and c["type_raw"] in ("Short", "Integer", "short") \
                        and any(kw in c["name"] for kw in ("status", "state", "type")):
                    # Find candidate enums by short_name matching
                    cands = conn.execute("""
                        SELECT short_name, enum_class FROM enums
                        WHERE repo_id = ? AND LOWER(short_name) LIKE ?
                        LIMIT 3
                    """, (repo_id, f"%{c['name'].replace('_', '')}%")).fetchall()
                    if cands:
                        enum_bindings.append({
                            "column": c["name"],
                            "candidates": [r["enum_class"] for r in cands],
                            "bound": False,
                            "confidence": "low",
                            "note": "命名弱匹配，待确认",
                        })

            # Relations
            rels = conn.execute("""
                SELECT from_column, to_table, to_column, kind, confidence
                FROM relations WHERE repo_id = ? AND from_table = ?
                LIMIT 20
            """, (repo_id, table)).fetchall()
            relations = [dict(r) for r in rels]

            # Human-curated traps and query templates
            metas = conn.execute("""
                SELECT kind, content, source FROM table_meta
                WHERE repo_id = ? AND table_name = ?
            """, (repo_id, table)).fetchall()
            traps = [m["content"] for m in metas if m["kind"] == "trap"]
            templates = [m["content"] for m in metas if m["kind"] == "query_template"]

            dims = json.loads(t["dimension_fields"]) if t["dimension_fields"] else []
            soft = t["soft_delete_column"]

            return {
                "table": table,
                "purpose": t["purpose"],
                "domain": t["domain"],
                "entity_class": t["entity_class"],
                "source_file": t["source_file"],
                "soft_delete_column": soft,
                "soft_delete_guidance": (
                    f"该表有 `{soft}` 列，查询活跃记录用 WHERE {soft} = 0"
                    if soft else
                    "该表无软删除列。禁用 WHERE deleted=0（会报列不存在）。废弃语义通常用 status 终态表达。"
                ),
                "dimension_fields": dims,
                "dimension_guidance": (
                    f"查询必须带维度过滤 {dims}，否则跨客户混查。" if dims else ""
                ),
                "columns": columns,
                "enum_bindings": enum_bindings,
                "relations": relations,
                "traps": traps,
                "query_templates": templates,
                "coverage": "human_curated" if traps or templates else "auto_only",
                "repo": repo_id,
            }
        finally:
            conn.close()

    @mcp.tool()
    def enum_resolve(
        enum_class: str | None = None,
        table: str | None = None,
        column: str | None = None,
        repo: str | None = None,
    ) -> dict:
        """Resolve enum codes to meanings. Handles Paradigm B aggregator divergence.

        Provide either `enum_class` OR both `table` + `column`.

        Args:
            enum_class: Fully qualified enum class name (e.g. "com.example.OrderStatus")
                        or short name (e.g. "OrderStatus")
            table: Table name (alternative to enum_class)
            column: Column name (alternative to enum_class, requires table)
            repo: Repo alias
        """
        conn = _conn()
        try:
            repo_id = _default_repo(conn, repo)
            if not repo_id:
                return {"error": "No repos indexed."}

            # Resolve enum_class
            if not enum_class and table and column:
                row = conn.execute("""
                    SELECT enum_class FROM columns
                    WHERE repo_id = ? AND table_name = ? AND name = ?
                """, (repo_id, table, column)).fetchone()
                if row and row["enum_class"]:
                    enum_class = row["enum_class"]
                else:
                    return {
                        "error": f"No enum binding found for {table}.{column}.",
                        "hint": "Use table_detail to see candidate enums for this column.",
                    }
            elif not enum_class:
                return {"error": "Provide either enum_class or (table + column)."}

            # Try exact match first, then short_name match
            e = conn.execute("""
                SELECT enum_class, short_name, paradigm, java_type FROM enums
                WHERE repo_id = ? AND (enum_class = ? OR short_name = ?)
                LIMIT 1
            """, (repo_id, enum_class, enum_class)).fetchone()
            if not e:
                return {"error": f"Enum '{enum_class}' not found in repo '{repo_id}'."}

            values = conn.execute("""
                SELECT code, name, message_key, i18n_zh, aggregators,
                       is_default, warning
                FROM enum_values WHERE repo_id = ? AND enum_class = ?
                ORDER BY code, aggregators NULLS FIRST
            """, (repo_id, e["enum_class"])).fetchall()

            vals = []
            has_divergence = False
            for v in values:
                aggs = json.loads(v["aggregators"]) if v["aggregators"] else None
                if aggs:
                    has_divergence = True
                vals.append({
                    "code": v["code"],
                    "name": v["name"],
                    "i18n_zh": v["i18n_zh"],
                    "aggregators": aggs,
                    "is_default": bool(v["is_default"]),
                    "warning": v["warning"],
                })

            return {
                "enum_class": e["enum_class"],
                "short_name": e["short_name"],
                "paradigm": e["paradigm"],
                "java_type": e["java_type"],
                "values": vals,
                "dimension_warning": (
                    "范式 B：同一 code 对不同 aggregator 含义不同，"
                    "查询必须联合 aggregator 判断，否则误读。" if has_divergence else ""
                ),
                "repo": repo_id,
            }
        finally:
            conn.close()

    @mcp.tool()
    def sql_grounding_check(
        sql: str,
        repo: str | None = None,
        aggregator_context: int | None = None,
    ) -> dict:
        """Pre-flight SQL grounding check: column existence, enum ambiguity, dimension filters.

        Detects:
        - Column missing (including soft-delete column misuse)
        - Enum code ambiguity (Paradigm B: same code, different meaning per aggregator)
        - Missing dimension filter (cross-tenant data leak risk)

        Args:
            sql: SQL statement to check
            repo: Repo alias
            aggregator_context: Current aggregator ID (helps clarify Paradigm B warnings)
        """
        conn = _conn()
        try:
            repo_id = _default_repo(conn, repo)
            if not repo_id:
                return {"error": "No repos indexed."}

            # Need plain conn (not Row factory) for SqlGroundingChecker
            plain_conn = sqlite3.connect(str(db_path))
            checker = SqlGroundingChecker(plain_conn, repo_id)
            result = checker.check(sql, aggregator_context)
            plain_conn.close()

            return {
                "verdict": result.verdict,
                "checks": [
                    {
                        "kind": c.kind,
                        "severity": c.severity,
                        "table": c.table,
                        "column": c.column,
                        "message": c.message,
                        "fix": c.fix,
                    }
                    for c in result.checks
                ],
                "parsed_tables": result.parsed_tables,
                "repo": repo_id,
            }
        finally:
            conn.close()

    return mcp
