"""Find actions: wait for image, wait for text."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable

from macro_recorder.model.actions._common import (
    boolean, duration, fmt_ms, number, on_timeout_fields, region, template, var_name,
)
from macro_recorder.model.actions.variables import name_problem, region_overlay, region_text
from macro_recorder.model.catalogue import (
    Abort, ActionSpec, FieldKind, FieldSpec, Flow, Jump, Preset, make, register,
)
from macro_recorder.model.document import Row
from macro_recorder.model.events import EventType
from macro_recorder.model.protocols import PlayContext

IMAGE_TYPES = (("Images", "*.png *.jpg *.jpeg *.bmp"), ("All files", "*"))

POLL_MS = 300

def text_similarity(a: str, b: str) -> float:
    """Normalised Levenshtein similarity, 0 to 1 (1 = identical).

    Whitespace runs collapse to one space and case is ignored first, to absorb
    OCR artefacts.
    """
    a = " ".join(a.split()).lower()
    b = " ".join(b.split()).lower()
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return 1.0 - prev[-1] / max(len(a), len(b))


def _poll(ctx: PlayContext, timeout_ms: float, probe: Callable[[], Any]) -> tuple[Any, bool]:
    """Call ``probe`` every POLL_MS until it returns non-None or the timeout passes.

    Returns (result or None, stopped).  The timeout and poll interval are
    not scaled by speed.
    """
    deadline = ctx.now() + timeout_ms / 1000.0
    while True:
        result = probe()
        if result is not None:
            return result, False
        remaining_ms = (deadline - ctx.now()) * 1000.0
        if remaining_ms <= 0:
            return None, False
        if ctx.sleep(min(POLL_MS, remaining_ms), scaled=False):
            return None, True


def _check_names(*names: str) -> Abort | None:
    """The first problem among the non-empty variable names."""
    for name in names:
        if name:
            problem = name_problem(name)
            if problem is not None:
                return problem
    return None


def _store(ctx: PlayContext, values: dict[str, Any]) -> None:
    """Assign each value whose variable name is non-empty (names already checked)."""
    for name, value in values.items():
        if name:
            ctx.variables[name] = value


def _on_timeout(row: Row, message: str) -> Flow | Jump | Abort:
    e = row.event
    if e["on_timeout"] == "abort":
        return Abort(message)
    if e["on_timeout"] == "goto":
        return Jump(e["target"]) if e["target"] else Abort(message + " and Go to has no target")
    return Flow.NEXT


def _image_file(ctx: PlayContext, path: str) -> str:
    """A relative image path is read from the macro's folder."""
    if ctx.document_dir and not os.path.isabs(path):
        return os.path.join(ctx.document_dir, path)
    return path


def _play_image(ctx: PlayContext, row: Row) -> Flow | Jump | Abort:
    e = row.event
    problem = _check_names(e["var_name"], e["capture_var"], e["capture_var_y"])
    if problem is not None:
        return problem
    if not e["image_path"]:
        return Abort("Wait for Image has no image file")
    path = _image_file(ctx, e["image_path"])
    if not os.path.isfile(path):
        return Abort("image file %s not found" % path)
    rect = ctx.region(e.params)
    confidence = float(e["confidence"])
    hit, stopped = _poll(ctx, ctx.resolve(e["timeout_ms"]),
                         lambda: ctx.screen.find_image(rect, path, confidence))
    if stopped:
        return Flow.STOP
    if hit is None:
        _store(ctx, {e["var_name"]: 0})
        return _on_timeout(row, 'image "%s" not found within %s'
                           % (Path(path).name, fmt_ms(e["timeout_ms"])))
    cx, cy = hit[0], hit[1]
    fx, fy = ctx.to_frame(cx, cy)
    _store(ctx, {e["var_name"]: 1, e["capture_var"]: round(fx), e["capture_var_y"]: round(fy)})
    if e["click_found"]:
        ctx.input.move(cx, cy)
        ctx.input.press_button("left")
        ctx.input.release_button("left")
    return Flow.NEXT


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
    describe=_describe_image, overlay=region_overlay, play=_play_image,
))


def _describe_text(row: Row) -> str:
    e = row.event
    return '%s = text "%s" in %s within %s' % (e["var_name"], e["expr"], region_text(row), fmt_ms(e["timeout_ms"]))


def _play_text(ctx: PlayContext, row: Row) -> Flow | Jump | Abort:
    e = row.event
    problem = _check_names(e["var_name"], e["capture_var"])
    if problem is not None:
        return problem
    expected = ctx.render(e["expr"])
    rect = ctx.region(e.params)
    threshold = float(e["similarity"])

    def probe() -> str | None:
        found = ctx.screen.ocr(rect)
        return found if text_similarity(found, expected) >= threshold else None

    found, stopped = _poll(ctx, ctx.resolve(e["timeout_ms"]), probe)
    if stopped:
        return Flow.STOP
    if found is None:
        _store(ctx, {e["var_name"]: 0})
        return _on_timeout(row, 'text "%s" not found within %s' % (expected, fmt_ms(e["timeout_ms"])))
    _store(ctx, {e["var_name"]: 1, e["capture_var"]: found.strip()})
    return Flow.NEXT


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
    describe=_describe_text, overlay=region_overlay, play=_play_text,
))
