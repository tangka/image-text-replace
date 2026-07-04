---
name: image-text-replace
description: Remove, translate, and replace text in raster images. Use when the user wants to erase embedded text, labels, captions, watermarks, product-image copy, poster text, screenshots, or any language text from PNG/JPEG/WebP images and recreate the image with target-language text. Produces clean image, translated final image, compare image, OCR/region JSON, masks, and QA artifacts.
---

# image-text-replace

Use this skill for still images that contain text burned into pixels. The goal is not only OCR and translation; the workflow must first create a clean image with old text removed, then add target-language text with matching layout.

## Bootstrap

```bash
cd /Users/tangka/Code/scripts/image-text-replace
miss=""
command -v uv >/dev/null || miss="$miss uv"
command -v tesseract >/dev/null || miss="$miss tesseract"
[ -z "$miss" ] && echo READY || echo "NEEDS:$miss"
```

`tesseract` is optional when a manual regions JSON is provided. Translation is optional; if `DEEPSEEK_API_KEY` is present, the wrapper can translate OCR text into the requested target language.

## Workflow

Do not paint new text over old text. Build the pipeline:

1. Inspect the image and decide which text is editable copy vs protected artwork.
2. OCR text boxes with `inspect`, or create/edit a regions JSON manually.
3. Generate a mask and clean image with inpainting.
4. Check the clean image for old-text residue and product/logo damage.
5. Add replacement target-language text.
6. Export compare and QA images.

## Commands

OCR/region draft:

```bash
uv run --python 3.12 --with pillow scripts/image_text_replace.py inspect <image> \
  --output-dir outputs \
  --prefix <name> \
  --ocr-lang eng
```

Full run:

```bash
uv run --python 3.12 --with pillow --with opencv-python scripts/image_text_replace.py run <image> \
  --regions outputs/<name>.regions.json \
  --target-lang English \
  --output-dir outputs \
  --prefix <name> \
  --mask-mode text \
  --compare
```

One-shot OCR + cleanup + replacement:

```bash
uv run --python 3.12 --with pillow --with opencv-python scripts/image_text_replace.py run <image> \
  --ocr-lang eng \
  --target-lang Chinese \
  --output-dir outputs \
  --prefix <name> \
  --mask-mode text \
  --compare
```

Useful cleanup parameters:

| Parameter | Use |
|-|-|
| `--mask-mode text` | Default. Build a pixel-level text mask inside each region so large boxes do not repaint the whole label/background. |
| `--mask-mode box` | Remove the entire rectangular region. Use only when the whole label area should disappear. |
| `--text-threshold 35` | Pixel difference threshold for text mask selection. Raise it if background texture is being masked; lower it if thin text edges remain. |
| `--pad 4` | Dilation padding around detected text pixels. Increase for outline/shadow residue, decrease when nearby artwork is touched. |

## Regions JSON

The wrapper reads and writes this schema:

```json
{
  "source": "/absolute/path/image.png",
  "regions": [
    {
      "id": "r001",
      "text": "SALE",
      "replacement": "促销",
      "box": [120, 80, 240, 64],
      "keep": false,
      "fill": "auto",
      "stroke": "auto",
      "align": "center"
    }
  ]
}
```

`box` is `[x, y, width, height]`. Set `keep: true` for logo/product artwork that OCR detected but should not be removed. Add `replacement` manually when translation is not available or when marketing copy needs human wording.

## Outputs

- `<prefix>.ocr.json`: OCR line boxes and source text.
- `<prefix>.regions.json`: editable region plan.
- `<prefix>.mask.png`: text-removal mask.
- `<prefix>.clean.png`: old text removed, no replacement text yet.
- `<prefix>.replaced.png`: target-language final image.
- `<prefix>.compare.png`: original vs final side by side.
- `<prefix>.qa.png`: compact QA sheet with original, mask, clean, final.

## QA Standard

First inspect `<prefix>.clean.png`:

- Old text must be gone, including thin edge pixels.
- Background texture should not look smeared at normal viewing size.
- Product artwork, logos, packaging marks, and intentional decorative text must remain if they were marked `keep`.
- If text crosses a complex product edge or textured background and OpenCV inpaint looks fake, rerun with tighter regions or switch to AI image inpainting.
- Prefer `--mask-mode text`. Use `--mask-mode box` only when the entire rectangular label/background should disappear.

Then inspect `<prefix>.replaced.png`:

- New text should fit the original visual hierarchy.
- Avoid opaque bars unless the user explicitly asks for a label/bar style.
- Match color, stroke, alignment, and line breaks where possible.
- Do not overwrite protected product details.

## Notes

Tesseract language packs may be limited on a machine. If OCR misses non-English text, use manual regions: run `inspect` on what it can detect, then edit `<prefix>.regions.json` with exact boxes and replacements before `run`.
