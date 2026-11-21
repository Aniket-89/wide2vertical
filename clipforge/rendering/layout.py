"""Choose a vertical layout and compute exact crop geometry. Pure: numbers in, numbers out.

Layouts:
- ``facecam_top_gameplay_bottom``: streamer's facecam overlay stacked over a gameplay crop
- ``center_crop``: a 9:16 crop following the person (full-cam / just-chatting streams)
- ``gameplay_blur_background``: the whole frame, letterboxed over a blurred zoom of itself
"""

from __future__ import annotations

import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

LAYOUTS = ("facecam_top_gameplay_bottom", "center_crop", "gameplay_blur_background")
LAYOUT_RULES_VERSION = "2"  # bump when choose_layout logic changes: invalidates renders
OUT_W, OUT_H = 1080, 1920


@dataclass(frozen=True, slots=True)
class Box:
    x: int
    y: int
    w: int
    h: int

    @property
    def cx(self) -> float:
        return self.x + self.w / 2

    @property
    def cy(self) -> float:
        return self.y + self.h / 2


@dataclass(frozen=True, slots=True)
class FrameFaces:
    """Face detections from frames sampled across the clip."""

    width: int
    height: int
    samples: Sequence[Sequence[Box]]  # one list of faces per sampled frame


@dataclass(frozen=True, slots=True)
class LayoutPlan:
    layout: str
    cam: Box | None  # source-pixel crop for the facecam (facecam layout only)
    game: Box  # source-pixel crop for the main area
    cam_out_h: int = 0  # output height of the facecam band (facecam layout only)
    reason: str = ""


def _even(v: float) -> int:
    return max(2, round(v / 2) * 2)


def _clamp_box(x: float, y: float, w: float, h: float, W: int, H: int) -> Box:
    w, h = min(w, W), min(h, H)
    x = min(max(0.0, x), W - w)
    y = min(max(0.0, y), H - h)
    return Box(_even(x) if x > 1 else 0, _even(y) if y > 1 else 0, _even(w), _even(h))


def crop_to_aspect(area: Box, aspect_w: int, aspect_h: int, cx: float | None = None) -> Box:
    """Largest crop of ``area`` with aspect aspect_w:aspect_h, centred on cx (or area centre)."""
    target = aspect_w / aspect_h
    if area.w / area.h > target:  # area too wide: crop width
        w, h = area.h * target, float(area.h)
    else:
        w, h = float(area.w), area.w / target
    centre = area.cx if cx is None else cx
    x = min(max(area.x, centre - w / 2), area.x + area.w - w)
    y = area.y + (area.h - h) / 2
    return Box(_even(x) if x > 1 else 0, _even(y) if y > 1 else 0, _even(w), _even(h))


def facecam_from_face(face: Box, W: int, H: int) -> Box:
    """Estimate the facecam overlay rectangle from the streamer's face: typical overlays are
    ~16:9 and about 4x as wide as the face, with the face in the upper middle."""
    cam_w = min(W * 0.45, face.w * 4.2)
    cam_h = cam_w * 9 / 16
    return _clamp_box(face.cx - cam_w / 2, face.cy - cam_h * 0.45, cam_w, cam_h, W, H)


def _consistent_face(ff: FrameFaces, min_hit_ratio: float) -> Box | None:
    """Median position of the largest face, if a face appears in enough samples at a stable
    place (a facecam doesn't move; a face in gameplay footage usually does)."""
    picks = [max(fs, key=lambda b: b.w * b.h) for fs in ff.samples if fs]
    if not ff.samples or len(picks) / len(ff.samples) < min_hit_ratio:
        return None
    med = Box(
        int(statistics.median(b.x for b in picks)),
        int(statistics.median(b.y for b in picks)),
        int(statistics.median(b.w for b in picks)),
        int(statistics.median(b.h for b in picks)),
    )
    drift = statistics.median(abs(b.cx - med.cx) + abs(b.cy - med.cy) for b in picks)
    return med if drift <= max(ff.width, ff.height) * 0.08 else None


def _dominant(ff: FrameFaces, min_ratio: float = 0.7, area_ratio: float = 2.5) -> bool:
    """In most frames with faces there is a single face, or the largest is clearly bigger
    than the next one (a webcam, not a panel of people)."""
    with_faces = [fs for fs in ff.samples if fs]
    if not with_faces:
        return False
    ok = 0
    for fs in with_faces:
        areas = sorted((b.w * b.h for b in fs), reverse=True)
        if len(areas) == 1 or areas[0] >= area_ratio * areas[1]:
            ok += 1
    return ok / len(with_faces) >= min_ratio


def _near_edge(face: Box, W: int, H: int, band: float = 0.3) -> bool:
    """Webcam overlays sit in a corner or along an edge, not in the middle of the frame."""
    return (
        face.cx < band * W
        or face.cx > (1 - band) * W
        or face.cy < band * H
        or face.cy > (1 - band) * H
    )


def _from_override(ov: Mapping[str, Any], W: int, H: int) -> LayoutPlan | None:
    """Manual override saved per clip or per source. Rects are normalised 0..1 [x, y, w, h]
    so one override works at any source resolution."""

    def rect(key: str) -> Box | None:
        r = ov.get(key)
        if not (isinstance(r, list | tuple) and len(r) == 4):
            return None
        x, y, w, h = (float(v) for v in r)
        return _clamp_box(x * W, y * H, w * W, h * H, W, H)

    layout = ov.get("layout")
    if layout not in LAYOUTS:
        return None
    full = Box(0, 0, W, H)
    if layout == "facecam_top_gameplay_bottom":
        cam = rect("cam")
        if cam is None:
            return None
        return _stack_plan(cam, rect("game") or full, "manual override")
    if layout == "center_crop":
        area = rect("game") or full
        return LayoutPlan(layout, None, crop_to_aspect(area, 9, 16), 0, "manual override")
    return LayoutPlan(layout, None, rect("game") or full, 0, "manual override")


def _stack_plan(cam: Box, game_area: Box, reason: str) -> LayoutPlan:
    cam_out_h = _even(min(max(OUT_W * cam.h / cam.w, 480), 840))
    game = crop_to_aspect(game_area, OUT_W, OUT_H - cam_out_h)
    return LayoutPlan("facecam_top_gameplay_bottom", cam, game, cam_out_h, reason)


def choose_layout(
    ff: FrameFaces,
    requested: str = "auto",
    override: Mapping[str, Any] | None = None,
    *,
    min_hit_ratio: float = 0.4,
    facecam_max_face_ratio: float = 0.12,
) -> LayoutPlan:
    W, H = ff.width, ff.height
    full = Box(0, 0, W, H)
    if override:
        plan = _from_override(override, W, H)
        if plan is not None:
            return plan
    face = _consistent_face(ff, min_hit_ratio)

    if requested == "gameplay_blur_background":
        return LayoutPlan(requested, None, full, 0, "requested")
    if requested == "center_crop":
        cx = face.cx if face else None
        return LayoutPlan(requested, None, crop_to_aspect(full, 9, 16, cx), 0, "requested")
    if requested == "facecam_top_gameplay_bottom":
        if face is None:  # can't stack a facecam we can't find: show everything instead
            return LayoutPlan(
                "gameplay_blur_background", None, full, 0, "facecam requested but none found"
            )
        return _stack_plan(facecam_from_face(face, W, H), full, "requested")

    # auto
    if face is None:
        return LayoutPlan("gameplay_blur_background", None, full, 0, "no stable face found")
    if face.w / W <= facecam_max_face_ratio:
        # A small stable face is only a facecam overlay if it's the one dominant face and sits
        # toward an edge. A static wide shot of a stage/crowd also has small, still faces.
        if not _dominant(ff) or not _near_edge(face, W, H):
            return LayoutPlan(
                "gameplay_blur_background",
                None,
                full,
                0,
                "small faces but no facecam overlay (crowd or centred)",
            )
        return _stack_plan(
            facecam_from_face(face, W, H), full, "small stable face: facecam overlay"
        )
    return LayoutPlan(
        "center_crop",
        None,
        crop_to_aspect(full, 9, 16, face.cx),
        0,
        "large face: full-camera stream",
    )
