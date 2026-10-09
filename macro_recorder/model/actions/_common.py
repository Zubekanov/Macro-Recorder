"""Helpers shared by the category modules."""

from __future__ import annotations

from typing import Any, Mapping

from macro_recorder.model.catalogue import END_TARGET, FieldKind, FieldSpec


def fmt_num(value: Any) -> str:
    """Compact text for a number-or-expression: 12, 12.5, or the expression."""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, float):
        return ("%.3f" % value).rstrip("0").rstrip(".")
    if value is None:
        return ""
    return str(value)


def fmt_point(x: Any, y: Any) -> str:
    return "(%s, %s)" % (fmt_num(x), fmt_num(y))


def fmt_ms(value: Any) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value >= 1000 and value % 1000 == 0:
            return "%d s" % (value // 1000)
        return "%s ms" % fmt_num(value)
    return "%s ms" % value


def coord_pair(x_key: str, y_key: str, label: str, default: tuple[Any, Any] = (100, 100),
               targetable: bool = True, **kw: Any) -> FieldSpec:
    return FieldSpec(keys=(x_key, y_key), label=label, kind=FieldKind.COORD_PAIR,
                     default=default, targeting_fills=(x_key, y_key) if targetable else (), **kw)


def region(label: str = "Region") -> FieldSpec:
    """x, y, x2, y2 corners or a monitor number (1-based, None = corners)."""
    return FieldSpec(keys=("x", "y", "x2", "y2", "monitor"), label=label, kind=FieldKind.REGION,
                     default=(100, 100, 400, 300, None), targeting_fills=("x", "y", "x2", "y2"),
                     help="Two opposite corners, or a monitor number for the whole monitor")


def duration(key: str, label: str, default: Any, **kw: Any) -> FieldSpec:
    return FieldSpec(keys=(key,), label=label, kind=FieldKind.DURATION, default=default,
                     minimum=0, **kw)


def number(key: str, label: str, default: Any, **kw: Any) -> FieldSpec:
    return FieldSpec(keys=(key,), label=label, kind=FieldKind.NUMBER, default=default, **kw)


def choice(key: str, label: str, choices: tuple[str, ...], default: str | None = None, **kw: Any) -> FieldSpec:
    return FieldSpec(keys=(key,), label=label, kind=FieldKind.CHOICE, choices=choices,
                     default=default if default is not None else choices[0], **kw)


def text(key: str, label: str, default: str = "", **kw: Any) -> FieldSpec:
    return FieldSpec(keys=(key,), label=label, kind=FieldKind.TEXT, default=default, **kw)


def template(key: str, label: str, default: str = "", **kw: Any) -> FieldSpec:
    return FieldSpec(keys=(key,), label=label, kind=FieldKind.TEMPLATE, default=default, **kw)


def boolean(key: str, label: str, default: bool = False, **kw: Any) -> FieldSpec:
    return FieldSpec(keys=(key,), label=label, kind=FieldKind.BOOL, default=default, **kw)


def var_name(key: str, label: str, default: str = "", **kw: Any) -> FieldSpec:
    return FieldSpec(keys=(key,), label=label, kind=FieldKind.VAR_NAME, default=default, **kw)


def label_target(key: str, label: str, default: str | None = END_TARGET,
                 none_label: str | None = None, **kw: Any) -> FieldSpec:
    return FieldSpec(keys=(key,), label=label, kind=FieldKind.LABEL_TARGET, default=default,
                     none_label=none_label, **kw)


def on_timeout_fields(default: str = "abort") -> tuple[FieldSpec, FieldSpec]:
    return (
        choice("on_timeout", "On timeout", ("abort", "continue", "goto"), default),
        label_target("target", "Go to", none_label=None,
                     visible_when=lambda v: v.get("on_timeout") == "goto"),
    )


def when_goto(values: Mapping[str, Any]) -> bool:
    return values.get("on_timeout") == "goto"
