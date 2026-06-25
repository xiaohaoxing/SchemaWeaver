# SchemaWeaver

**Static ORM-annotation → DB schema + business-semantics MCP server**

SchemaWeaver extracts database schema and business semantics from source code ORM annotations — offline, without connecting to a running database — and exposes them as an [MCP](https://modelcontextprotocol.io) server for AI agents.

## Why SchemaWeaver?

Every existing schema MCP server connects to a live database to fetch DDL. That tells you column names and types, but nothing about:

- What does `status = 133` mean? (Answer: "warehoused" for most shippers, but "ordered" for shipper `18325`)
- Does this table have a soft-delete column? (Answer: only 14 out of 517 tables do — and the other 503 will throw an error if you `WHERE deleted = 0`)
- Which dimension field must I always filter by to avoid cross-tenant data leaks?
- What's the relationship between `pcl_box.order_id` and `pcl_order.id`?

This is **business semantics**. It lives in source code — enum classes, `@ApiModelProperty` annotations, i18n bundles — not in database DDL. SchemaWeaver mines it statically.

## How It Works

```
Source code repo
  @Table/@Column/@ApiModelProperty   ──► Extraction pipeline
  Enum dict classes (code→meaning)    ──► SQLite + FTS5
  i18n bundles (messageKey→Chinese)  ──►   (jieba pre-tokenization)
                                              │
                                              ▼
                                         MCP server
                                     schema_search
                                     table_detail
                                     enum_resolve
                                     sql_grounding_check
```

## Quick Start

```bash
# Install
uv tool install schemaweaver

# Extract schema from your repo
schemaweaver extract /path/to/your/repo --alias myapp

# Start MCP server
schemaweaver mcp
```

Configure in Claude Code `.mcp.json`:

```json
{
  "mcpServers": {
    "schemaweaver": {
      "command": "schemaweaver",
      "args": ["mcp"]
    }
  }
}
```

Or in `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "schemaweaver": {
      "command": "schemaweaver",
      "args": ["mcp", "--db", "/path/to/schemaweaver.db"]
    }
  }
}
```

## CLI Reference

```bash
schemaweaver extract <repo_root> --alias <name>   # Extract schema (like gitnexus analyze)
schemaweaver mcp [--db <path>]                     # Start MCP server (stdio)
schemaweaver list                                   # List indexed repos
schemaweaver status <alias>                         # Show repo stats
schemaweaver remove <alias>                         # Remove indexed repo
```

## MCP Tools

| Tool | Description |
|------|-------------|
| `schema_search` | Natural language → candidate tables (FTS5 BM25, Chinese-aware) |
| `table_detail` | Single table: all columns + enum codes + relations + known traps |
| `enum_resolve` | Enum class or `table.column` → all `code → meaning` entries, including Paradigm B aggregator divergence |
| `sql_grounding_check` | Pre-flight SQL check: missing columns, invalid enum literals, missing dimension filters, `deleted` column misuse |
| `drift_check` | Compare extracted schema vs live DB `information_schema` *(Phase 2)* |
| `repo_diff` | Compare two repos' schemas (e.g. SaaS vs a custom fork) *(Phase 2)* |

### What `enum_resolve` catches

SchemaWeaver handles the hard case where the **same enum code has different meanings depending on a dimension value** (Paradigm B):

```
enum_resolve(table="pcl_order", column="status")
→ code=133  default  → "warehoused"
→ code=133  aggregator=[18325]  → "ordered"  ⚠ divergence warning
```

### What `sql_grounding_check` catches

```sql
SELECT status FROM pcl_order WHERE status = 133 AND deleted = 0
```

```
✗ FAIL
  - column_missing: column `deleted` does not exist in `pcl_order`
    (this table has no soft-delete column; use status=400 for "abandoned")
  - enum_ambiguous: status=133 means "warehoused" by default,
    but "ordered" for aggregator=18325 — add dimension filter
  - dimension_missing: pcl_order has dimension fields [shipper, aggregator];
    query has no dimension filter — may return cross-tenant data
```

## Supported ORMs

| Adapter | Language | Markers | Status |
|---------|----------|---------|--------|
| `tkmybatis` | Java | `@Table(` `javax.persistence` | ✅ v0.1 |
| `mybatis-plus` | Java | `@TableName(` `@TableField` | 🔲 v0.3 |
| `jpa` | Java | `@Entity` `@OneToMany` | 🔲 v0.3 |
| `sqlalchemy` | Python | `Column(` `__tablename__` | 🔲 v0.3 |
| `django` | Python | `models.CharField` `class Meta` | 🔲 v0.3 |
| `gorm` | Go | `gorm:"column:` | 🔲 v0.3 |
| `typeorm` / `prisma` | TypeScript | `@Entity` / `model ` | 🔲 v0.3 |

Want to add an ORM? See [CONTRIBUTING.md](CONTRIBUTING.md).

## vs. Live-Database Schema MCPs

| Feature | Live DB MCP | SchemaWeaver |
|---------|-------------|--------------|
| Column names & types | ✅ | ✅ |
| Enum code meanings | ❌ | ✅ |
| Same-code/different-aggregator divergence | ❌ | ✅ |
| Chinese column comments | ❌ | ✅ (from @ApiModelProperty) |
| Soft-delete column detection | ❌ | ✅ |
| Dimension / tenant filter fields | ❌ | ✅ |
| FK relations | ❌ | ✅ (inferred + declared) |
| Known query traps / templates | ❌ | ✅ (human-curated `table_meta`) |
| Requires DB connection | ✅ | ❌ |
| Works with offline / read-only repos | ❌ | ✅ |

## Multi-Repo Support

SchemaWeaver stores all repos in a single SQLite DB (`~/.schemaweaver/schemaweaver.db`) with `repo_id` isolation:

```bash
schemaweaver extract /repos/saas    --alias saas
schemaweaver extract /repos/fork-jp --alias fork-jp
schemaweaver list
# saas:    517 tables, 67 enums
# fork-jp: 504 tables, 55 enums
```

The `repo_diff` tool (v0.2) shows what changed between any two indexed repos.

## Architecture

```
~/.schemaweaver/schemaweaver.db
  repos           — indexed repo registry
  tables          — one row per physical table
  columns         — one row per column (with comment, type, enum binding)
  enums           — enum class definitions
  enum_values     — code→meaning, one row per (enum, code, aggregators)
  relations       — FK relations (declared or naming-inferred)
  i18n_keys       — messageKey→Chinese
  table_meta      — human-curated traps, query templates, notes
  drift_reports   — schema drift history
  tables_fts      — FTS5 full-text index (jieba pre-tokenization)
  columns_fts     — FTS5 column search
  enums_fts       — FTS5 enum search
```

Enum Paradigm B (same code, different meaning per aggregator):
```
enum_values(enum_class, code=133, aggregators=NULL,     i18n_zh="warehoused", is_default=1)
enum_values(enum_class, code=133, aggregators=[18325],  i18n_zh="ordered",    is_default=0, warning=...)
```

## Development

```bash
git clone https://github.com/xiaohaoxing/schemaweaver
cd schemaweaver
uv sync --extra dev
uv run pytest
uv run ruff check src/
```

## License

Apache-2.0 — see [LICENSE](LICENSE).
