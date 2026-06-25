"""Extraction pipeline: orchestrate adapter dispatch + semantic layer + storage."""
import re
from pathlib import Path
from dataclasses import dataclass
from .model import Table, EnumDef, I18nKey
from .adapters.base import AdapterRegistry
from .store import SchemaStore


@dataclass
class ExtractionReport:
    """Report from extraction pipeline."""
    tables: int
    enums: int
    relations: int
    i18n_keys: int
    warnings: list[str]


class ExtractionPipeline:
    """Orchestrates schema extraction from a repo."""

    def __init__(self, repo_root: Path, registry: AdapterRegistry, db_path: Path,
                 repo_alias: str, extractor_ver: str,
                 domain_map: dict[str, str] | None = None):
        self.repo_root = repo_root
        self.registry = registry
        self.db_path = db_path
        self.repo_alias = repo_alias
        self.extractor_ver = extractor_ver
        self.domain_map = domain_map or {}

    def _collect_i18n(self) -> dict[str, I18nKey]:
        """Collect i18n bundles from repo."""
        i18n = {}
        for f in self.repo_root.rglob("messages_zh_CN*.properties"):
            if "/target/" in str(f):
                continue
            try:
                content = f.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            for line in content.splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                k = k.strip()
                v = v.strip()
                # Decode \uXXXX
                try:
                    v = v.encode("raw_unicode_escape").decode("unicode_escape")
                except Exception:
                    pass
                i18n[k] = I18nKey(key=k, value_zh=v, bundle_file=str(f.relative_to(self.repo_root)))
        return i18n

    def _apply_semantic_layer(self, tables: list[Table], enums: list[EnumDef]):
        """Apply ORM-agnostic semantic post-processing."""
        # Soft-delete detection
        SOFT_DELETE = {"deleted", "is_deleted", "del_flag", "is_del", "is_active"}
        for t in tables:
            for c in t.columns:
                if c.name in SOFT_DELETE:
                    t.soft_delete_column = c.name
                    break

        # Dimension field detection
        DIMENSIONS = {"shipper", "shipper_id", "party_id", "seller_party_id", "aggregator", "tenant_id", "org_id"}
        for t in tables:
            t.dimension_fields = [c.name for c in t.columns if c.name in DIMENSIONS]

        # Paradigm B warning injection
        for e in enums:
            if e.paradigm.value == "B":
                for v in e.values:
                    if v.aggregators:
                        v.is_default_meaning = False

        # Enum→column binding (layered confidence)
        # high: column name matches enum short_name + column type is Short/Integer + same domain has enum class
        # declared: ORM annotation (not implemented in tkmybatis)
        # low: weak match (type/weak naming)
        enum_short_names = {e.short_name.lower(): e.enum_class for e in enums}
        for t in tables:
            for c in t.columns:
                if c.enum_class:
                    continue  # Already bound
                # High confidence: column name matches enum short_name (e.g., status→Status?)
                # Actually, "status" doesn't match "OrderStatus", so skip this for now
                # Low confidence: column type is Short/Integer and name contains "status" or "state"
                if c.type_raw in ("Short", "Integer", "short", "int") and ("status" in c.name.lower() or "state" in c.name.lower()):
                    # Check if there's an enum in the same domain
                    # For now, just mark as candidate (don't auto-bind)
                    # c.enum_candidate = ...  # Not implemented in model yet
                    pass

    def _infer_relations_by_name(self, tables: list[Table]) -> list:
        """Infer foreign key relations by naming convention (<table>_id)."""
        from .model import Relation
        relations = []
        table_names = {t.name for t in tables}
        for t in tables:
            for c in t.columns:
                if c.name.endswith("_id") and c.name != "id":
                    # Infer: e.g., order_id → pcl_order.id
                    ref_table = c.name[:-3]  # Remove "_id"
                    # Try common prefixes
                    for prefix in ["pcl_", "act_", "sys_"]:
                        if f"{prefix}{ref_table}" in table_names:
                            relations.append(Relation(
                                from_table=t.name,
                                from_column=c.name,
                                to_table=f"{prefix}{ref_table}",
                                to_column="id",
                                kind="fk",
                                confidence="inferred",
                            ))
                            break
        return relations

    def run(self) -> ExtractionReport:
        """Run extraction pipeline."""
        warnings = []

        # 1. Collect i18n
        i18n = self._collect_i18n()

        # 2. Walk source files + adapter dispatch
        tables: list[Table] = []
        enums: list[EnumDef] = []
        relations = []

        # Get all adapters
        adapters = self.registry.all()
        if not adapters:
            warnings.append("No adapters registered")

        # For each adapter, discover and parse
        for adapter in adapters:
            # Set domain_map if adapter supports it
            if hasattr(adapter, 'domain_map') and self.domain_map:
                adapter.domain_map = self.domain_map

            # Get base entity columns for this adapter
            base_cols_map = adapter.base_entity_resolver
            base_cols = base_cols_map.get("BaseEntity", []) if base_cols_map else []

            # Discover and parse tables
            table_files = adapter.discover_tables(self.repo_root)
            for path in table_files:
                try:
                    src = path.read_text(encoding="utf-8", errors="replace")
                    t = adapter.parse_table(path, src, base_cols, self.repo_root)
                    if t:
                        tables.append(t)
                except Exception as e:
                    warnings.append(f"Failed to parse {path}: {e}")

            # Discover and parse enums
            enum_files = adapter.discover_enums(self.repo_root)
            # Convert i18n dict to simple {key: value_zh} for adapter
            i18n_simple = {k: v.value_zh for k, v in i18n.items()}
            for path in enum_files:
                try:
                    src = path.read_text(encoding="utf-8", errors="replace")
                    e = adapter.parse_enum(path, src, i18n_simple, self.repo_root)
                    if e:
                        enums.append(e)
                except Exception as ex:
                    warnings.append(f"Failed to parse enum {path}: {ex}")

            # Parse relations (usually empty for tkmybatis)
            try:
                rels = adapter.parse_relations(self.repo_root)
                relations.extend(rels)
            except Exception as ex:
                warnings.append(f"Failed to parse relations: {ex}")

        # 3. Semantic layer post-processing
        self._apply_semantic_layer(tables, enums)

        # Deduplicate tables by name (multiple entity classes may map to same table)
        # Keep the one with the most columns (usually the primary entity, not a VO)
        tables_by_name: dict[str, Table] = {}
        for t in tables:
            if t.name not in tables_by_name:
                tables_by_name[t.name] = t
            else:
                existing = tables_by_name[t.name]
                if len(t.columns) > len(existing.columns):
                    tables_by_name[t.name] = t
        tables = list(tables_by_name.values())

        # Deduplicate enums by class name
        # Priority: DIMENSIONAL (B) over SIMPLE (A), then more values wins
        enums_by_class: dict[str, EnumDef] = {}
        for e in enums:
            if e.enum_class not in enums_by_class:
                enums_by_class[e.enum_class] = e
            else:
                existing = enums_by_class[e.enum_class]
                # Prefer paradigm B over A, then more values
                if (e.paradigm.value == "B" and existing.paradigm.value == "A") or \
                   (e.paradigm == existing.paradigm and len(e.values) > len(existing.values)):
                    enums_by_class[e.enum_class] = e
        enums = list(enums_by_class.values())

        relations += self._infer_relations_by_name(tables)

        # 4. Save to SQLite
        store = SchemaStore(self.db_path)
        try:
            store.save_repo(
                repo_id=self.repo_alias,
                repo_root=self.repo_root,
                tables=tables,
                enums=enums,
                i18n_keys=i18n,
                extractor_ver=self.extractor_ver,
                domain_map=self.domain_map,
            )
        finally:
            store.close()

        return ExtractionReport(
            tables=len(tables),
            enums=len(enums),
            relations=len(relations),
            i18n_keys=len(i18n),
            warnings=warnings,
        )
