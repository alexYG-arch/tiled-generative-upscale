#!/usr/bin/env python3
"""Plan true native-resolution tiled generation; record reviews and package delivery.

This helper never calls a generation provider or grants visual acceptance itself.
Python 3.10+, Pillow, NumPy. See ../SKILL.md for the agent-driven workflow.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
from io import BytesIO
import json
import math
from pathlib import Path
import shutil
import zipfile

import numpy as np
from PIL import Image, ImageCms, ImageDraw, ImageOps

from compose import ASPECT_TOLERANCE, local_path, preflight


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def now():
    return datetime.now(timezone.utc).isoformat()


def size_arg(value):
    try:
        values = tuple(int(v) for v in value.lower().split("x"))
        if len(values) != 2 or min(values) < 1:
            raise ValueError()
        return values
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Use positive WIDTHxHEIGHT") from exc


def box_arg(value):
    try:
        values = [int(v) for v in value.split(",")]
        if len(values) != 4:
            raise ValueError()
        return values
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Use left,top,right,bottom") from exc


def intersect(a, b):
    return min(a[2], b[2]) > max(a[0], b[0]) and min(a[3], b[3]) > max(a[1], b[1])


def validate_analysis(analysis, size):
    if analysis.get("inspected") is not True or analysis.get("coordinate_system") != "exif_oriented_source_pixels":
        raise ValueError("Inspect the EXIF-oriented image first; record inspected=true and coordinate_system=exif_oriented_source_pixels")
    for field in ("summary", "style"):
        if not isinstance(analysis.get(field), str) or not analysis[field].strip():
            raise ValueError(f"analysis.{field} must describe the inspected image")
    if not isinstance(analysis.get("palette"), list) or not analysis["palette"] or not all(isinstance(v, str) for v in analysis["palette"]):
        raise ValueError("analysis.palette must be a nonempty string list")
    materials = analysis.get("materials")
    if not isinstance(materials, dict) or not materials or not all(isinstance(v, str) and v.strip() for v in materials.values()):
        raise ValueError("analysis.materials maps stable material IDs to shared detail instructions")
    regions = analysis.get("regions")
    if not isinstance(regions, list) or not regions:
        raise ValueError("analysis.regions must identify content extents, including background")
    ids = set()
    for region in regions:
        rid = region.get("id")
        if not isinstance(rid, str) or not rid or rid in ids:
            raise ValueError("Region IDs must be nonempty and unique")
        ids.add(rid)
        box = region.get("box")
        if not isinstance(box, list) or len(box) != 4 or any(type(v) is not int for v in box):
            raise ValueError(f"{rid}: box must contain four integer coordinates")
        l, t, r, b = box
        if not (0 <= l < r <= size[0] and 0 <= t < b <= size[1]):
            raise ValueError(f"{rid}: region outside oriented source")
        if region.get("material") not in materials:
            raise ValueError(f"{rid}: unknown material")
        if type(region.get("seam_avoid", False)) is not bool:
            raise ValueError(f"{rid}: seam_avoid must be boolean")
    if not isinstance(analysis.get("objects", {}), dict):
        raise ValueError("analysis.objects must be a dictionary when provided")


def axis_starts(length, crop, overlap):
    if length <= crop:
        return [0]
    count = 1 + math.ceil((length - crop) / (crop - overlap))
    return [round(i * (length - crop) / (count - 1)) for i in range(count)]


def adapt_axis(starts, crop, overlap, length, axis, regions, edge):
    """Shift interior grid lines inside overlap constraints; semantic input is human/agent supplied."""
    starts = list(starts)
    radius = max(1, round(crop * 0.12))
    def penalty(start, original):
        score = abs(start - original) / max(1, crop)
        for boundary in (start, start + crop):
            if boundary in (0, length):
                continue
            score += float(edge[min(length - 1, boundary)])
            for region in regions:
                if region.get("seam_avoid") and region["box"][axis] < boundary < region["box"][axis + 2]:
                    extent = region["box"][3 - axis] - region["box"][1 - axis]
                    score += 20 * (1 + extent / max(length, 1))
        return score
    for i in range(1, len(starts) - 1):
        original = starts[i]
        low = max(starts[i - 1] + 1, starts[i + 1] - (crop - overlap), original - radius)
        high = min(starts[i + 1] - 1, starts[i - 1] + crop - overlap, original + radius)
        if low <= high:
            starts[i] = min(range(low, high + 1), key=lambda v: (penalty(v, original), abs(v - original)))
    return starts


def normalized_source(path):
    with Image.open(path) as raw:
        if getattr(raw, "n_frames", 1) != 1:
            raise ValueError("Animated/multipage sources require an explicitly selected frame")
        image = ImageOps.exif_transpose(raw)
        embedded = raw.info.get("icc_profile")
        # Convert in the profile's original mode, then retain alpha separately.
        alpha = image.convert("RGBA").getchannel("A")
        if embedded:
            profile = ImageCms.ImageCmsProfile(BytesIO(embedded))
            color = image.convert("L") if image.mode == "LA" else (image.convert("RGB") if image.mode in ("RGBA", "P") else image)
            image = ImageCms.profileToProfile(color, profile, ImageCms.createProfile("sRGB"), outputMode="RGB")
        else:
            image = image.convert("RGB")
        image.putalpha(alpha)
        image.load()
        return image.copy()


def tile_prompt(tile, analysis, source_size):
    l, t, r, b = tile["box"]
    L, T, R, B = tile["target_box"]
    sections = [
        "TASK: Reconstruct THIS EXACT SOURCE CROP at genuinely higher native detail. The first reference is the crop to edit. A whole-image reference, if present, is context only; never return the whole image.",
        f"Source crop [{l},{t},{r},{b}) in an oriented {source_size[0]}x{source_size[1]} image. Required usable output at least {R-L}x{B-T} pixels. Preserve the crop aspect ratio {(r-l)}:{(b-t)}, framing, boundary crossings and all object positions.",
        "The requested resolution is a target, not permission to interpolate. Reconstruct believable fine detail inside existing silhouettes. No new objects, logos, letters, architecture, ornaments, lighting directions, camera changes, crop shifts or stylization. Do not complete partially clipped objects outside the crop.",
        "LOCKED GLOBAL CONTENT: " + analysis["summary"],
        "LOCKED STYLE: " + analysis["style"],
        "LOCKED PALETTE: " + "; ".join(analysis["palette"]),
        "SHARED MATERIAL DETAIL RULES (reuse verbatim across all tiles):",
    ]
    sections += [f"[{mid}] {analysis['materials'][mid]}" for mid in tile["materials"]]
    if tile["objects"]:
        sections.append("SHARED OBJECT RULES:")
        sections += [f"[{oid}] {analysis.get('objects', {}).get(oid, 'Preserve the same object identity, shape and scale across crops.')}" for oid in tile["objects"]]
    sections.append("LOCAL CONTENT:")
    sections += [f"[{region['id']}] {region.get('kind', 'content')}: {region.get('detail', 'Preserve the visible source content.')}" for region in analysis["regions"] if intersect(tile["box"], region["box"])]
    sections += [
        "SEAMS: Keep color, perspective, texture scale, contour endpoints and lighting continuous through all four edges and overlapping areas. No borders, vignettes, edge fading or artificial frame. Do not move an object to improve composition.",
        "TEXT: Preserve only visible source lettering, exactly. Add no guessed words or labels. If the source is illegible, preserve its contour instead of inventing text. Match existing transparency; do not paint a checkerboard into the image.",
        "OUTPUT: One crop image, no comparison, grid, annotations, captions or surrounding padding.",
    ]
    return "\n\n".join(sections) + "\n"


def make_plan(args):
    if not math.isfinite(args.scale) or args.scale <= 1:
        raise ValueError("Enlargement scale must be finite and greater than 1; interpreted per axis")
    if not 0 < args.overlap < 0.5 or not 0 <= args.margin < 0.25 or args.max_attempts < 1:
        raise ValueError("Use 0<overlap<0.5, 0<=margin<0.25, max_attempts>=1")
    run = args.run.resolve()
    if run.exists() and any(run.iterdir()):
        raise FileExistsError("Use a new/empty RUN_DIR; planning never resets an existing run")
    image = normalized_source(args.image)
    sw, sh = image.size
    analysis = read(args.analysis)
    validate_analysis(analysis, image.size)
    native = args.native
    usable = [math.floor(v * (1 - args.margin)) for v in native]
    cw, ch = min(sw, math.floor(usable[0] / args.scale)), min(sh, math.floor(usable[1] / args.scale))
    if min(cw, ch) < 2:
        raise ValueError("Native budget is too small for the requested scale")
    ow, oh = max(1, math.ceil(cw * args.overlap)), max(1, math.ceil(ch * args.overlap))
    xs, ys = axis_starts(sw, cw, ow), axis_starts(sh, ch, oh)
    gray = np.asarray(image.convert("L"), dtype=np.float32)
    ex = np.mean(np.abs(np.diff(gray, axis=1, prepend=gray[:, :1])), axis=0) / 255
    ey = np.mean(np.abs(np.diff(gray, axis=0, prepend=gray[:1])), axis=1) / 255
    initial = {"x": xs[:], "y": ys[:]}
    xs = adapt_axis(xs, cw, ow, sw, 0, analysis["regions"], ex)
    ys = adapt_axis(ys, ch, oh, sh, 1, analysis["regions"], ey)
    tiles = []
    for row, top in enumerate(ys):
        for col, left in enumerate(xs):
            tid = f"r{row:03d}c{col:03d}"
            box = [left, top, left + cw, top + ch]
            target = [round(v * args.scale) for v in box]
            if target[2] - target[0] > usable[0] or target[3] - target[1] > usable[1]:
                raise ValueError("Rounded coordinates exceed usable native budget; reduce budget or increase margin")
            regions = [r for r in analysis["regions"] if intersect(box, r["box"])]
            tiles.append({"id": tid, "row": row, "col": col, "box": box, "target_box": target,
                          "input": f"tiles/{tid}.png", "prompt": f"prompts/{tid}.txt", "mode": "generate",
                          "materials": sorted({r["material"] for r in regions}),
                          "objects": sorted({r["object"] for r in regions if r.get("object")}),
                          "risk": sum(bool(r.get("seam_avoid")) for r in regions)})
    # A high-risk pilot + an adjacent crop tests boundaries; then cover every material.
    first = max(tiles, key=lambda t: (t["risk"], len(t["materials"])))
    pilots = [first["id"]]
    adjacent = [t for t in tiles if t["id"] != first["id"] and abs(t["row"] - first["row"]) + abs(t["col"] - first["col"]) == 1]
    if adjacent:
        pilots.append(max(adjacent, key=lambda t: t["risk"])["id"])
    seen = {m for t in tiles if t["id"] in pilots for m in t["materials"]}
    for tile in sorted(tiles, key=lambda t: -t["risk"]):
        if set(tile["materials"]) - seen:
            pilots.append(tile["id"])
            seen.update(tile["materials"])
    snake = [t["id"] for t in sorted(tiles, key=lambda t: (t["row"], t["col"] if t["row"] % 2 == 0 else -t["col"]))]
    sequence = pilots + [tid for tid in snake if tid not in pilots]
    warnings = []
    for region in analysis["regions"]:
        if region.get("seam_avoid"):
            box = region["box"]
            if box[2]-box[0] > cw or box[3]-box[1] > ch:
                warnings.append(f"{region['id']}: protected object is larger than a native tile; crossings need object-level continuity review")
    target_size = [round(sw * args.scale), round(sh * args.scale)]
    estimate = math.prod(target_size) * 40
    run.mkdir(parents=True, exist_ok=True)
    for directory in ("tiles", "prompts", "generated", "receipts", "reviews"):
        (run / directory).mkdir()
    icc = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    image.save(run / "source.png", icc_profile=icc)
    shutil.copy2(args.image, run / ("original" + args.image.suffix.lower()))
    write(run / "analysis.json", analysis)
    for tile in tiles:
        image.crop(tile["box"]).save(run / tile["input"], icc_profile=icc)
        (run / tile["prompt"]).write_text(tile_prompt(tile, analysis, [sw, sh]), encoding="utf-8")
    plan = {"schema_version": 1, "created_at": now(), "source": "source.png", "source_size": [sw, sh],
            "target_size": target_size, "scale": args.scale, "scale_meaning": "both dimensions; pixel count multiplied by scale squared",
            "native_budget": list(native), "native_budget_basis": args.native_basis, "margin": args.margin, "usable_native": usable,
            "crop_size": [cw, ch], "minimum_overlap": [ow, oh], "grid": [len(xs), len(ys)], "initial_starts": initial,
            "content_adjusted_starts": {"x": xs, "y": ys}, "tiles": tiles, "pilot_ids": pilots, "sequence": sequence,
            "max_attempts": args.max_attempts, "warnings": warnings, "rough_scratch_bytes": estimate,
            "disk_free_bytes": shutil.disk_usage(run).free, "plan_review": "pending"}
    write(run / "plan.json", plan)
    write(run / "state.json", {"schema_version": 1, "tiles": {t["id"]: {"status": "planned", "attempts": []} for t in tiles}})
    factor = min(1, 1800 / max(sw, sh))
    canvas = image.convert("RGB").resize((max(1, round(sw*factor)), max(1, round(sh*factor))))
    draw = ImageDraw.Draw(canvas)
    for region in analysis["regions"]:
        if region.get("seam_avoid"):
            draw.rectangle([round(v*factor) for v in region["box"]], outline="#ff44cc", width=3)
    for tile in tiles:
        l, t, r, b = [round(v*factor) for v in tile["box"]]
        draw.rectangle((l, t, r, b), outline="#00ffcc", width=1)
        draw.text((l+2, t+2), tile["id"][1:4]+","+tile["id"][-3:], fill="white", stroke_width=1, stroke_fill="black")
    canvas.save(run / "tile_map.jpg", quality=95)
    print(json.dumps({"run": str(run), "target_size": target_size, "tiles": len(tiles), "grid": plan["grid"], "pilots": pilots, "warnings": warnings, "plan_review": "pending"}, ensure_ascii=False))
    return plan


def checked_review(review, fields):
    if not isinstance(review.get("notes"), str) or not review["notes"].strip():
        raise ValueError("A review requires concrete notes from inspected images")
    checks = review.get("checks", {})
    for field in fields:
        value = checks.get(field)
        if value is True:
            continue
        if value is None and isinstance(review.get("not_applicable", {}).get(field), str) and review["not_applicable"][field].strip():
            continue
        raise ValueError(f"Review failed or missing: {field}; use true, or null with a not_applicable explanation")


def approve_plan(args):
    run = args.run.resolve()
    plan = read(run / "plan.json")
    review = read(args.review_json)
    checked_review(review, ("image_content", "tile_extents", "critical_objects", "shared_prompts", "pilot_sequence"))
    if review.get("warnings_acknowledged") != plan["warnings"]:
        raise ValueError("Copy all plan warnings into warnings_acknowledged and explain handling in notes")
    write(run / "reviews/plan.json", review)
    plan["plan_review"] = "passed"
    write(run / "plan.json", plan)
    print("Plan review recorded. Generate pilots in plan.sequence order.")


def get_tile(run, tid):
    plan, state = read(run / "plan.json"), read(run / "state.json")
    tile = next((t for t in plan["tiles"] if t["id"] == tid), None)
    if tile is None:
        raise ValueError(f"Unknown tile: {tid}")
    return plan, state, tile, state["tiles"][tid]


def register(args):
    run = args.run.resolve()
    plan, state, tile, entry = get_tile(run, args.tile)
    if plan["plan_review"] != "passed":
        raise ValueError("Inspect source, tile_map and prompts; approve-plan before generation")
    if args.tile not in plan["pilot_ids"] and any(state["tiles"][tid]["status"] != "qa_passed" for tid in plan["pilot_ids"]):
        raise ValueError("All pilots must pass native geometry and visual review before batch generation")
    if any((run / name).exists() for name in ("composite.png", "compose_report.json", "final_qa.json")):
        raise ValueError("Archive old composite/report/seam_reviews/final_qa before revising a tile")
    attempts = entry.setdefault("attempts", [])
    if len(attempts) >= plan["max_attempts"]:
        raise ValueError("Attempt limit reached; record blocker and replan instead of repeating silently")
    receipt = read(args.receipt)
    if receipt.get("tool") != "image_gen__imagegen" or not receipt.get("output_path") or not receipt.get("called_at"):
        raise ValueError("Receipt requires tool=image_gen__imagegen, called_at and actual output_path")
    if Path(receipt["output_path"]).expanduser().resolve() != args.image.expanduser().resolve():
        raise ValueError("Receipt output_path must identify the supplied original generator output")
    if args.crop and not args.placement_note:
        raise ValueError("A placement crop requires evidence that the full intended source frame survives")
    with Image.open(args.image) as image:
        image.load()
        native = list(image.size)
        fmt = image.format
        has_alpha = "A" in image.getbands() or "transparency" in image.info
    crop = args.crop or [0, 0, *native]
    x0, y0, x1, y1 = crop
    errors = []
    tw, th = tile["target_box"][2]-tile["target_box"][0], tile["target_box"][3]-tile["target_box"][1]
    if not (0 <= x0 < x1 <= native[0] and 0 <= y0 < y1 <= native[1]):
        errors.append("placement crop outside native output")
    else:
        cw, ch = x1-x0, y1-y0
        if cw < tw or ch < th:
            errors.append(f"effective native {cw}x{ch} below target {tw}x{th}")
        error = abs((cw/ch)/(tw/th)-1)
        if error > ASPECT_TOLERANCE + 1e-12:
            errors.append(f"aspect error {error:.3%} exceeds 0.5%; no stretching")
    number = len(attempts) + 1
    folder = run / "generated" / args.tile
    folder.mkdir(exist_ok=True)
    suffix = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp", "TIFF": ".tif"}.get(fmt, args.image.suffix)
    output = f"generated/{args.tile}/attempt_{number:02d}{suffix}"
    if (run / output).exists():
        raise FileExistsError("Attempt output exists; inspect prior interrupted write before resuming")
    shutil.copy2(args.image, run / output)
    prompt_path = folder / f"attempt_{number:02d}.txt"
    shutil.copy2(args.prompt or run / tile["prompt"], prompt_path)
    receipt_path = f"receipts/{args.tile}_{number:02d}.json"
    write(run / receipt_path, receipt)
    record = {"number": number, "output": output, "prompt": str(prompt_path.relative_to(run)), "receipt": receipt_path,
              "native_size": native, "placement": {"crop": crop, "note": args.placement_note or "Full native frame"},
              "has_alpha_channel": has_alpha, "geometry_errors": errors, "registered_at": now()}
    attempts.append(record)
    entry.update({"status": "qa_failed" if errors else "generated", "output": output, "native_size": native,
                  "placement": record["placement"], "geometry_errors": errors})
    entry.pop("visual_review", None)
    write(run / "state.json", state)
    print(json.dumps({"tile": args.tile, "status": entry["status"], "native_size": native, "errors": errors}, ensure_ascii=False))
    return not errors


def review_tile(args):
    run = args.run.resolve()
    _, state, tile, entry = get_tile(run, args.tile)
    if entry["status"] not in ("generated", "qa_passed", "qa_failed") or entry.get("geometry_errors") or not entry.get("output"):
        raise ValueError("Only a mechanically valid generated tile can receive a passing visual review")
    review = read(args.review_json)
    if review.get("output") != entry["output"]:
        raise ValueError("Review output must match the current attempt path")
    try:
        checked_review(review, ("composition", "detail", "geometry", "color", "text", "borders", "alpha"))
    except ValueError:
        entry["status"] = "qa_failed"
        entry["visual_review"] = review
        write(run / "state.json", state)
        write(run / f"reviews/{args.tile}_{len(entry['attempts']):02d}.json", review)
        raise
    entry["status"] = "qa_passed"
    entry["visual_review"] = review
    entry["attempts"][-1]["visual_review"] = review
    write(run / f"reviews/{args.tile}_{len(entry['attempts']):02d}.json", review)
    write(run / "state.json", state)
    print(f"{tile['id']}: qa_passed (recorded visual assessment, not inferred from size)")


def record_failure(args):
    run = args.run.resolve()
    plan, state, _, entry = get_tile(run, args.tile)
    if plan["plan_review"] != "passed":
        raise ValueError("Plan review must precede generation attempts")
    if any((run / name).exists() for name in ("composite.png", "compose_report.json", "final_qa.json")):
        raise ValueError("Archive old exports before recording a new attempt")
    attempts = entry.setdefault("attempts", [])
    if len(attempts) >= plan["max_attempts"]:
        raise ValueError("Attempt limit reached")
    receipt = read(args.receipt)
    if receipt.get("tool") != "image_gen__imagegen" or not receipt.get("called_at") or not receipt.get("error"):
        raise ValueError("Failure receipt requires tool=image_gen__imagegen, called_at and actual error")
    number = len(attempts) + 1
    receipt_path = f"receipts/{args.tile}_{number:02d}.json"
    write(run / receipt_path, receipt)
    attempts.append({"number": number, "status": "tool_failed", "receipt": receipt_path, "registered_at": now()})
    entry.update({"status": "tool_failed"})
    for key in ("output", "native_size", "placement", "geometry_errors", "visual_review"):
        entry.pop(key, None)
    write(run / "state.json", state)
    print(f"{args.tile}: tool_failed; attempt {number}/{plan['max_attempts']} recorded")


def status(args):
    run = args.run.resolve()
    plan, state = read(run / "plan.json"), read(run / "state.json")
    counts = Counter(t["status"] for t in state["tiles"].values())
    remaining = [tid for tid in plan["sequence"] if state["tiles"][tid]["status"] != "qa_passed"]
    print(json.dumps({"total": len(plan["tiles"]), "counts": dict(counts), "next": remaining[:12], "plan_review": plan["plan_review"], "delivery": "accepted" if (run / "final_qa.json").exists() else "pending"}, ensure_ascii=False, indent=2))


def finish(args):
    run = args.run.resolve()
    plan, _ = preflight(run)
    report = read(run / "compose_report.json")
    review = read(args.review_json)
    checked_review(review, ("whole_image", "critical_objects", "all_seams", "text", "color", "alpha", "detail_comparison"))
    seam_entries = read(run / "seam_reviews/index.json")
    seam_files = {s["file"] for s in seam_entries}
    if len(seam_files) != len(seam_entries) or len(seam_entries) != report.get("seam_segments"):
        raise ValueError("Seam index is incomplete or contains duplicate files")
    for entry in seam_entries:
        path = local_path(run, entry["file"], "seam review image")
        with Image.open(path) as image:
            image.load()
            if image.size != (entry["box"][2]-entry["box"][0], entry["box"][3]-entry["box"][1]):
                raise ValueError("Seam preview dimensions disagree with index")
    reviewed = review.get("seams", {})
    if set(reviewed) != seam_files or any(not isinstance(v, dict) or v.get("passed") is not True or not isinstance(v.get("notes"), str) or not v["notes"].strip() for v in reviewed.values()):
        raise ValueError("Every seam segment needs an explicit passed=true assessment and a concrete note; no missing/extra segments")
    if report.get("coverage", {}).get("complete") is not True or report.get("coverage", {}).get("uncovered_pixels") != 0:
        raise ValueError("Composite coverage is not complete")
    if report.get("target_size") != plan["target_size"] or report.get("tile_count") != len(plan["tiles"]):
        raise ValueError("Composition report does not match the current plan")
    current = read(run / "state.json")["tiles"]
    reported_tiles = {tile["id"]: tile for tile in report.get("tiles", [])}
    if set(reported_tiles) != {tile["id"] for tile in plan["tiles"]}:
        raise ValueError("Composition report has different tiles")
    for tile in plan["tiles"]:
        recorded, active = reported_tiles[tile["id"]], current[tile["id"]]
        if (recorded["output"] != active["output"] or recorded["crop"] != active["placement"]["crop"]
                or recorded["target_box"] != tile["target_box"]):
            raise ValueError("Composition report is stale; recompose and repeat final review")
    Image.MAX_IMAGE_PIXELS = None
    with Image.open(run / "composite.png") as image:
        image.load()
        if list(image.size) != plan["target_size"]:
            raise ValueError("Final decoded dimensions do not match scale")
    if not isinstance(review.get("limitations"), list):
        raise ValueError("Final review must list limitations (an empty list is allowed)")
    out = args.out.resolve()
    if out == run or run in out.parents or out in run.parents:
        raise ValueError("Delivery directory must be separate from RUN_DIR")
    if out.exists() and any(out.iterdir()):
        raise FileExistsError("Use a new/empty delivery directory")
    out.mkdir(parents=True, exist_ok=True)
    accepted = {"accepted_at": now(), "target_size": plan["target_size"], "tile_count": len(plan["tiles"]), "visual_review": review,
                "scope": "Mechanical checks plus recorded agent visual review; generative details are reconstructed, not recovered ground truth"}
    write(out / "final_qa.json", accepted)
    for name in ("composite.png", "preview.jpg", "plan.json", "compose_report.json"):
        shutil.copy2(run / name, out / name)
    # Keep generated tiles, prompts and receipts reviewable without duplicating the huge composite.
    with zipfile.ZipFile(out / "generation_records.zip", "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in ("plan.json", "state.json", "analysis.json", "compose_report.json", "tile_map.jpg", "source.png"):
            archive.write(run / name, name)
        archive.write(out / "final_qa.json", "final_qa.json")
        for folder in ("tiles", "prompts", "generated", "receipts", "reviews", "seam_reviews"):
            for path in sorted((run / folder).rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(run))
    (out / "交付说明.txt").write_text(
        f"原图 {plan['source_size'][0]}×{plan['source_size'][1]}；宽高各放大 {plan['scale']:g} 倍。\n"
        f"成图 {plan['target_size'][0]}×{plan['target_size'][1]}；{len(plan['tiles'])} 片分别生成并通过尺寸与已记录的视觉检查。\n"
        "composite.png 为完整成图；preview.jpg 为缩小预览，透明区使用棋盘格显示。\n"
        "generation_records.zip 保留切片、Prompt、工具记录和逐片/接缝检查。没有最终整图上采样步骤。\n"
        "新增细节为生成式重建，不能解释为恢复原图不存在的真实信息。\n"
        "已知限制：" + ("；".join(str(v) for v in review["limitations"]) or "本次检查未记录额外限制") + "\n", encoding="utf-8")
    write(out / "delivery_complete.json", {"completed_at": now(), "files": sorted(p.name for p in out.iterdir())})
    write(run / "final_qa.json", accepted)
    print(json.dumps({"delivery": str(out), "target_size": plan["target_size"], "tiles": len(plan["tiles"])}, ensure_ascii=False))


def parser():
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)
    p = commands.add_parser("plan", help="Inspect image first; compute crops/prompts/order from content JSON")
    p.add_argument("--image", type=Path, required=True)
    p.add_argument("--scale", type=float, required=True)
    p.add_argument("--native", type=size_arg, required=True, help="Conservative native generator size, WIDTHxHEIGHT")
    p.add_argument("--native-basis", default="provisional; must be confirmed by pilot outputs")
    p.add_argument("--analysis", type=Path, required=True)
    p.add_argument("--run", type=Path, required=True)
    p.add_argument("--overlap", type=float, default=0.15)
    p.add_argument("--margin", type=float, default=0.03)
    p.add_argument("--max-attempts", type=int, default=3)
    p.set_defaults(func=make_plan)
    p = commands.add_parser("approve-plan", help="Record agent review before generation")
    p.add_argument("run", type=Path)
    p.add_argument("--review-json", type=Path, required=True)
    p.set_defaults(func=approve_plan)
    p = commands.add_parser("register", help="Copy actual original generator output and check native geometry")
    p.add_argument("run", type=Path)
    p.add_argument("--tile", required=True)
    p.add_argument("--image", type=Path, required=True)
    p.add_argument("--receipt", type=Path, required=True)
    p.add_argument("--prompt", type=Path, help="Exact prompt used when revising the planned prompt")
    p.add_argument("--crop", type=box_arg)
    p.add_argument("--placement-note")
    p.set_defaults(func=register)
    p = commands.add_parser("failure", help="Record a failed generator call with no image; counts toward attempt limit")
    p.add_argument("run", type=Path)
    p.add_argument("--tile", required=True)
    p.add_argument("--receipt", type=Path, required=True)
    p.set_defaults(func=record_failure)
    p = commands.add_parser("review", help="Record per-tile visual assessment of the current output")
    p.add_argument("run", type=Path)
    p.add_argument("--tile", required=True)
    p.add_argument("--review-json", type=Path, required=True)
    p.set_defaults(func=review_tile)
    p = commands.add_parser("status")
    p.add_argument("run", type=Path)
    p.set_defaults(func=status)
    p = commands.add_parser("finish", help="Validate complete seam review and package a separate delivery")
    p.add_argument("run", type=Path)
    p.add_argument("--review-json", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.set_defaults(func=finish)
    return root


if __name__ == "__main__":
    args = parser().parse_args()
    try:
        result = args.func(args)
    except (ValueError, FileExistsError, OSError) as error:
        raise SystemExit(f"ERROR: {error}")
    if result is False:
        raise SystemExit(2)
