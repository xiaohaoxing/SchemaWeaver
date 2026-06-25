"""SQL grounding check using sqlglot for schema-aware pre-flight validation."""
import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any

try:
    import sqlglot
    import sqlglot.expressions as exp
    HAS_SQLGLOT = True
except ImportError:
    HAS_SQLGLOT = False


@dataclass
class CheckItem:
    kind: str          # column_missing / soft_delete_misuse / enum_ambiguous / dimension_missing
    severity: str      # error / warn
    table: str = ""
    column: str = ""
    message: str = ""
    fix: str = ""


@dataclass
class GroundingResult:
    verdict: str              # PASS / WARN / FAIL
    checks: list[CheckItem] = field(default_factory=list)
    parsed_tables: list[str] = field(default_factory=list)
    parsed_columns: list[tuple[str, str]] = field(default_factory=list)  # (table, col)


class SqlGroundingChecker:
    """Pre-flight SQL checker against SchemaWeaver SQLite store."""

    def __init__(self, conn: sqlite3.Connection, repo_id: str):
        self.conn = conn
        self.repo_id = repo_id

    def _get_table_meta(self, table_name: str) -> dict | None:
        row = self.conn.execute("""
            SELECT soft_delete_column, dimension_fields FROM tables
            WHERE repo_id = ? AND name = ?
        """, (self.repo_id, table_name)).fetchone()
        if not row:
            return None
        return {
            "soft_delete_column": row[0],
            "dimension_fields": json.loads(row[1]) if row[1] else [],
        }

    def _column_exists(self, table_name: str, col_name: str) -> bool:
        row = self.conn.execute("""
            SELECT 1 FROM columns WHERE repo_id = ? AND table_name = ? AND name = ?
        """, (self.repo_id, table_name, col_name)).fetchone()
        return row is not None

    def _get_enum_values(self, table_name: str, col_name: str) -> list[dict]:
        """Get enum values for a column (via column→enum binding)."""
        row = self.conn.execute("""
            SELECT enum_class FROM columns
            WHERE repo_id = ? AND table_name = ? AND name = ?
        """, (self.repo_id, table_name, col_name)).fetchone()
        if not row or not row[0]:
            return []
        enum_class = row[0]
        rows = self.conn.execute("""
            SELECT code, i18n_zh, aggregators, warning, is_default FROM enum_values
            WHERE repo_id = ? AND enum_class = ?
        """, (self.repo_id, enum_class)).fetchall()
        return [{"code": r[0], "i18n_zh": r[1], "aggregators": json.loads(r[2]) if r[2] else None,
                 "warning": r[3], "is_default": r[4]} for r in rows]

    def _get_paradigm(self, table_name: str, col_name: str) -> str | None:
        row = self.conn.execute("""
            SELECT e.paradigm FROM columns c
            JOIN enums e ON e.enum_class = c.enum_class AND e.repo_id = c.repo_id
            WHERE c.repo_id = ? AND c.table_name = ? AND c.name = ?
        """, (self.repo_id, table_name, col_name)).fetchone()
        return row[0] if row else None

    def check(self, sql: str, aggregator_context: int | None = None) -> GroundingResult:
        """Run grounding checks on a SQL string."""
        if not HAS_SQLGLOT:
            return GroundingResult(
                verdict="WARN",
                checks=[CheckItem(kind="sqlglot_missing", severity="warn",
                                  message="sqlglot not installed; SQL parsing skipped")],
            )

        checks: list[CheckItem] = []
        parsed_tables: list[str] = []
        parsed_columns: list[tuple[str, str]] = []

        try:
            parsed = sqlglot.parse_one(sql, dialect="mysql")
        except Exception as e:
            return GroundingResult(
                verdict="WARN",
                checks=[CheckItem(kind="parse_error", severity="warn",
                                  message=f"SQL parse failed: {e}")],
            )

        # 1. Extract referenced tables
        for table_node in parsed.find_all(exp.Table):
            tname = table_node.name.lower()
            if tname and tname not in parsed_tables:
                parsed_tables.append(tname)

        # 2. Extract referenced columns (with table context where available)
        where_eq_pairs: dict[str, list[Any]] = {}  # col_name → [values]
        for col_node in parsed.find_all(exp.Column):
            col_name = col_node.name.lower()
            table_name = (col_node.table or "").lower()
            if col_name:
                parsed_columns.append((table_name, col_name))

        # 3. Extract WHERE equality literals for enum checking
        for eq_node in parsed.find_all(exp.EQ):
            left = eq_node.left
            right = eq_node.right
            if isinstance(left, exp.Column) and isinstance(right, exp.Literal):
                col_name = left.name.lower()
                table_name = (left.table or "").lower()
                try:
                    val = int(right.this) if right.is_number else right.this
                except Exception:
                    val = right.this
                key = f"{table_name}.{col_name}" if table_name else col_name
                where_eq_pairs.setdefault(key, []).append(val)

        # Resolve table for each column (single table query)
        def resolve_table(col_name: str, table_hint: str) -> str | None:
            if table_hint and table_hint in parsed_tables:
                return table_hint
            if len(parsed_tables) == 1:
                return parsed_tables[0]
            # Try finding in any of the referenced tables
            for t in parsed_tables:
                if self._column_exists(t, col_name):
                    return t
            return None

        # Per-table checks
        checked_dims: set[str] = set()
        for table_name in parsed_tables:
            meta = self._get_table_meta(table_name)
            if meta is None:
                continue  # Unknown table, skip

            # 3a. Check each referenced column exists
            for (t_hint, col_name) in parsed_columns:
                resolved = resolve_table(col_name, t_hint)
                if resolved != table_name:
                    continue
                if col_name in ("*",):
                    continue
                if not self._column_exists(table_name, col_name):
                    # Special case: soft-delete misuse
                    if col_name in ("deleted", "is_deleted", "del_flag"):
                        soft = meta["soft_delete_column"]
                        checks.append(CheckItem(
                            kind="soft_delete_misuse",
                            severity="error",
                            table=table_name,
                            column=col_name,
                            message=f"列 `{col_name}` 在 `{table_name}` 不存在；"
                                    f"该表{'有' if soft else '无'}软删除列{'（' + soft + '）' if soft else ''}。"
                                    f"废弃语义通常用 status=400（废弃）表达。",
                            fix=f"去掉 WHERE {col_name}=0，改用 AND status != 400 过滤废弃记录",
                        ))
                    else:
                        checks.append(CheckItem(
                            kind="column_missing",
                            severity="error",
                            table=table_name,
                            column=col_name,
                            message=f"列 `{col_name}` 在表 `{table_name}` 中不存在",
                            fix=f"检查列名拼写，或用 table_detail 查询该表的完整列列表",
                        ))

            # 3b. Enum value ambiguity check (Paradigm B)
            for eq_key, values in where_eq_pairs.items():
                if "." in eq_key:
                    t_hint, col_name = eq_key.split(".", 1)
                else:
                    t_hint, col_name = "", eq_key
                resolved = resolve_table(col_name, t_hint)
                if resolved != table_name:
                    continue
                paradigm = self._get_paradigm(table_name, col_name)
                if paradigm != "B":
                    continue
                enum_vals = self._get_enum_values(table_name, col_name)
                if not enum_vals:
                    continue
                for val in values:
                    try:
                        code = int(val)
                    except (TypeError, ValueError):
                        continue
                    # Check if this code has aggregator divergence
                    divergent = [v for v in enum_vals if v["code"] == code and v["aggregators"]]
                    if divergent:
                        default = next((v for v in enum_vals if v["code"] == code and v["is_default"]), None)
                        for d in divergent:
                            aggs = d["aggregators"]
                            checks.append(CheckItem(
                                kind="enum_ambiguous",
                                severity="warn",
                                table=table_name,
                                column=col_name,
                                message=(
                                    f"`{table_name}.{col_name}={code}` 存在语义分化："
                                    f" 默认含义=「{default['i18n_zh'] if default else '?'}」，"
                                    f" aggregator={aggs} 时=「{d['i18n_zh']}」。"
                                    f" WHERE 未指定 aggregator，结果可能混合两种语义。"
                                    + (f" aggregator_context={aggregator_context} 时含义=「"
                                       f"{'已明确' if aggregator_context in (aggs or []) else default['i18n_zh'] if default else '?'}」"
                                       if aggregator_context else "")
                                ),
                                fix=(
                                    f"加 AND aggregator = {aggs[0]} 查「{d['i18n_zh']}」，"
                                    f"或 AND aggregator != {aggs[0]} 查「{default['i18n_zh'] if default else '默认'}」"
                                ),
                            ))

            # 3c. Dimension filter missing
            if table_name not in checked_dims:
                checked_dims.add(table_name)
                dims = meta.get("dimension_fields", [])
                if dims:
                    # Check if any dimension field appears in WHERE
                    where_cols = {c for (_, c) in parsed_columns}
                    missing_dims = [d for d in dims if d not in where_cols]
                    if missing_dims and len(where_cols) > 0:
                        checks.append(CheckItem(
                            kind="dimension_missing",
                            severity="warn",
                            table=table_name,
                            message=(
                                f"`{table_name}` 有维度字段 {dims}，"
                                f"但 WHERE 中未出现任何维度过滤，可能跨客户混查。"
                            ),
                            fix=f"加 AND {missing_dims[0]} = <your_value> 限制查询范围",
                        ))

        # Determine verdict
        if any(c.severity == "error" for c in checks):
            verdict = "FAIL"
        elif checks:
            verdict = "WARN"
        else:
            verdict = "PASS"

        return GroundingResult(
            verdict=verdict,
            checks=checks,
            parsed_tables=parsed_tables,
            parsed_columns=list(set(parsed_columns)),
        )
