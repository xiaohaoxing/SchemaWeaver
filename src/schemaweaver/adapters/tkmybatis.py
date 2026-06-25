"""TkMybatis adapter for Java javax.persistence @Table/@Column annotations."""
import re
from pathlib import Path
from ..model import Table, Column, EnumDef, EnumValue, EnumParadigm, ColType
from .base import BaseAdapter


# BaseEntity public fields (precise from BaseEntity.java source)
BASE_ENTITY_COLS = [
    {"name": "id",               "javaField": "id",              "type": "Long",    "isId": True,  "comment": "主键id"},
    {"name": "site_id",          "javaField": "siteId",          "type": "Integer", "isId": False, "comment": "区域id"},
    {"name": "created_by",       "javaField": "createdBy",       "type": "Integer", "isId": False, "comment": "创建人ID"},
    {"name": "created_name",     "javaField": "createdName",     "type": "String",  "isId": False, "comment": "创建人"},
    {"name": "created_account",  "javaField": "createdAccount",  "type": "String",  "isId": False, "comment": "创建人账户"},
    {"name": "date_created",     "javaField": "dateCreated",     "type": "Date",    "isId": False, "comment": "新增时间"},
    {"name": "modified_by",      "javaField": "modifiedBy",      "type": "Integer", "isId": False, "comment": "更新人ID"},
    {"name": "modified_name",    "javaField": "modifiedName",    "type": "String",  "isId": False, "comment": "更新人"},
    {"name": "modified_account", "javaField": "modifiedAccount", "type": "String",  "isId": False, "comment": "更新人账户"},
    {"name": "date_modified",    "javaField": "dateModified",    "type": "Date",    "isId": False, "comment": "更新时间"},
    {"name": "aggregator",       "javaField": "aggregator",      "type": "Integer", "isId": False, "comment": "集成商ID"},
    {"name": "aggregator_name",  "javaField": "aggregatorName",  "type": "String",  "isId": False, "comment": "集成商名称"},
    {"name": "operation",        "javaField": "operation",       "type": "String",  "isId": False, "comment": "数据来源(URL)"},
]

# Soft-delete column names
SOFT_DELETE_NAMES = {"deleted", "is_deleted", "del_flag", "is_del"}

# Dimension field names
DIMENSION_NAMES = {"shipper", "shipper_id", "party_id", "seller_party_id", "aggregator"}


def _camel_to_snake(name):
    s1 = re.sub(r'(.)([A-Z][a-z]+)', r'\1_\2', name)
    return re.sub(r'([a-z0-9])([A-Z])', r'\1_\2', s1).lower()


def _normalize_type(java_type: str) -> ColType:
    """Map Java type string to normalized ColType."""
    t = java_type.lower()
    if t in ("string", "varchar", "char"):
        return ColType.STRING
    elif t in ("integer", "int"):
        return ColType.INTEGER
    elif t in ("long",):
        return ColType.LONG
    elif t in ("bigdecimal", "double", "float"):
        return ColType.DECIMAL
    elif t in ("boolean", "short"):
        return ColType.BOOLEAN if t == "boolean" else ColType.INTEGER
    elif t in ("date",):
        return ColType.DATE
    elif t in ("datetime", "timestamp", "localdatetime"):
        return ColType.DATETIME
    elif t in ("text", "clob"):
        return ColType.TEXT
    elif t in ("byte[]", "blob"):
        return ColType.BLOB
    else:
        return ColType.UNKNOWN


class TkMybatisAdapter(BaseAdapter):
    """Adapter for Java tkmybatis (javax.persistence @Table/@Column)."""

    id = "tkmybatis"
    language = "java"
    file_globs = ["**/*.java"]
    orm_markers = ["@Table(", "javax.persistence"]

    def __init__(self, domain_map: dict[str, str] | None = None):
        """Initialize with optional domain prefix map."""
        self.domain_map = domain_map or {
            "pcl_packet_": "01-pcl", "pcl_": "01-pcl",
            "act_": "02-act",
            "sys_": "03-sys", "dmn_": "03-sys", "member_": "03-sys", "cms_": "03-sys",
            "tz_": "04-tz", "king_dee_": "04-tz",
            "eto_": "05-eto", "eto_kr_": "05-eto", "wms_": "05-eto",
            "trk_": "06-trk", "shopify_": "06-trk", "amazon_": "06-trk",
            "temu_": "06-trk", "oms_": "06-trk", "b2c_": "06-trk",
        }

    def supports_file(self, path: Path, src: str) -> bool:
        """Check if file is a Java entity with @Table annotation."""
        return path.suffix == ".java" and "@Table(" in src and "javax.persistence" in src

    def discover_tables(self, repo_root: Path) -> list[Path]:
        """Find all Java entity files with @Table annotation."""
        entities = []
        for java_path in repo_root.rglob("*.java"):
            if "/target/" in str(java_path):
                continue
            try:
                src = java_path.read_text(encoding="utf-8", errors="replace")
                if self.supports_file(java_path, src):
                    entities.append(java_path)
            except Exception:
                continue
        return entities

    def parse_table(self, path: Path, src: str, base_entity_cols: list[dict] | None, repo_root: Path | None = None) -> Table | None:
        """Parse single Java entity file → Table."""
        # Must have @Table(name = "...")
        table_m = re.search(r'@Table\s*\(\s*name\s*=\s*"([^"]+)"', src)
        if not table_m:
            return None
        table_name = table_m.group(1).lower()

        # Purpose: @ApiModel("中文") first, else javadoc first line, else class name
        purpose = ""
        api_model_m = re.search(r'@ApiModel\s*\(\s*"([^"]+)"', src)
        if api_model_m:
            purpose = api_model_m.group(1)
        else:
            javadoc_m = re.search(r'/\*\*\s*\n\s*\*\s*([^\n*]+)', src)
            if javadoc_m:
                purpose = javadoc_m.group(1).strip()

        extends_base = bool(re.search(r'class\s+\w+\s+extends\s+BaseEntity', src))

        # Fully qualified class name
        pkg_m = re.search(r'^package\s+([\w.]+)\s*;', src, re.MULTILINE)
        cls_m = re.search(r'(?:public\s+)?class\s+(\w+)', src)
        entity_class = f"{pkg_m.group(1)}.{cls_m.group(1)}" if pkg_m and cls_m else path.stem

        # Extract columns
        cols = []
        # Strategy: scan all @Column annotation blocks, find private field below
        col_pattern = re.compile(
            r'@Column\s*\([^)]*name\s*=\s*"([^"]+)"[^)]*\)'
            r'(?:\s*@\w+[^;{]*?)*?'
            r'\s*private\s+([\w<>, \[\]]+?)\s+(\w+)\s*;',
            re.DOTALL
        )
        for m in col_pattern.finditer(src):
            col_name = m.group(1).lower()
            java_type = m.group(2).strip().split("<")[0].strip()
            java_field = m.group(3)
            # Find @ApiModelProperty comment (look backwards)
            field_comment = ""
            field_start = m.start()
            pre_block = src[max(0, field_start - 300):field_start]
            api_prop_matches = re.findall(r'@ApiModelProperty\s*\(\s*"([^"]+)"', pre_block)
            if api_prop_matches:
                field_comment = api_prop_matches[-1]
            cols.append(Column(
                name=col_name,
                java_field=java_field,
                type_raw=java_type,
                type=_normalize_type(java_type),
                is_primary_key=False,
                is_nullable=True,
                comment=field_comment,
                name_inferred=False,
                from_base_entity=False,
            ))

        # Fields without @Column but with @ApiModelProperty: infer column name from camelCase→snake_case
        inferred_pattern = re.compile(
            r'@ApiModelProperty\s*\(\s*"([^"]+)"\s*\)\s*'
            r'(?:@(?!Column)\w+[^;{]*?\s*)*'
            r'private\s+([\w<>, \[\]]+?)\s+(\w+)\s*;',
            re.DOTALL
        )
        known_fields = {c.java_field for c in cols}
        for m in inferred_pattern.finditer(src):
            field_comment = m.group(1)
            java_type = m.group(2).strip().split("<")[0].strip()
            java_field = m.group(3)
            if java_field in known_fields or java_field in ("serialVersionUID",):
                continue
            inferred_name = _camel_to_snake(java_field)
            cols.append(Column(
                name=inferred_name,
                java_field=java_field,
                type_raw=java_type,
                type=_normalize_type(java_type),
                is_primary_key=False,
                is_nullable=True,
                comment=field_comment,
                name_inferred=True,
                from_base_entity=False,
            ))
            known_fields.add(java_field)

        # @Id field
        id_pattern = re.compile(r'@Id\b.*?private\s+\w+\s+(\w+)\s*;', re.DOTALL)
        for m in id_pattern.finditer(src):
            for c in cols:
                if c.java_field == m.group(1):
                    c.is_primary_key = True

        # Merge BaseEntity public fields (deduplicate by column name)
        if extends_base and base_entity_cols:
            # Get column names already defined in the table
            existing_col_names = {c.name for c in cols}
            # Only add BaseEntity columns that aren't already defined
            base_cols_to_add = [
                Column(
                    name=bc["name"], java_field=bc["javaField"], type_raw=bc["type"],
                    type=_normalize_type(bc["type"]), is_primary_key=bc["isId"],
                    is_nullable=False if bc["isId"] else True, comment=bc["comment"],
                    name_inferred=False, from_base_entity=True,
                )
                for bc in base_entity_cols
                if bc["name"] not in existing_col_names
            ]
            all_cols = base_cols_to_add + cols
        else:
            all_cols = cols

        # Detect soft-delete column
        soft_delete_col = None
        for c in all_cols:
            if c.name in SOFT_DELETE_NAMES:
                soft_delete_col = c.name
                break

        # Detect dimension fields
        dim_fields = [c.name for c in all_cols if c.name in DIMENSION_NAMES]

        # Infer domain from table prefix
        domain = ""
        for prefix, dom in sorted(self.domain_map.items(), key=lambda x: -len(x[0])):
            if table_name.startswith(prefix):
                domain = dom
                break
        if not domain:
            domain = "06-trk"

        return Table(
            name=table_name,
            entity_class=entity_class,
            source_file=str(path.relative_to(repo_root)) if repo_root in path.parents else str(path),
            purpose=purpose,
            domain=domain,
            columns=all_cols,
            extends_base_entity=extends_base,
            soft_delete_column=soft_delete_col,
            dimension_fields=dim_fields if dim_fields else [],
            base_entity_name="BaseEntity" if extends_base else None,
        )

    def discover_enums(self, repo_root: Path) -> list[Path]:
        """Find all Java enum files in dict/ directories."""
        enum_files = []
        for java_path in repo_root.rglob("*.java"):
            if "/target/" in str(java_path):
                continue
            if "dict" in java_path.parts:
                enum_files.append(java_path)
        return enum_files

    def parse_enum(self, path: Path, src: str, i18n: dict[str, str], repo_root: Path | None = None) -> EnumDef | None:
        """Parse single Java enum file → EnumDef."""
        cls_m = re.search(r'(?:public\s+)?enum\s+(\w+)', src)
        if not cls_m:
            return None
        cls_name = cls_m.group(1)

        pkg_m = re.search(r'^package\s+([\w.]+)\s*;', src, re.MULTILINE)
        full_class = f"{pkg_m.group(1)}.{cls_name}" if pkg_m else cls_name

        # Paradigm A: `NAME((short) N, "key")` or `NAME((Short)N, "key")`
        # Paradigm B: multi-field with aggregator / Set<Integer>
        is_paradigm_b = bool(re.search(r'Set\s*<\s*Integer\s*>', src) or "aggregator" in src.lower())
        paradigm = EnumParadigm.DIMENSIONAL if is_paradigm_b else EnumParadigm.SIMPLE

        values = []
        if paradigm == EnumParadigm.SIMPLE:
            # Match: NAME((short) N, "key") or NAME((Short) N, "key")
            for m in re.finditer(
                r'(\w+)\s*\(\s*\(?[Ss]hort\)?\s*(\d+)\s*,\s*"([^"]+)"\s*\)',
                src
            ):
                name, code, key = m.group(1), int(m.group(2)), m.group(3)
                if name in ("class", "enum", "interface", "void", "return"):
                    continue
                i18n_zh = i18n.get(key, "")
                values.append(EnumValue(
                    code=code,
                    name=name,
                    message_key=key,
                    i18n_zh=i18n_zh,
                    aggregators=None,
                    is_default_meaning=True,
                ))
        else:
            # Paradigm B: NAME((short) N, "extName", "key", bool, "statKey", Sets.newHashSet(N,...)/null)
            for m in re.finditer(
                r'(\w+)\s*\(\s*\(?[Ss]hort\)?\s*(\d+)\s*,\s*"([^"]*)"'
                r'(?:\s*,\s*"([^"]*)")?'     # optional: extName or key
                r'(?:[^;{(]*?Sets\.newHashSet\(([^)]+)\)|\s*,\s*null)?',
                src
            ):
                name = m.group(1)
                if name in ("class", "enum", "interface", "void", "return",
                            cls_name, "get", "getStatus", "getMessageByStatus"):
                    continue
                code = int(m.group(2))
                str1 = m.group(3) or ""
                str2 = m.group(4) or ""
                # For OrderStatus: (code, extName, messageKey, ...)
                key = str2 if str2 else str1
                agg_raw = m.group(5)
                aggregators = None
                if agg_raw:
                    try:
                        aggregators = [int(x.strip()) for x in agg_raw.split(",") if x.strip().isdigit()]
                    except Exception:
                        aggregators = None
                i18n_zh = i18n.get(key, "")
                warning = None
                if aggregators:
                    warning = f"同 code={code} 对 aggregator={aggregators} 含义不同，需带维度过滤"
                values.append(EnumValue(
                    code=code,
                    name=name,
                    message_key=key,
                    i18n_zh=i18n_zh,
                    aggregators=aggregators,
                    is_default_meaning=aggregators is None,
                    warning=warning,
                ))

        if not values:
            return None

        return EnumDef(
            enum_class=full_class,
            short_name=cls_name,
            paradigm=paradigm,
            source_file=str(path.relative_to(repo_root)) if repo_root in path.parents else str(path),
            java_type="Short",
            values=values,
        )

    def parse_relations(self, repo_root: Path) -> list:
        """tkmybatis has no explicit relation annotations; return [] and rely on naming inference."""
        return []

    @property
    def base_entity_resolver(self) -> dict[str, list[dict]]:
        """Return BaseEntity public field definitions."""
        return {"BaseEntity": BASE_ENTITY_COLS}
