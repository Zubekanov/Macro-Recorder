"""Find actions: wait for image, wait for text."""

from __future__ import annotations

from pathlib import Path

from macro_recorder.model.actions._common import (
    boolean, duration, fmt_ms, number, on_timeout_fields, region, template, var_name,
)
from macro_recorder.model.actions.variables import region_overlay, region_text
from macro_recorder.model.catalogue import ActionSpec, FieldKind, FieldSpec, Preset, make, register
from macro_recorder.model.document import Row
from macro_recorder.model.events import EventType

IMAGE_TYPES = (("Images", "*.png *.jpg *.jpeg *.bmp"), ("All files", "*"))


def _describe_image(row: Row) -> str:
    e = row.event
    name = Path(e["image_path"]).name if e["image_path"] else "?"
    return '%s = image "%s" in %s within %s' % (e["var_name"], name, region_text(row), fmt_ms(e["timeout_ms"]))


MATCH_IMAGE = register(ActionSpec(
    type=EventType.MATCH_IMAGE.value, name="Wait for Image", category="Find",
    fields=(
        FieldSpec(keys=("image_path",), label="Image file", kind=FieldKind.FILE, default="",
                  file_types=IMAGE_TYPES),
        region(),
        number("confidence", "Confidence", 0.9, minimum=0, maximum=1, allow_expr=False),
        duration("timeout_ms", "Timeout", 5000),
        var_name("var_name", "Found variable", "found"),
        var_name("capture_var", "Centre X variable", ""),
        var_name("capture_var_y", "Centre Y variable", ""),
        boolean("click_found", "Click when found", False),
    ) + on_timeout_fields("continue"),
    presets=(Preset("Wait for image", None,
                    lambda: make("match_image", x=100, y=100, x2=400, y2=300)),),
    describe=_describe_image, overlay=region_overlay,
))


def _describe_text(row: Row) -> str:
    e = row.event
    return '%s = text "%s" in %s within %s' % (e["var_name"], e["expr"], region_text(row), fmt_ms(e["timeout_ms"]))


MATCH_TEXT = register(ActionSpec(
    type=EventType.MATCH_TEXT.value, name="Wait for Text", category="Find",
    fields=(
        template("expr", "Expected text", "expected"),
        region(),
        number("similarity", "Similarity", 0.8, minimum=0, maximum=1, allow_expr=False),
        duration("timeout_ms", "Timeout", 5000),
        var_name("var_name", "Found variable", "found"),
        var_name("capture_var", "Text variable", ""),
    ) + on_timeout_fields("continue"),
    presets=(Preset("Wait for text", None,
                    lambda: make("match_text", expr="expected", x=100, y=100, x2=400, y2=300)),),
    describe=_describe_text, overlay=region_overlay,
))
