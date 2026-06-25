# Changelog

All notable changes to SchemaWeaver are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## [0.1.0] - 2026-06-25

### Added
- Initial MVP release
- `tkmybatis` adapter for Java `javax.persistence` `@Table`/`@Column` annotations
- Support for `@ApiModelProperty` / `@ApiModel` Chinese semantic annotations
- Paradigm A/B enum parsing (Paradigm B: same code, different meaning per aggregator)
- i18n bundle parsing (`messages_zh_CN.properties`) with `\uXXXX` decode
- BaseEntity public field auto-merge (13 fields: id, siteId, createdBy, ..., aggregator)
- Soft-delete column auto-detection (`deleted`, `is_deleted`, `del_flag`, `is_del`)
- Dimension field auto-detection (`shipper`, `aggregator`, `party_id`, ...)
- FK relation inference by naming convention (`<table>_id` → `<table>.id`)
- SQLite storage with FTS5 full-text search (unicode61 + jieba pre-tokenization for Chinese)
- CLI: `schemaweaver extract / list / status / remove`
- Multi-repo support via `repo_id` isolation in a single SQLite DB
- Table and enum deduplication (multi-entity-per-table pattern)
