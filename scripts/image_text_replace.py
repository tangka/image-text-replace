#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib import request

from PIL import Image, ImageDraw, ImageFont


@dataclass
class Region:
    id: str
    text: str
    replacement: str
    box: tuple[int, int, int, int]
    keep: bool = False
    fill: str = "auto"
    stroke: str = "auto"
    align: str = "center"

    @classmethod
    def from_dict(cls, data: dict[str, Any], idx: int) -> "Region":
        box = data.get("box")
        if not isinstance(box, list) or len(box) != 4:
            raise SystemExit(f"region {idx} must have box [x, y, width, height]")
        return cls(
            id=str(data.get("id") or f"r{idx + 1:03d}"),
            text=str(data.get("text") or ""),
            replacement=str(data.get("replacement") or ""),
            box=tuple(int(v) for v in box),  # type: ignore[arg-type]
            keep=bool(data.get("keep", False)),
            fill=str(data.get("fill") or "auto"),
            stroke=str(data.get("stroke") or "auto"),
            align=str(data.get("align") or "center"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "text": self.text,
            "replacement": self.replacement,
            "box": list(self.box),
            "keep": self.keep,
            "fill": self.fill,
            "stroke": self.stroke,
            "align": self.align,
        }


def run(cmd: list[str], *, capture: bool = False) -> subprocess.CompletedProcess[str]:
    if not capture:
        print("+ " + " ".join(str(part) for part in cmd), file=sys.stderr)
    return subprocess.run(cmd, check=True, text=True, capture_output=capture)


def slug(path: Path) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in path.stem)


def output_paths(image: Path, out_dir: Path, prefix: str | None) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    base = prefix or slug(image)
    return {
        "ocr": out_dir / f"{base}.ocr.json",
        "regions": out_dir / f"{base}.regions.json",
        "mask": out_dir / f"{base}.mask.png",
        "clean": out_dir / f"{base}.clean.png",
        "final": out_dir / f"{base}.replaced.png",
        "compare": out_dir / f"{base}.compare.png",
        "qa": out_dir / f"{base}.qa.png",
    }


def parse_tesseract_tsv(tsv: str, min_conf: float) -> list[Region]:
    rows = csv.DictReader(tsv.splitlines(), delimiter="\t")
    lines: dict[tuple[str, str, str], list[dict[str, str]]] = {}
    for row in rows:
        text = (row.get("text") or "").strip()
        if not text:
            continue
        try:
            conf = float(row.get("conf") or -1)
        except ValueError:
            conf = -1
        if conf < min_conf:
            continue
        key = (row.get("block_num") or "0", row.get("par_num") or "0", row.get("line_num") or "0")
        lines.setdefault(key, []).append(row)

    regions: list[Region] = []
    for idx, words in enumerate(lines.values(), start=1):
        lefts = [int(w["left"]) for w in words]
        tops = [int(w["top"]) for w in words]
        rights = [int(w["left"]) + int(w["width"]) for w in words]
        bottoms = [int(w["top"]) + int(w["height"]) for w in words]
        text = " ".join((w.get("text") or "").strip() for w in words if (w.get("text") or "").strip())
        x0, y0, x1, y1 = min(lefts), min(tops), max(rights), max(bottoms)
        regions.append(Region(f"r{idx:03d}", text, "", (x0, y0, x1 - x0, y1 - y0)))
    return regions


def ocr_image(image: Path, lang: str, psm: int, min_conf: float) -> list[Region]:
    result = run(
        ["tesseract", str(image), "stdout", "-l", lang, "--psm", str(psm), "tsv"],
        capture=True,
    )
    return parse_tesseract_tsv(result.stdout, min_conf)


def load_regions(path: Path) -> list[Region]:
    data = json.loads(path.read_text())
    raw = data["regions"] if isinstance(data, dict) and "regions" in data else data
    if not isinstance(raw, list):
        raise SystemExit("regions JSON must be a list or an object with a regions list")
    return [Region.from_dict(item, idx) for idx, item in enumerate(raw)]


def write_regions(path: Path, source: Path, regions: list[Region]) -> None:
    path.write_text(
        json.dumps(
            {"source": str(source), "regions": [region.to_dict() for region in regions]},
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )


def deepseek_translate(texts: list[str], target_lang: str) -> list[str]:
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        return texts
    payload = {
        "model": os.environ.get("DEEPSEEK_MODEL", "deepseek-chat"),
        "messages": [
            {
                "role": "system",
                "content": (
                    "Translate image copy into the requested target language. "
                    "Keep it short, natural, and suitable for visual layout. "
                    "Return only a JSON array of translated strings."
                ),
            },
            {
                "role": "user",
                "content": json.dumps({"target_language": target_lang, "texts": texts}, ensure_ascii=False),
            },
        ],
        "temperature": 0.2,
    }
    req = request.Request(
        "https://api.deepseek.com/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    with request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    content = data["choices"][0]["message"]["content"].strip()
    if content.startswith("```"):
        content = content.strip("`")
        content = content.split("\n", 1)[-1]
    translated = json.loads(content)
    if not isinstance(translated, list) or len(translated) != len(texts):
        raise SystemExit("DeepSeek did not return a same-length JSON array")
    return [str(item) for item in translated]


def translate_missing(regions: list[Region], target_lang: str | None) -> None:
    if not target_lang:
        return
    editable = [region for region in regions if not region.keep and not region.replacement and region.text]
    if not editable:
        return
    translated = deepseek_translate([region.text for region in editable], target_lang)
    for region, replacement in zip(editable, translated, strict=True):
        region.replacement = replacement


def padded_box(box: tuple[int, int, int, int], pad: int, size: tuple[int, int]) -> tuple[int, int, int, int]:
    x, y, w, h = box
    width, height = size
    x0 = max(0, x - pad)
    y0 = max(0, y - pad)
    x1 = min(width, x + w + pad)
    y1 = min(height, y + h + pad)
    return x0, y0, x1, y1


def make_mask(image: Image.Image, regions: list[Region], pad: int, mode: str, text_threshold: float) -> Image.Image:
    mask = Image.new("L", image.size, 0)
    draw = ImageDraw.Draw(mask)
    for region in regions:
        if region.keep:
            continue
        x0, y0, x1, y1 = padded_box(region.box, pad, image.size)
        if mode == "box":
            draw.rectangle((x0, y0, x1, y1), fill=255)
            continue

        import cv2
        import numpy as np

        bx, by, bw, bh = region.box
        x0 = max(0, bx)
        y0 = max(0, by)
        x1 = min(image.width, bx + bw)
        y1 = min(image.height, by + bh)
        crop = np.array(image.crop((x0, y0, x1, y1)).convert("RGB"))
        if crop.size == 0:
            continue
        background = np.median(crop.reshape(-1, 3), axis=0)
        diff = np.sqrt(np.sum((crop.astype("float32") - background.astype("float32")) ** 2, axis=2))
        local = (diff > text_threshold).astype("uint8") * 255
        kernel_size = max(1, min(7, pad * 2 + 1))
        kernel = np.ones((kernel_size, kernel_size), np.uint8)
        local = cv2.dilate(local, kernel, iterations=1)
        local_mask = Image.fromarray(local, mode="L")
        mask.paste(local_mask, (x0, y0))
    return mask


def inpaint_image(image_path: Path, mask_path: Path, out_path: Path, radius: int) -> None:
    import cv2

    image = cv2.imread(str(image_path), cv2.IMREAD_COLOR)
    mask = cv2.imread(str(mask_path), cv2.IMREAD_GRAYSCALE)
    if image is None or mask is None:
        raise SystemExit("failed to read image or mask for inpainting")
    clean = cv2.inpaint(image, mask, radius, cv2.INPAINT_TELEA)
    cv2.imwrite(str(out_path), clean)


def parse_color(value: str, crop: Image.Image, *, stroke: bool = False) -> tuple[int, int, int, int] | None:
    if value.lower() == "none":
        return None
    if value.lower() != "auto":
        text = value.strip()
        if text.startswith("#"):
            text = text[1:]
        if len(text) == 6:
            return tuple(int(text[i : i + 2], 16) for i in (0, 2, 4)) + (255,)
        raise SystemExit(f"unsupported color: {value}")

    gray = crop.convert("L")
    pixels = list(gray.getdata())
    mean = sum(pixels) / max(1, len(pixels))
    if stroke:
        return (255, 255, 255, 255) if mean < 128 else (0, 0, 0, 255)
    return (0, 0, 0, 255) if mean > 150 else (255, 255, 255, 255)


def find_font(user_font: str | None) -> str | None:
    candidates = []
    if user_font:
        candidates.append(user_font)
    candidates.extend(
        [
            "/System/Library/Fonts/PingFang.ttc",
            "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
            "/System/Library/Fonts/Supplemental/Arial.ttf",
            "/Library/Fonts/Arial Unicode.ttf",
        ]
    )
    for candidate in candidates:
        if Path(candidate).exists():
            return candidate
    return None


def wrap_text(text: str, font: ImageFont.FreeTypeFont, max_width: int, draw: ImageDraw.ImageDraw) -> list[str]:
    if not text:
        return []
    words = text.split()
    if len(words) <= 1:
        chars: list[str] = []
        line = ""
        for ch in text:
            trial = line + ch
            if draw.textbbox((0, 0), trial, font=font)[2] <= max_width or not line:
                line = trial
            else:
                chars.append(line)
                line = ch
        if line:
            chars.append(line)
        return chars

    lines: list[str] = []
    line = ""
    for word in words:
        trial = word if not line else f"{line} {word}"
        if draw.textbbox((0, 0), trial, font=font)[2] <= max_width or not line:
            line = trial
        else:
            lines.append(line)
            line = word
    if line:
        lines.append(line)
    return lines


def fit_font(
    text: str,
    font_path: str | None,
    box: tuple[int, int, int, int],
    draw: ImageDraw.ImageDraw,
    max_size: int,
) -> tuple[ImageFont.ImageFont, list[str], int]:
    _, _, w, h = box
    for size in range(min(max_size, max(8, h)), 7, -1):
        font = ImageFont.truetype(font_path, size) if font_path else ImageFont.load_default()
        lines = wrap_text(text, font, max(1, w), draw)
        if not lines:
            return font, [], size
        line_h = max(1, draw.textbbox((0, 0), "Ag", font=font)[3])
        total_h = int(line_h * len(lines) * 1.15)
        max_line_w = max(draw.textbbox((0, 0), line, font=font)[2] for line in lines)
        if total_h <= h and max_line_w <= w:
            return font, lines, size
    font = ImageFont.truetype(font_path, 8) if font_path else ImageFont.load_default()
    return font, wrap_text(text, font, max(1, w), draw), 8


def draw_replacements(
    clean_path: Path,
    final_path: Path,
    regions: list[Region],
    font_path: str | None,
    max_font_size: int,
    stroke_width: int,
) -> None:
    image = Image.open(clean_path).convert("RGBA")
    draw = ImageDraw.Draw(image)
    for region in regions:
        if region.keep:
            continue
        text = region.replacement or region.text
        if not text:
            continue
        x, y, w, h = region.box
        crop = image.crop((x, y, x + w, y + h))
        fill = parse_color(region.fill, crop, stroke=False) or (0, 0, 0, 255)
        stroke = parse_color(region.stroke, crop, stroke=True)
        font, lines, _ = fit_font(text, font_path, region.box, draw, max_font_size)
        if not lines:
            continue
        line_h = max(1, draw.textbbox((0, 0), "Ag", font=font)[3])
        total_h = int(line_h * len(lines) * 1.15)
        yy = y + max(0, (h - total_h) // 2)
        for line in lines:
            bbox = draw.textbbox((0, 0), line, font=font)
            line_w = bbox[2] - bbox[0]
            if region.align == "left":
                xx = x
            elif region.align == "right":
                xx = x + w - line_w
            else:
                xx = x + max(0, (w - line_w) // 2)
            kwargs: dict[str, Any] = {"font": font, "fill": fill}
            if stroke is not None and stroke_width > 0:
                kwargs.update({"stroke_width": stroke_width, "stroke_fill": stroke})
            draw.text((xx, yy), line, **kwargs)
            yy += int(line_h * 1.15)
    image.convert("RGB").save(final_path)


def side_by_side(left_path: Path, right_path: Path, out_path: Path) -> None:
    left = Image.open(left_path).convert("RGB")
    right = Image.open(right_path).convert("RGB")
    height = max(left.height, right.height)
    width = left.width + right.width
    canvas = Image.new("RGB", (width, height), "white")
    canvas.paste(left, (0, 0))
    canvas.paste(right, (left.width, 0))
    canvas.save(out_path)


def qa_sheet(original: Path, mask: Path, clean: Path, final: Path, out_path: Path) -> None:
    labels = [("original", original), ("mask", mask), ("clean", clean), ("final", final)]
    thumbs: list[Image.Image] = []
    for label, path in labels:
        img = Image.open(path).convert("RGB")
        img.thumbnail((360, 360))
        tile = Image.new("RGB", (360, 400), "white")
        tile.paste(img, ((360 - img.width) // 2, 30))
        draw = ImageDraw.Draw(tile)
        draw.text((12, 8), label, fill=(0, 0, 0))
        thumbs.append(tile)
    sheet = Image.new("RGB", (720, 800), "white")
    for idx, thumb in enumerate(thumbs):
        sheet.paste(thumb, ((idx % 2) * 360, (idx // 2) * 400))
    sheet.save(out_path)


def cmd_inspect(args: argparse.Namespace) -> None:
    image = Path(args.image).expanduser().resolve()
    paths = output_paths(image, Path(args.output_dir).expanduser().resolve(), args.prefix)
    regions = ocr_image(image, args.ocr_lang, args.psm, args.min_conf)
    write_regions(paths["ocr"], image, regions)
    write_regions(paths["regions"], image, regions)
    print(f"ocr={paths['ocr']}", file=sys.stderr)
    print(f"regions={paths['regions']}", file=sys.stderr)


def cmd_run(args: argparse.Namespace) -> None:
    image = Path(args.image).expanduser().resolve()
    paths = output_paths(image, Path(args.output_dir).expanduser().resolve(), args.prefix)
    if args.regions:
        regions = load_regions(Path(args.regions).expanduser().resolve())
    else:
        regions = ocr_image(image, args.ocr_lang, args.psm, args.min_conf)
    translate_missing(regions, args.target_lang)
    write_regions(paths["regions"], image, regions)

    original = Image.open(image).convert("RGB")
    mask = make_mask(original, regions, args.pad, args.mask_mode, args.text_threshold)
    mask.save(paths["mask"])
    inpaint_image(image, paths["mask"], paths["clean"], args.radius)
    draw_replacements(
        paths["clean"],
        paths["final"],
        regions,
        find_font(args.font),
        args.max_font_size,
        args.stroke_width,
    )
    if args.compare:
        side_by_side(image, paths["final"], paths["compare"])
    qa_sheet(image, paths["mask"], paths["clean"], paths["final"], paths["qa"])

    print("DONE", file=sys.stderr)
    print(f"regions={paths['regions']}", file=sys.stderr)
    print(f"mask={paths['mask']}", file=sys.stderr)
    print(f"clean={paths['clean']}", file=sys.stderr)
    print(f"final={paths['final']}", file=sys.stderr)
    print(f"qa={paths['qa']}", file=sys.stderr)
    if args.compare:
        print(f"compare={paths['compare']}", file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser(description="Remove and replace text in raster images.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    inspect = sub.add_parser("inspect", help="OCR an image and write editable regions JSON")
    inspect.add_argument("image")
    inspect.add_argument("--output-dir", default="outputs")
    inspect.add_argument("--prefix")
    inspect.add_argument("--ocr-lang", default="eng")
    inspect.add_argument("--psm", type=int, default=6)
    inspect.add_argument("--min-conf", type=float, default=35)
    inspect.set_defaults(func=cmd_inspect)

    run_parser = sub.add_parser("run", help="Inpaint old text and draw replacement text")
    run_parser.add_argument("image")
    run_parser.add_argument("--regions", help="Regions JSON from inspect or manual edit")
    run_parser.add_argument("--output-dir", default="outputs")
    run_parser.add_argument("--prefix")
    run_parser.add_argument("--ocr-lang", default="eng")
    run_parser.add_argument("--psm", type=int, default=6)
    run_parser.add_argument("--min-conf", type=float, default=35)
    run_parser.add_argument("--target-lang", help="Translate missing replacements to this language if API key exists")
    run_parser.add_argument("--pad", type=int, default=4)
    run_parser.add_argument("--mask-mode", choices=["text", "box"], default="text")
    run_parser.add_argument("--text-threshold", type=float, default=35)
    run_parser.add_argument("--radius", type=int, default=3)
    run_parser.add_argument("--font")
    run_parser.add_argument("--max-font-size", type=int, default=96)
    run_parser.add_argument("--stroke-width", type=int, default=2)
    run_parser.add_argument("--compare", action="store_true")
    run_parser.set_defaults(func=cmd_run)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
