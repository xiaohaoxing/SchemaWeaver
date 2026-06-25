"""Unified data model for schema extraction. ORM-agnostic dataclasses."""
from dataclasses import dataclass, field
from enum import Enum


class ColType(str, Enum):
    """Normalized column types across ORMs."""
    STRING = "string"
    INTEGER = "integer"
    LONG = "long"
    DECIMAL = "decimal"
    BOOLEAN = "boolean"
    DATE = "date"
    DATETIME = "datetime"
    TEXT = "text"
    BLOB = "blob"
    ENUM = "enum"
    UNKNOWN = "unknown"


class EnumParadigm(str, Enum):
    """Enum patterns for business semantics."""
    SIMPLE = "A"       # (code, messageKey) — code meaning is unique
    DIMENSIONAL = "B"  # multi-field, same code means different things for different aggregators


@dataclass
class Column:
    """Single database column."""
    name: str                  # Physical column name (snake_case)
    java_field: str = ""       # Source field name (camelCase), = name for non-Java
    type_raw: str = ""         # Source type string ("Integer"/"String"/"CharField")
    type: ColType = ColType.UNKNOWN  # Normalized type
    is_primary_key: bool = False
    is_nullable: bool = True
    comment: str = ""          # From @ApiModelProperty/@ColumnLength/comment
    name_inferred: bool = False  # camelCase→snake_case inferred (no @Column name)
    from_base_entity: bool = False  # Inherited from base class
    enum_class: str | None = None   # Bound enum class (e.g., "OrderStatus")


@dataclass
class Table:
    """Database table."""
    name: str                  # Physical table name (lower)
    entity_class: str = ""     # Fully qualified class name
    source_file: str = ""      # Relative path in repo
    purpose: str = ""          # One-line purpose (from @ApiModel)
    domain: str = ""           # Business domain (e.g., "01-pcl")
    columns: list[Column] = field(default_factory=list)
    extends_base_entity: bool = False
    soft_delete_column: str | None = None   # Auto-detected soft-delete column
    dimension_fields: list[str] = field(default_factory=list)  # Auto-detected dimension fields
    base_entity_name: str | None = None  # Base class name for cross-table merge


@dataclass
class EnumValue:
    """Single enum constant."""
    code: int
    name: str                  # Enum constant name (CREATED/CONFIRMED)
    message_key: str = ""
    i18n_zh: str = ""          # Decoded Chinese
    aggregators: list[int] | None = None  # Paradigm B: which aggregators this code applies to
    is_default_meaning: bool = True  # aggregator=None means default meaning
    warning: str | None = None  # Auto-generated warning for paradigm B divergence


@dataclass
class EnumDef:
    """Enum class definition."""
    enum_class: str            # Fully qualified name
    short_name: str            # e.g., "OrderStatus"
    paradigm: EnumParadigm = EnumParadigm.SIMPLE
    source_file: str = ""
    java_type: str = "Short"   # Code field Java type
    values: list[EnumValue] = field(default_factory=list)


@dataclass
class Relation:
    """Foreign key / relationship."""
    from_table: str
    from_column: str
    to_table: str
    to_column: str = "id"
    kind: str = "fk"           # fk / one_to_many / many_to_one
    confidence: str = "inferred"  # declared(annotated) / inferred(naming) / curated(human)


@dataclass
class I18nKey:
    """Internationalization key-value pair."""
    key: str
    value_zh: str
    bundle_file: str = ""


@dataclass
class ColumnEnumBinding:
    """Binding between a column and an enum class."""
    table: str
    column: str
    enum_class: str
    confidence: str = "inferred"  # inferred(naming/type) / declared(@EnumValue) / curated
