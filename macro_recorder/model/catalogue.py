"""The action catalogue: FieldSpec, ActionSpec and the registry.

Category modules under ``model/actions/`` call :func:`register` at import
time.  Everything that needs to know about an action type (the Details
panel, the Value column, the Add menu, validation, the file reader, the
player) asks the registry.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import TYPE_CHECKING, Any, Callable, Mapping

from macro_recorder.model.events import EVENT_TYPE_VALUES, MacroEvent, freeze_value
from macro_recorder.model.protocols import OverlayShape, PlayContext

if TYPE_CHECKING:
    from macro_recorder.model.document import Row


class FieldKind(Enum):
    NUMBER = auto()         # one numeric key; expression allowed unless allow_expr is False
    COORD_PAIR = auto()     # two keys (x, y); frame-adjusted at play time
    REGION = auto()         # five keys (x, y, x2, y2, monitor); monitor overrides corners
    DURATION = auto()       # milliseconds; expression allowed
    TEXT = auto()           # plain string
    TEMPLATE = auto()       # string with {expr} segments
    CHOICE = auto()         # one of choices
    FILE = auto()           # path with a Browse button
    COMMAND = auto()        # shell command with a Browse button that inserts a quoted path
    BOOL = auto()
    COLOUR = auto()         # "#rrggbb"
    LABEL_TARGET = auto()   # a row label, "Start", "End" or none
    VAR_NAME = auto()       # identifier
    KEY = auto()            # one keysym name
    KEY_COMBO = auto()      # "ctrl+shift+s"
    INFO = auto()           # read-only text


_EXPR_KINDS = {FieldKind.NUMBER, FieldKind.COORD_PAIR, FieldKind.REGION, FieldKind.DURATION}


@dataclass(frozen=True)
class FieldSpec:
    keys: tuple[str, ...]
    label: str
    kind: FieldKind
    default: Any = None
    choices: tuple[str, ...] = ()
    allow_expr: bool = True
    minimum: float | None = None
    maximum: float | None = None
    integer: bool = False
    file_types: tuple[tuple[str, str], ...] = ()
    none_label: str | None = None
    visible_when: Callable[[Mapping[str, Any]], bool] | None = None
    enabled_when: Callable[[Mapping[str, Any]], bool] | None = None
    targeting_fills: tuple[str, ...] = ()
    help: str = ""

    def __post_init__(self) -> None:
        if self.kind == FieldKind.COORD_PAIR and len(self.keys) != 2:
            raise ValueError("COORD_PAIR needs two keys: %r" % (self.keys,))
        if self.kind == FieldKind.REGION and len(self.keys) != 5:
            raise ValueError("REGION needs five keys: %r" % (self.keys,))
        if self.kind not in (FieldKind.COORD_PAIR, FieldKind.REGION) and len(self.keys) != 1:
            raise ValueError("%s needs one key: %r" % (self.kind.name, self.keys))
        if self.kind == FieldKind.CHOICE and not self.choices:
            raise ValueError("CHOICE needs choices: %r" % (self.keys,))

    @property
    def key(self) -> str:
        return self.keys[0]

    @property
    def expr_allowed(self) -> bool:
        return self.allow_expr and self.kind in _EXPR_KINDS

    def defaults(self) -> dict[str, Any]:
        """Default value per key.  Multi-key fields take a tuple default."""
        if len(self.keys) == 1:
            return {self.keys[0]: self.default}
        values = self.default if isinstance(self.default, (tuple, list)) else (None,) * len(self.keys)
        return dict(zip(self.keys, values))


@dataclass(frozen=True)
class Preset:
    name: str
    insert_key: str | None
    make: Callable[[], MacroEvent]


class Flow(Enum):
    NEXT = auto()
    STOP = auto()


@dataclass(frozen=True)
class Jump:
    target: str                 # a label, "Start" or "End"


@dataclass(frozen=True)
class Abort:
    message: str        # without the "Row N: " prefix; the engine adds it


START_TARGET = "Start"
END_TARGET = "End"
RESERVED_TARGETS = (START_TARGET, END_TARGET)

FILTER_GROUPS = ("mouse_move", "mouse_click", "mouse_scroll", "keyboard", "wait")
CATEGORIES = ("Mouse", "Keyboard", "Wait", "Window", "Variable", "Control", "Find", "Misc")


def _default_describe(row: "Row") -> str:
    return ", ".join("%s=%s" % (k, v) for k, v in row.events[0].params.items())


@dataclass(frozen=True)
class ActionSpec:
    type: str
    name: str
    category: str
    fields: tuple[FieldSpec, ...]
    presets: tuple[Preset, ...] = ()
    filter_group: str | None = None
    play: Callable[[PlayContext, "Row"], Flow | Jump | Abort] = lambda ctx, row: Flow.NEXT
    describe: Callable[["Row"], str] = _default_describe
    overlay: Callable[["Row"], OverlayShape | None] = lambda row: None
    layout: Callable[["Row"], tuple[FieldSpec, ...]] | None = None
    read: Callable[["Row"], dict[str, Any]] | None = None
    write: Callable[["Row", Mapping[str, Any]], "Row"] | None = None
    multi_event: bool = False       # True only for the path kind of mouse_move

    def __post_init__(self) -> None:
        if self.type not in EVENT_TYPE_VALUES:
            raise ValueError("Unknown event type %r" % self.type)
        if self.category not in CATEGORIES:
            raise ValueError("Unknown category %r" % self.category)
        if self.filter_group is not None and self.filter_group not in FILTER_GROUPS:
            raise ValueError("Unknown filter group %r" % self.filter_group)
        seen: set[str] = set()
        for f in self.fields:
            for k in f.keys:
                if k in seen:
                    raise ValueError("Duplicate field key %r in %s" % (k, self.type))
                seen.add(k)

    @property
    def keys(self) -> tuple[str, ...]:
        return tuple(k for f in self.fields for k in f.keys)

    def defaults(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for f in self.fields:
            out.update(f.defaults())
        return out

    def field_for(self, key: str) -> FieldSpec:
        for f in self.fields:
            if key in f.keys:
                return f
        raise KeyError(key)

    def layout_for(self, row: "Row") -> tuple[FieldSpec, ...]:
        return self.layout(row) if self.layout is not None else self.fields

    def read_row(self, row: "Row") -> dict[str, Any]:
        if self.read is not None:
            return self.read(row)
        return dict(row.events[0].params)

    def write_row(self, row: "Row", values: Mapping[str, Any]) -> "Row":
        if self.write is not None:
            return self.write(row, values)
        from dataclasses import replace
        ev = row.events[0].with_params(**{k: freeze_value(v) for k, v in values.items()})
        return replace(row, events=(ev,))


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_registry: dict[str, ActionSpec] = {}


def register(spec: ActionSpec) -> ActionSpec:
    if spec.type in _registry:
        raise ValueError("Action type %r registered twice" % spec.type)
    _registry[spec.type] = spec
    return spec


def spec(type_: str) -> ActionSpec:
    """The spec for an event type.  Raises KeyError for an unknown type."""
    _ensure_loaded()
    return _registry[str(type_)]


def spec_for(row: "Row") -> ActionSpec:
    return spec(row.events[0].type)


def all_specs() -> tuple[ActionSpec, ...]:
    _ensure_loaded()
    return tuple(_registry[t] for t in sorted(_registry))


def presets() -> tuple[tuple[ActionSpec, Preset], ...]:
    """Every preset with its spec, in category then registration order."""
    _ensure_loaded()
    order = {c: i for i, c in enumerate(CATEGORIES)}
    out = []
    for s in sorted(_registry.values(), key=lambda s: order[s.category]):
        for p in s.presets:
            out.append((s, p))
    return tuple(out)


def make(type_: str, dt: float = 0.0, **params: Any) -> MacroEvent:
    """Build an event with defaults filled in.  Rejects unknown keys."""
    s = spec(type_)
    values = s.defaults()
    unknown = set(params) - set(values)
    if unknown:
        raise ValueError("Unknown field(s) for %s: %s" % (type_, ", ".join(sorted(unknown))))
    values.update({k: freeze_value(v) for k, v in params.items()})
    return MacroEvent(str(type_), values, dt)


_loaded = False


def _ensure_loaded() -> None:
    """Import the category modules once so their specs are registered."""
    global _loaded
    if not _loaded:
        _loaded = True
        import macro_recorder.model.actions  # noqa: F401


def _reset_for_tests() -> None:
    global _loaded
    _registry.clear()
    _loaded = False
