"""Base adapter interface and registry for ORM-specific parsers."""
from abc import ABC, abstractmethod
from pathlib import Path
from importlib.metadata import entry_points
from ..model import Table, EnumDef, Relation


class BaseAdapter(ABC):
    """ORM/language adapter. Each adapter handles one ORM style."""

    # Class attributes (set by subclasses)
    id: str = ""               # "tkmybatis" / "mybatis-plus" / "sqlalchemy" / "django" / "gorm"
    language: str = ""         # "java" / "python" / "go" / "ts"
    file_globs: list[str] = [] # ["**/*.java"] / ["**/models.py"]
    orm_markers: list[str] = [] # Heuristic: file contains these strings → delegate to this adapter

    @abstractmethod
    def supports_file(self, path: Path, src: str) -> bool:
        """Quick check if this file belongs to this adapter (marker check, avoid full AST)."""
        pass

    @abstractmethod
    def discover_tables(self, repo_root: Path) -> list[Path]:
        """Return list of entity files this adapter handles (rglob + glob filter)."""
        pass

    @abstractmethod
    def parse_table(self, path: Path, src: str, base_entity_cols: list[dict] | None, repo_root: Path) -> Table | None:
        """Parse single entity file → Table (with columns). Return None if not an entity."""
        pass

    @abstractmethod
    def discover_enums(self, repo_root: Path) -> list[Path]:
        """Return enum/dict file list. Return [] if this ORM has no code-level enums."""
        pass

    @abstractmethod
    def parse_enum(self, path: Path, src: str, i18n: dict[str, str], repo_root: Path) -> EnumDef | None:
        """Parse single enum file → EnumDef."""
        pass

    def parse_relations(self, repo_root: Path) -> list[Relation]:
        """Optional: parse explicit relations (JPA @OneToMany / Prisma relation / GORM foreignKey).
        tkmybatis returns [], relations inferred by naming + curated."""
        return []

    @property
    def base_entity_resolver(self) -> dict[str, list[dict]]:
        """Return {base_class_name: [public_field_definitions]}. tkmybatis returns {"BaseEntity": [...13 fields]}.
        Other ORMs default to {}. Subclass overrides."""
        return {}


class AdapterRegistry:
    """Registry of adapters, auto-discovered via entry_points."""

    def __init__(self):
        self._adapters: list[BaseAdapter] = []

    def register(self, adapter: BaseAdapter):
        self._adapters.append(adapter)

    def all(self) -> list[BaseAdapter]:
        return self._adapters

    def select(self, path: Path, src: str) -> BaseAdapter | None:
        """Find first adapter that supports this file."""
        for a in self._adapters:
            if a.supports_file(path, src):
                return a
        return None


def load_adapters() -> AdapterRegistry:
    """Load all adapters registered via entry_points group='schemaweaver.adapters'."""
    reg = AdapterRegistry()
    for ep in entry_points(group="schemaweaver.adapters"):
        try:
            adapter_cls = ep.load()
            reg.register(adapter_cls())
        except Exception as e:
            # Skip broken adapters gracefully
            pass
    return reg
