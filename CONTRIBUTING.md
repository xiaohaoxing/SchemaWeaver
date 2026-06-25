# Contributing to SchemaWeaver

Thank you for your interest in contributing!

## Quick Start

```bash
git clone https://github.com/xiaohaoxing/schemaweaver
cd schemaweaver
uv sync --extra dev
uv run pytest
```

## Project Structure

```
src/schemaweaver/
├── model.py          # ORM-agnostic data model (Table/Column/EnumDef...)
├── adapters/
│   ├── base.py       # BaseAdapter ABC + AdapterRegistry
│   └── tkmybatis.py  # Java tkmybatis adapter
├── pipeline.py       # Extraction pipeline
├── store.py          # SQLite + FTS5 storage
├── mcp_server.py     # MCP server (coming in v0.2)
└── cli.py            # Click CLI
tests/
├── test_tkmybatis.py
├── test_store_fts.py
└── test_sql_check.py
```

## Writing a New Adapter

1. Create `src/schemaweaver/adapters/<id>.py`
2. Subclass `BaseAdapter` and implement all abstract methods:
   - `supports_file(path, src)` — fast marker check
   - `discover_tables(repo_root)` — return entity file list
   - `parse_table(path, src, base_entity_cols, repo_root)` → `Table | None`
   - `discover_enums(repo_root)` — return enum file list
   - `parse_enum(path, src, i18n, repo_root)` → `EnumDef | None`
3. Register in `pyproject.toml`:
   ```toml
   [project.entry-points."schemaweaver.adapters"]
   my_orm = "schemaweaver.adapters.my_orm:MyOrmAdapter"
   ```
4. Add tests under `tests/adapters/test_<id>.py`

### Adapter Guidelines

- Return `None` from `parse_table`/`parse_enum` for non-entity files — never raise.
- Log parse failures as warnings, don't abort the pipeline.
- Use only stdlib + project core deps. Heavy deps (AST parsers, tree-sitter) go in `extras_require`.
- Set `id`, `language`, `file_globs`, `orm_markers` as class attributes.

## Running Tests

```bash
uv run pytest                  # all tests
uv run pytest tests/test_tkmybatis.py -v
uv run pytest --cov=schemaweaver
```

## Code Style

```bash
uv run ruff check src/
uv run ruff format src/
```

## Pull Request Guidelines

- One feature/fix per PR.
- Include tests for new adapters or bug fixes.
- Update `CHANGELOG.md` under `[Unreleased]`.
- Keep `README.md` ORM support table updated.
