#!/usr/bin/env python3
"""Place reviewed native tiles without geometry repair or whole-image upscaling.

Usage: python compose.py RUN_DIR | python compose.py --self-test
Dependencies: Python 3.10+, Pillow, NumPy. Inputs and output paths are local to RUN_DIR.
All generation, content review, and any justified placement crop precede this helper.
"""
from __future__ import annotations

import argparse
import gc
import json
import math
import os
from pathlib import Path
import shutil
import tempfile

import numpy as np
from PIL import Image, ImageCms


ASPECT_TOLERANCE = 0.005
STRIP_ROWS = 128
SEAM_SEGMENT = 1500
OUTPUT_NAMES = ("composite.png", "preview.jpg", "compose_report.json", "seam_reviews")


def checked_ints(values, length, name):
    if not isinstance(values, (list, tuple)) or len(values) != length:
        raise ValueError(f"{name}: expected {length} integers")
    if any(isinstance(v, bool) or not isinstance(v, int) for v in values):
        raise ValueError(f"{name}: coordinates and dimensions must be integers")
    return tuple(values)


def local_path(run, relative, name):
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        raise ValueError(f"{name}: use a path relative to RUN_DIR")
    path = (run / relative).resolve()
    try:
        path.relative_to(run)
    except ValueError as exc:
        raise ValueError(f"{name}: path escapes RUN_DIR") from exc
    if not path.is_file():
        raise ValueError(f"{name}: missing file {relative}")
    return path


def no_existing_outputs(run):
    existing = [name for name in OUTPUT_NAMES if (run / name).exists()]
    if existing:
        raise FileExistsError(
            "Refusing to replace existing exports: " + ", ".join(existing)
            + ". Archive the previous exports or use a new run directory."
        )


def preflight(run):
    plan = json.loads((run / "plan.json").read_text(encoding="utf-8"))
    state = json.loads((run / "state.json").read_text(encoding="utf-8"))
    if plan.get("schema_version") != 1:
        raise ValueError("Unsupported plan schema_version")
    sw, sh = checked_ints(plan.get("source_size"), 2, "source_size")
    width, height = checked_ints(plan.get("target_size"), 2, "target_size")
    scale = plan.get("scale")
    if min(sw, sh, width, height) < 1 or isinstance(scale, bool) or not isinstance(scale, (int, float)) or not math.isfinite(scale) or scale <= 0:
        raise ValueError("Invalid source/target size or scale")
    if (width, height) != (round(sw * scale), round(sh * scale)):
        raise ValueError("target_size disagrees with source_size and scale")
    source = local_path(run, plan.get("source"), "source")
    with Image.open(source) as image:
        if image.size != (sw, sh):
            raise ValueError("Decoded source dimensions disagree with plan")
        image.load()
    entries = plan.get("tiles")
    if not isinstance(entries, list) or not entries:
        raise ValueError("Plan contains no tiles")
    states = state.get("tiles", {})
    validated, ids = [], set()
    for tile in entries:
        tid = tile.get("id")
        if not isinstance(tid, str) or not tid or tid in ids:
            raise ValueError("Tile IDs must be nonempty and unique")
        ids.add(tid)
        if tile.get("mode") != "generate":
            raise ValueError(f"{tid}: this helper accepts only mode=generate")
        status = states.get(tid, {})
        if status.get("status") != "qa_passed":
            raise ValueError(f"{tid}: only qa_passed tiles can be composed")
        if not isinstance(status.get("visual_review"), dict) or not status["visual_review"]:
            raise ValueError(f"{tid}: missing recorded visual_review")
        left, top, right, bottom = checked_ints(tile.get("box"), 4, f"{tid}.box")
        if not (0 <= left < right <= sw and 0 <= top < bottom <= sh):
            raise ValueError(f"{tid}: source box is outside the source")
        box = checked_ints(tile.get("target_box"), 4, f"{tid}.target_box")
        L, T, R, B = box
        if not (0 <= L < R <= width and 0 <= T < B <= height):
            raise ValueError(f"{tid}: target box is outside the target canvas")
        if box != tuple(round(v * scale) for v in (left, top, right, bottom)):
            raise ValueError(f"{tid}: target_box is not the global coordinate mapping")
        local_path(run, tile.get("input"), f"{tid}.input")
        local_path(run, tile.get("prompt"), f"{tid}.prompt")
        output = local_path(run, status.get("output"), f"{tid}.output")
        crop = checked_ints(status.get("placement", {}).get("crop"), 4, f"{tid}.placement.crop")
        with Image.open(output) as image:
            native_size = image.size
            image.load()
        recorded_native = checked_ints(status.get("native_size"), 2, f"{tid}.native_size")
        if recorded_native != native_size:
            raise ValueError(f"{tid}: recorded native_size does not match decoded output")
        x0, y0, x1, y1 = crop
        if not (0 <= x0 < x1 <= native_size[0] and 0 <= y0 < y1 <= native_size[1]):
            raise ValueError(f"{tid}: invalid placement crop")
        cw, ch, tw, th = x1 - x0, y1 - y0, R - L, B - T
        if cw < tw or ch < th:
            raise ValueError(f"{tid}: effective native crop {cw}x{ch} is below target {tw}x{th}")
        aspect_error = abs((cw / ch) / (tw / th) - 1.0)
        if aspect_error > ASPECT_TOLERANCE + 1e-12:
            raise ValueError(f"{tid}: crop aspect error {aspect_error:.3%} exceeds 0.5%; retry generation or review an evidence-backed crop")
        validated.append({
            "id": tid, "box": box, "crop": crop, "path": output,
            "output": status["output"], "native_size": native_size,
            "effective_size": (cw, ch), "aspect_error": aspect_error,
            "feather": [0, 0, 0, 0],
        })
    return plan, validated


def neighbors(tiles, width, height):
    """Find all intersecting or edge-adjacent tile pairs, including corner overlaps."""
    seams = []
    for i, first in enumerate(tiles):
        aL, aT, aR, aB = first["box"]
        for second in tiles[i + 1:]:
            bL, bT, bR, bB = second["box"]
            L, T, R, B = max(aL, bL), max(aT, bT), min(aR, bR), min(aB, bB)
            ow, oh = R - L, B - T
            if ow > 0 and oh > 0:
                seams.append({"tiles": [first["id"], second["id"]], "kind": "overlap", "box": [L, T, R, B]})
                for target, other in ((first, second), (second, first)):
                    tL, tT, tR, tB = target["box"]
                    oL, oT, oR, oB = other["box"]
                    # A fade is always strictly positive; unshared portions of an
                    # irregular edge therefore remain covered after normalization.
                    f = target["feather"]
                    if oL < tL < oR: f[0] = max(f[0], min(tR, oR) - tL)
                    if oT < tT < oB: f[1] = max(f[1], min(tB, oB) - tT)
                    if oL < tR < oR: f[2] = max(f[2], tR - max(tL, oL))
                    if oT < tB < oB: f[3] = max(f[3], tB - max(tT, oT))
            elif ow == 0 and oh > 0:
                seams.append({"tiles": [first["id"], second["id"]], "kind": "abut", "box": [max(0, L - 32), T, min(width, R + 32), B]})
            elif oh == 0 and ow > 0:
                seams.append({"tiles": [first["id"], second["id"]], "kind": "abut", "box": [L, max(0, T - 32), R, min(height, B + 32)]})
    return seams


def srgb_to_linear(rgb):
    return np.where(rgb <= 0.04045, rgb / 12.92, ((rgb + 0.055) / 1.055) ** 2.4)


def linear_to_srgb(rgb):
    rgb = np.clip(rgb, 0.0, 1.0)
    return np.where(rgb <= 0.0031308, rgb * 12.92, 1.055 * np.power(rgb, 1.0 / 2.4) - 0.055)


def normalized_premultiplied(tile):
    """Crop and downsample premultiplied linear RGBA; never enlarge either axis."""
    L, T, R, B = tile["box"]
    size = (R - L, B - T)
    with Image.open(tile["path"]) as image:
        cropped = image.convert("RGBA").crop(tile["crop"])
        embedded_profile = image.info.get("icc_profile")
        if embedded_profile:
            # Preserve alpha while honoring an embedded non-sRGB color profile.
            from io import BytesIO
            cropped = ImageCms.profileToProfile(
                cropped, ImageCms.ImageCmsProfile(BytesIO(embedded_profile)),
                ImageCms.createProfile("sRGB"), outputMode="RGBA",
            )
        rgba = np.asarray(cropped, dtype=np.float32) / 255.0
    alpha = rgba[:, :, 3:4]
    premult = np.concatenate((srgb_to_linear(rgba[:, :, :3]) * alpha, alpha), axis=2)
    if premult.shape[1::-1] != size:
        channels = [np.asarray(Image.fromarray(premult[:, :, c]).resize(size, Image.Resampling.LANCZOS)) for c in range(4)]
        premult = np.stack(channels, axis=2)
        # Lanczos can overshoot around alpha edges; keep a valid premultiplied range.
        premult[:, :, 3] = np.clip(premult[:, :, 3], 0, 1)
        premult[:, :, :3] = np.clip(premult[:, :, :3], 0, premult[:, :, 3:4])
    return premult


def feather_weights(tile):
    L, T, R, B = tile["box"]
    width, height = R - L, B - T
    left, top, right, bottom = tile["feather"]
    x, y = np.ones(width, np.float32), np.ones(height, np.float32)
    for axis, low, high in ((x, left, right), (y, top, bottom)):
        if low:
            u = (np.arange(low, dtype=np.float32) + 0.5) / low
            axis[:low] *= np.sin(u * np.pi / 2) ** 2
        if high:
            u = (np.arange(high, dtype=np.float32) + 0.5) / high
            axis[-high:] *= np.cos(u * np.pi / 2) ** 2
    return y[:, None] * x[None, :]


def checker_rgb(rgba, origin=(0, 0)):
    """Render transparency only for JPG previews; the composite retains alpha."""
    height, width = rgba.shape[:2]
    yy, xx = np.ogrid[:height, :width]
    checks = (((xx + origin[0]) // 16 + (yy + origin[1]) // 16) % 2).astype(np.float32)
    background = (0.78 + checks * 0.12)[:, :, None]
    rgb = rgba[:, :, :3].astype(np.float32) / 255
    alpha = rgba[:, :, 3:4].astype(np.float32) / 255
    return np.rint((rgb * alpha + background * (1 - alpha)) * 255).astype(np.uint8)


def compose(run):
    run = Path(run).resolve()
    no_existing_outputs(run)
    # Resource checks below precede any large allocation. Inputs are a reviewed plan.
    Image.MAX_IMAGE_PIXELS = None
    plan, tiles = preflight(run)
    width, height = plan["target_size"]
    seams = neighbors(tiles, width, height)
    pixels = width * height
    seam_pixels = sum((s["box"][2] - s["box"][0]) * (s["box"][3] - s["box"][1]) for s in seams)
    # Float RGBA + float geometric weights + RGBA byte raster + conservative exports.
    estimated_bytes = pixels * 30 + seam_pixels * 4 + 64 * 1024 * 1024
    free_bytes = shutil.disk_usage(run).free
    if free_bytes < estimated_bytes:
        raise OSError(f"Insufficient disk space: need about {estimated_bytes:,} bytes, have {free_bytes:,}")
    icc = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    report = {
        "schema_version": 1, "source_size": plan["source_size"], "target_size": [width, height],
        "tile_count": len(tiles), "no_mosaic_upscaling": True,
        "geometry_operations": ["reviewed crop", "native downsampling within 0.5% aspect tolerance", "coordinate placement", "overlap feathering"],
        "alpha": "premultiplied linear RGB blending; geometric coverage is independent of alpha",
        "color_profile": "sRGB; embedded tile profiles converted to sRGB, untagged tiles assumed sRGB",
        "disk_estimate_bytes": estimated_bytes,
        "visual_acceptance": "pending: agent must review the composite and every seam segment",
        "tiles": [],
    }
    with tempfile.TemporaryDirectory(prefix=".compose-", dir=run) as temporary:
        work = Path(temporary)
        acc = np.memmap(work / "acc.float32", mode="w+", dtype=np.float32, shape=(height, width, 4))
        weights = np.memmap(work / "weights.float32", mode="w+", dtype=np.float32, shape=(height, width))
        result = np.memmap(work / "result.rgba", mode="w+", dtype=np.uint8, shape=(height, width, 4))
        for y in range(0, height, STRIP_ROWS):
            acc[y:y + STRIP_ROWS] = 0
            weights[y:y + STRIP_ROWS] = 0
        for tile in tiles:
            L, T, R, B = tile["box"]
            premult = normalized_premultiplied(tile)
            weight = feather_weights(tile)
            acc[T:B, L:R] += premult * weight[:, :, None]
            weights[T:B, L:R] += weight
            report["tiles"].append({
                "id": tile["id"], "output": tile["output"], "native_size": list(tile["native_size"]),
                "crop": list(tile["crop"]), "effective_size": list(tile["effective_size"]),
                "target_box": list(tile["box"]), "aspect_error": tile["aspect_error"],
                "normalized_size": [R - L, B - T], "native_upscaling": False,
            })
            print(f"placed {tile['id']}", flush=True)
        missing, nonfinite, transparent, min_weight = 0, 0, 0, math.inf
        for y in range(0, height, STRIP_ROWS):
            block = weights[y:y + STRIP_ROWS]
            missing += int(np.count_nonzero(block <= 0))
            nonfinite += int(np.count_nonzero(~np.isfinite(block)))
            min_weight = min(min_weight, float(np.min(block)))
        if missing or nonfinite:
            raise ValueError(f"Canvas coverage failed: {missing} uncovered pixels; {nonfinite} invalid weights. No export written.")
        for y in range(0, height, STRIP_ROWS):
            values = acc[y:y + STRIP_ROWS] / weights[y:y + STRIP_ROWS, :, None]
            alpha = np.clip(values[:, :, 3:4], 0, 1)
            rgb = np.zeros_like(values[:, :, :3])
            np.divide(values[:, :, :3], alpha, out=rgb, where=alpha > 0)
            result[y:y + STRIP_ROWS, :, :3] = np.rint(linear_to_srgb(rgb) * 255).astype(np.uint8)
            result[y:y + STRIP_ROWS, :, 3:4] = np.rint(alpha * 255).astype(np.uint8)
            transparent += int(np.count_nonzero(result[y:y + STRIP_ROWS, :, 3] == 0))
        result.flush()
        image = Image.frombuffer("RGBA", (width, height), result, "raw", "RGBA", 0, 1)
        image.save(work / "composite.png", format="PNG", compress_level=6, icc_profile=icc)
        preview_scale = min(1.0, 1920 / width, 1920 / height)
        preview_size = (max(1, round(width * preview_scale)), max(1, round(height * preview_scale)))
        preview = image.resize(preview_size, Image.Resampling.LANCZOS, reducing_gap=3.0)
        Image.fromarray(checker_rgb(np.asarray(preview))).save(work / "preview.jpg", quality=94, subsampling=0, icc_profile=icc)
        preview.close()
        seam_dir = work / "seam_reviews"
        seam_dir.mkdir()
        seam_index = []
        for number, seam in enumerate(seams, 1):
            L, T, R, B = seam["box"]
            for top in range(T, B, SEAM_SEGMENT):
                for left in range(L, R, SEAM_SEGMENT):
                    right, bottom = min(left + SEAM_SEGMENT, R), min(top + SEAM_SEGMENT, B)
                    name = f"seam_{number:05d}_{left}_{top}.jpg"
                    patch = checker_rgb(result[top:bottom, left:right], (left, top))
                    Image.fromarray(patch).save(seam_dir / name, quality=97, subsampling=0, icc_profile=icc)
                    seam_index.append({"tiles": seam["tiles"], "kind": seam["kind"], "box": [left, top, right, bottom], "file": "seam_reviews/" + name, "pixel_scale": 1})
        (seam_dir / "index.json").write_text(json.dumps(seam_index, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        image.close()
        del image, result, acc, weights, block
        gc.collect()
        with Image.open(work / "composite.png") as decoded:
            decoded.load()
            if decoded.size != (width, height) or decoded.mode != "RGBA" or not decoded.info.get("icc_profile"):
                raise ValueError("Final export decode/size/alpha/profile check failed")
            report["final_decoded_size"] = list(decoded.size)
            report["final_mode"] = decoded.mode
        report["coverage"] = {"complete": True, "covered_pixels": pixels, "uncovered_pixels": 0, "invalid_weights": 0, "minimum_weight": min_weight, "transparent_pixels": transparent}
        report["seam_pairs"] = len(seams)
        report["seam_segments"] = len(seam_index)
        report["seam_index"] = "seam_reviews/index.json"
        (work / "compose_report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        no_existing_outputs(run)
        # Publish composite last: its presence means all mechanical checks passed.
        for name in ("preview.jpg", "compose_report.json", "seam_reviews", "composite.png"):
            os.rename(work / name, run / name)
    print(json.dumps({"composite": str(run / "composite.png"), "size": [width, height], "tiles": len(tiles), "seam_segments": len(seam_index), "visual_acceptance": "pending"}, ensure_ascii=False), flush=True)
    return report


def self_test():
    def fixture(folder, boxes, colors, width=4, height=2, scale=1):
        folder.mkdir()
        Image.new("RGBA", (width, height), (0, 0, 0, 0)).save(folder / "source.png")
        tiles, states = [], {}
        for index, (box, color) in enumerate(zip(boxes, colors)):
            tid = f"t{index}"
            L, T, R, B = [round(v * scale) for v in box]
            size = (R - L, B - T)
            Image.new("RGBA", size, color).save(folder / f"{tid}.png")
            (folder / f"{tid}.txt").write_text("synthetic test", encoding="utf-8")
            tiles.append({"id": tid, "box": box, "target_box": [L, T, R, B], "input": f"{tid}.png", "prompt": f"{tid}.txt", "materials": [], "objects": [], "mode": "generate"})
            states[tid] = {"status": "qa_passed", "output": f"{tid}.png", "native_size": list(size), "placement": {"crop": [0, 0, *size]}, "visual_review": {"synthetic": True}}
        plan = {"schema_version": 1, "source": "source.png", "source_size": [width, height], "target_size": [round(width * scale), round(height * scale)], "scale": scale, "tiles": tiles}
        (folder / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
        (folder / "state.json").write_text(json.dumps({"tiles": states}), encoding="utf-8")
        return plan, states

    def rejected(folder, expected):
        try:
            compose(folder)
        except (ValueError, FileExistsError) as exc:
            assert expected in str(exc), (expected, str(exc))
        else:
            raise AssertionError(f"Expected rejection: {expected}")
        assert not (folder / "composite.png").exists()

    with tempfile.TemporaryDirectory(prefix="compose-self-test-") as root:
        root = Path(root)
        run = root / "alpha"
        fixture(run, [[0, 0, 3, 2], [1, 0, 4, 2]], [(255, 0, 0, 255), (0, 0, 255, 0)])
        report = compose(run)
        rgba = np.asarray(Image.open(run / "composite.png"))
        assert rgba.shape == (2, 4, 4)
        assert np.all(rgba[:, 1:3, 0] == 255) and np.all(rgba[:, 1:3, 1:3] == 0), "transparent RGB contaminated the overlap"
        assert np.all(rgba[:, -1, 3] == 0) and report["coverage"]["complete"]
        assert report["coverage"]["transparent_pixels"] == 2
        try:
            compose(run)
        except FileExistsError:
            pass
        else:
            raise AssertionError("Existing composite was overwritten")
        missing = root / "missing"
        _, states = fixture(missing, [[0, 0, 2, 2], [2, 0, 4, 2]], [(1, 2, 3, 255)] * 2)
        states["t1"]["status"] = "generated"
        (missing / "state.json").write_text(json.dumps({"tiles": states}), encoding="utf-8")
        rejected(missing, "only qa_passed")
        absent = root / "absent"
        fixture(absent, [[0, 0, 4, 2]], [(1, 2, 3, 255)])
        (absent / "t0.png").unlink()
        rejected(absent, "missing file")
        gap = root / "gap"
        fixture(gap, [[0, 0, 1, 2], [2, 0, 4, 2]], [(1, 2, 3, 255)] * 2)
        rejected(gap, "uncovered pixels")
        sized = root / "size"
        fixture(sized, [[0, 0, 4, 2]], [(12, 34, 56, 200)], scale=2)
        assert compose(sized)["final_decoded_size"] == [8, 4]
        bad = root / "aspect"
        _, states = fixture(bad, [[0, 0, 4, 2]], [(1, 2, 3, 255)])
        Image.new("RGBA", (8, 3)).save(bad / "t0.png")
        states["t0"].update(native_size=[8, 3], placement={"crop": [0, 0, 8, 3]})
        (bad / "state.json").write_text(json.dumps({"tiles": states}), encoding="utf-8")
        rejected(bad, "crop aspect error")
        small = root / "small"
        _, states = fixture(small, [[0, 0, 4, 2]], [(1, 2, 3, 255)])
        Image.new("RGBA", (2, 1)).save(small / "t0.png")
        states["t0"].update(native_size=[2, 1], placement={"crop": [0, 0, 2, 1]})
        (small / "state.json").write_text(json.dumps({"tiles": states}), encoding="utf-8")
        rejected(small, "below target")
    print("PASS: alpha, transparent coverage, size, missing QA, missing file, gaps, aspect, native minimum, overwrite protection")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", nargs="?")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        self_test()
    elif args.run_dir:
        compose(args.run_dir)
    else:
        parser.error("RUN_DIR or --self-test is required")
