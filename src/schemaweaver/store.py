"""SQLite storage layer with FTS5 full-text search (jieba pre-tokenization for Chinese)."""
import sqlite3
import json
from pathlib import Path
from datetime import datetime
import jieba
from .model import EnumDef, I18nKey, Table
from .live_sync import LiveTable, normalize_mysql_type, stale_warning


def _tokenize_zh(text: str) -> str:
    """Pre-tokenize Chinese text with jieba, join with spaces for FTS5."""
    if not text:
        return ""
    # jieba.cut returns iterator of words; join with spaces
    words = list(jieba.cut(text))
    return " ".join(words)


class SchemaStore:
    """SQLite storage for extracted schema."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._init_schema()

    def _init_schema(self):
        """Create tables if not exist."""
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS repos (
                repo_id        TEXT PRIMARY KEY,
                root_path      TEXT NOT NULL,
                orm_primary    TEXT,
                extracted_at   TEXT NOT NULL,
                extractor_ver  TEXT NOT NULL,
                table_count    INTEGER,
                enum_count     INTEGER,
                base_entity    TEXT,
                domain_map     TEXT
            );

            CREATE TABLE IF NOT EXISTS tables (
                repo_id        TEXT NOT NULL REFERENCES repos(repo_id) ON DELETE CASCADE,
                name           TEXT NOT NULL,
                entity_class   TEXT,
                source_file    TEXT,
                purpose        TEXT,
                domain         TEXT,
                extends_base   INTEGER DEFAULT 0,
                soft_delete_column TEXT,
                dimension_fields TEXT,
                PRIMARY KEY (repo_id, name)
            );

            CREATE TABLE IF NOT EXISTS columns (
                repo_id        TEXT NOT NULL,
                table_name     TEXT NOT NULL,
                name           TEXT NOT NULL,
                java_field     TEXT,
                type_raw       TEXT,
                type           TEXT,
                is_pk          INTEGER DEFAULT 0,
                is_nullable    INTEGER DEFAULT 1,
                comment        TEXT,
                name_inferred  INTEGER DEFAULT 0,
                from_base_entity INTEGER DEFAULT 0,
                enum_class     TEXT,
                ordinal        INTEGER,
                PRIMARY KEY (repo_id, table_name, name)
            );

            CREATE TABLE IF NOT EXISTS enums (
                repo_id        TEXT NOT NULL,
                enum_class     TEXT NOT NULL,
                short_name     TEXT NOT NULL,
                paradigm       TEXT NOT NULL,
                source_file    TEXT,
                java_type      TEXT DEFAULT 'Short',
                PRIMARY KEY (repo_id, enum_class)
            );

            CREATE TABLE IF NOT EXISTS enum_values (
                repo_id        TEXT NOT NULL,
                enum_class     TEXT NOT NULL,
                code           INTEGER NOT NULL,
                name           TEXT NOT NULL,
                message_key    TEXT,
                i18n_zh        TEXT,
                aggregators    TEXT,
                is_default     INTEGER DEFAULT 1,
                warning        TEXT,
                PRIMARY KEY (repo_id, enum_class, code, aggregators)
            );

            CREATE TABLE IF NOT EXISTS relations (
                repo_id        TEXT NOT NULL,
                from_table     TEXT NOT NULL,
                from_column    TEXT NOT NULL,
                to_table       TEXT NOT NULL,
                to_column      TEXT DEFAULT 'id',
                kind           TEXT,
                confidence     TEXT,
                PRIMARY KEY (repo_id, from_table, from_column, to_table)
            );

            CREATE TABLE IF NOT EXISTS i18n_keys (
                repo_id        TEXT NOT NULL,
                key            TEXT NOT NULL,
                value_zh       TEXT,
                bundle_file    TEXT,
                PRIMARY KEY (repo_id, key)
            );

            CREATE TABLE IF NOT EXISTS table_meta (
                repo_id        TEXT NOT NULL,
                table_name     TEXT NOT NULL,
                kind           TEXT NOT NULL,
                content        TEXT NOT NULL,
                source         TEXT NOT NULL,
                updated_at     TEXT NOT NULL,
                PRIMARY KEY (repo_id, table_name, kind, source)
            );

            CREATE TABLE IF NOT EXISTS drift_reports (
                repo_id        TEXT NOT NULL,
                checked_at     TEXT NOT NULL,
                source         TEXT NOT NULL,
                summary        TEXT,
                report_md      TEXT,
                PRIMARY KEY (repo_id, checked_at, source)
            );

            -- FTS5 virtual tables (unicode61 tokenizer + jieba pre-tokenization)
            -- Standalone mode (no content=): simpler, no rowid sync required
            CREATE VIRTUAL TABLE IF NOT EXISTS tables_fts USING fts5(
                repo_id UNINDEXED, table_name, purpose, domain,
                tokenize='unicode61'
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS columns_fts USING fts5(
                repo_id UNINDEXED, table_name UNINDEXED, col_name, comment, col_type,
                tokenize='unicode61'
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS enums_fts USING fts5(
                repo_id UNINDEXED, short_name, enum_class,
                tokenize='unicode61'
            );

            CREATE INDEX IF NOT EXISTS idx_columns_table ON columns(repo_id, table_name);
            CREATE INDEX IF NOT EXISTS idx_columns_enum  ON columns(enum_class);
            CREATE INDEX IF NOT EXISTS idx_enum_values_class ON enum_values(repo_id, enum_class);
        """)
        self.conn.commit()

        # These columns were added after the original static extractor. Keep
        # existing databases compatible without requiring a destructive rebuild.
        repo_columns = {
            row[1] for row in self.conn.execute("PRAGMA table_info(repos)").fetchall()
        }
        if "schema_source" not in repo_columns:
            self.conn.execute("ALTER TABLE repos ADD COLUMN schema_source TEXT")
        if "schema_environment" not in repo_columns:
            self.conn.execute("ALTER TABLE repos ADD COLUMN schema_environment TEXT")
        self.conn.commit()

    def save_repo(self, repo_id: str, repo_root: Path, tables: list[Table],
                  enums: list[EnumDef], i18n_keys: dict[str, I18nKey],
                  extractor_ver: str, domain_map: dict | None = None):
        """Save extracted schema for a repo (replaces existing)."""
        # Delete existing repo data explicitly (don't rely on CASCADE alone)
        self.conn.execute("DELETE FROM enum_values WHERE repo_id = ?", (repo_id,))
        self.conn.execute("DELETE FROM enums WHERE repo_id = ?", (repo_id,))
        self.conn.execute("DELETE FROM columns WHERE repo_id = ?", (repo_id,))
        self.conn.execute("DELETE FROM relations WHERE repo_id = ?", (repo_id,))
        self.conn.execute("DELETE FROM tables WHERE repo_id = ?", (repo_id,))
        self.conn.execute("DELETE FROM i18n_keys WHERE repo_id = ?", (repo_id,))
        self.conn.execute("DELETE FROM repos WHERE repo_id = ?", (repo_id,))
        self.conn.commit()

        # Insert repo record
        now = datetime.now().isoformat()
        self.conn.execute("""
            INSERT INTO repos (repo_id, root_path, orm_primary, extracted_at, extractor_ver,
                              table_count, enum_count, base_entity, domain_map)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (repo_id, str(repo_root), "tkmybatis", now, extractor_ver,
              len(tables), len(enums), "BaseEntity", json.dumps(domain_map or {})))

        # Insert tables
        for t in tables:
            self.conn.execute("""
                INSERT INTO tables (repo_id, name, entity_class, source_file, purpose, domain,
                                   extends_base, soft_delete_column, dimension_fields)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (repo_id, t.name, t.entity_class, t.source_file, t.purpose, t.domain,
                  1 if t.extends_base_entity else 0, t.soft_delete_column,
                  json.dumps(t.dimension_fields)))

            # Insert columns
            for i, c in enumerate(t.columns, 1):
                self.conn.execute("""
                    INSERT INTO columns (repo_id, table_name, name, java_field, type_raw, type,
                                        is_pk, is_nullable, comment, name_inferred, from_base_entity,
                                        enum_class, ordinal)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (repo_id, t.name, c.name, c.java_field, c.type_raw, c.type.value if c.type else None,
                      1 if c.is_primary_key else 0, 1 if c.is_nullable else 0, c.comment,
                      1 if c.name_inferred else 0, 1 if c.from_base_entity else 0,
                      c.enum_class, i))

        # Insert enums
        for e in enums:
            self.conn.execute("""
                INSERT INTO enums (repo_id, enum_class, short_name, paradigm, source_file, java_type)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (repo_id, e.enum_class, e.short_name, e.paradigm.value, e.source_file, e.java_type))

            for v in e.values:
                self.conn.execute("""
                    INSERT INTO enum_values (repo_id, enum_class, code, name, message_key, i18n_zh,
                                            aggregators, is_default, warning)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (repo_id, e.enum_class, v.code, v.name, v.message_key, v.i18n_zh,
                      json.dumps(v.aggregators) if v.aggregators else None,
                      1 if v.is_default_meaning else 0, v.warning))

        # Insert i18n keys (delete existing first to avoid duplicates)
        self.conn.execute("DELETE FROM i18n_keys WHERE repo_id = ?", (repo_id,))
        for k, v in i18n_keys.items():
            self.conn.execute("""
                INSERT INTO i18n_keys (repo_id, key, value_zh, bundle_file)
                VALUES (?, ?, ?, ?)
            """, (repo_id, v.key, v.value_zh, v.bundle_file))

        self.conn.commit()

        # Rebuild FTS
        self.rebuild_fts(repo_id)

    def sync_live_schema(
        self,
        repo_id: str,
        live_tables: list[LiveTable],
        source_env: str,
    ) -> None:
        """Overlay physical schema from a live database while keeping semantics."""
        repo = self.conn.execute(
            "SELECT root_path, extractor_ver, domain_map FROM repos WHERE repo_id = ?",
            (repo_id,),
        ).fetchone()
        if repo is None:
            raise ValueError(f"Repo '{repo_id}' is not indexed; run extract first")

        domain_map = json.loads(repo[2] or "{}")
        existing_tables = {
            row["name"]: row
            for row in self.conn.execute(
                "SELECT * FROM tables WHERE repo_id = ?", (repo_id,)
            ).fetchall()
        }
        live_names = {table.name for table in live_tables}

        for table in live_tables:
            existing = existing_tables.get(table.name)
            if existing is None:
                domain = _infer_domain(table.name, domain_map)
                dimension_fields = [
                    column.name
                    for column in table.columns
                    if column.name in {
                        "shipper", "shipper_id", "party_id", "seller_party_id",
                        "aggregator", "tenant_id", "org_id",
                    }
                ]
                self.conn.execute(
                    """
                    INSERT INTO tables (
                        repo_id, name, entity_class, source_file, purpose, domain,
                        extends_base, soft_delete_column, dimension_fields
                    ) VALUES (?, ?, '', '', ?, ?, 0, ?, ?)
                    """,
                    (
                        repo_id,
                        table.name,
                        table.comment,
                        domain,
                        _find_soft_delete(table),
                        json.dumps(dimension_fields),
                    ),
                )
            else:
                purpose = existing["purpose"] or table.comment
                domain = existing["domain"] or _infer_domain(table.name, domain_map)
                self.conn.execute(
                    "UPDATE tables SET purpose = ?, domain = ? WHERE repo_id = ? AND name = ?",
                    (purpose, domain, repo_id, table.name),
                )

            old_columns = {
                row["name"]: row
                for row in self.conn.execute(
                    "SELECT * FROM columns WHERE repo_id = ? AND table_name = ?",
                    (repo_id, table.name),
                ).fetchall()
            }
            self.conn.execute(
                "DELETE FROM columns WHERE repo_id = ? AND table_name = ?",
                (repo_id, table.name),
            )
            for live_column in table.columns:
                old = old_columns.get(live_column.name)
                self.conn.execute(
                    """
                    INSERT INTO columns (
                        repo_id, table_name, name, java_field, type_raw, type,
                        is_pk, is_nullable, comment, name_inferred, from_base_entity,
                        enum_class, ordinal
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        repo_id,
                        table.name,
                        live_column.name,
                        old["java_field"] if old else live_column.name,
                        live_column.column_type,
                        normalize_mysql_type(live_column.data_type).value,
                        int(live_column.is_primary_key),
                        int(live_column.is_nullable),
                        live_column.comment or (old["comment"] if old else ""),
                        old["name_inferred"] if old else 0,
                        old["from_base_entity"] if old else 0,
                        old["enum_class"] if old else None,
                        live_column.ordinal,
                    ),
                )

            self.conn.execute(
                """
                UPDATE tables
                SET soft_delete_column = ?, dimension_fields = ?
                WHERE repo_id = ? AND name = ?
                """,
                (
                    existing["soft_delete_column"] if existing and existing["soft_delete_column"] in {c.name for c in table.columns} else _find_soft_delete(table),
                    json.dumps(
                        json.loads(existing["dimension_fields"] or "[]")
                        if existing and json.loads(existing["dimension_fields"] or "[]")
                        else [c.name for c in table.columns if c.name in {"shipper", "shipper_id", "party_id", "seller_party_id", "aggregator", "tenant_id", "org_id"}]
                    ),
                    repo_id,
                    table.name,
                ),
            )

        # A live sync is authoritative for physical tables: remove tables that
        # disappeared from the target database, but keep curated table_meta.
        for name in set(existing_tables) - live_names:
            self.conn.execute(
                "DELETE FROM tables WHERE repo_id = ? AND name = ?", (repo_id, name)
            )

        now = datetime.now().isoformat()
        self.conn.execute(
            """
            UPDATE repos
            SET extracted_at = ?, table_count = ?, schema_source = ?, schema_environment = ?
            WHERE repo_id = ?
            """,
            (now, len(live_tables), "mysql_information_schema", source_env, repo_id),
        )
        self.conn.commit()
        self.rebuild_fts(repo_id)

    def repo_stale_warning(self, repo_id: str, stale_days: int = 7):
        row = self.conn.execute(
            "SELECT extracted_at FROM repos WHERE repo_id = ?", (repo_id,)
        ).fetchone()
        return stale_warning(row[0], stale_days) if row else None

    def rebuild_fts(self, repo_id: str | None = None):
        """Rebuild FTS5 indexes without dropping entries for other repos."""
        # Drop and recreate FTS tables (simpler than DELETE which has complex FTS5 syntax)
        self.conn.executescript("""
            DROP TABLE IF EXISTS tables_fts;
            DROP TABLE IF EXISTS columns_fts;
            DROP TABLE IF EXISTS enums_fts;

            CREATE VIRTUAL TABLE tables_fts USING fts5(
                repo_id UNINDEXED, table_name, purpose, domain,
                tokenize='unicode61'
            );

            CREATE VIRTUAL TABLE columns_fts USING fts5(
                repo_id UNINDEXED, table_name UNINDEXED, col_name, comment, col_type,
                tokenize='unicode61'
            );

            CREATE VIRTUAL TABLE enums_fts USING fts5(
                repo_id UNINDEXED, short_name, enum_class,
                tokenize='unicode61'
            );
        """)

        # Populate tables_fts (pre-tokenize Chinese). Rebuild all repos because
        # the standalone FTS tables are shared by every repo_id.
        for row in self.conn.execute("""
            SELECT repo_id, name, purpose, domain FROM tables
            ORDER BY repo_id, name
        """):
            current_repo, name, purpose, domain = row
            purpose_tok = _tokenize_zh(purpose)
            self.conn.execute("""
                INSERT INTO tables_fts (repo_id, table_name, purpose, domain)
                VALUES (?, ?, ?, ?)
            """, (current_repo, name, purpose_tok, domain))

        # Populate columns_fts (pre-tokenize Chinese comments)
        for row in self.conn.execute("""
            SELECT repo_id, table_name, name, comment, type FROM columns
            ORDER BY repo_id, table_name, ordinal
        """):
            current_repo, table_name, col_name, comment, col_type = row
            comment_tok = _tokenize_zh(comment)
            self.conn.execute("""
                INSERT INTO columns_fts (repo_id, table_name, col_name, comment, col_type)
                VALUES (?, ?, ?, ?, ?)
            """, (current_repo, table_name, col_name, comment_tok, col_type))

        # Populate enums_fts
        for row in self.conn.execute("""
            SELECT repo_id, short_name, enum_class FROM enums
            ORDER BY repo_id, enum_class
        """):
            current_repo, short_name, enum_class = row
            self.conn.execute("""
                INSERT INTO enums_fts (repo_id, short_name, enum_class)
                VALUES (?, ?, ?)
            """, (current_repo, short_name, enum_class))

        self.conn.commit()

    def close(self):
        self.conn.close()


def _infer_domain(table_name: str, domain_map: dict[str, str]) -> str:
    for prefix, domain in sorted(domain_map.items(), key=lambda item: -len(item[0])):
        if table_name.startswith(prefix):
            return domain
    return ""


def _find_soft_delete(table: LiveTable) -> str | None:
    names = {"deleted", "is_deleted", "del_flag", "is_del", "is_active"}
    return next((column.name for column in table.columns if column.name in names), None)
