from __future__ import annotations

from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont, ImageOps

import system_info

PAGE_POINTS = {"A4": (842, 595), "A3": (1191, 842)}
FONT_REG = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(FONT_BOLD if bold else FONT_REG, size)
    except Exception:
        return ImageFont.load_default(size=size)


def _wrap(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont,
          max_width: int) -> list[str]:
    words = str(text or "").replace("\n", " ").split()
    if not words:
        return [""]
    lines: list[str] = []
    cur = ""
    for w in words:
        test = (cur + " " + w).strip()
        if draw.textlength(test, font=font) <= max_width:
            cur = test
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def _value_lines(draw: ImageDraw.ImageDraw, value: str, font: ImageFont.FreeTypeFont,
                 max_width: int) -> list[str]:
    return _wrap(draw, value, font, max_width)


def _draw_field(draw: ImageDraw.ImageDraw, label: str, value: str, x: int, y: int,
                max_width: int, size: int, line_h: int, color_val=(140, 140, 140),
                color_label=(220, 220, 220)) -> int:
    fl = _font(size, True)
    fv = _font(size, False)
    draw.text((x, y), label, font=fl, fill=color_label)
    y += line_h
    for line in _value_lines(draw, value, fv, max_width):
        draw.text((x, y), line, font=fv, fill=color_val)
        y += line_h
    return y + max(2, line_h // 3)


def _models_text(item: dict[str, Any]) -> str:
    models = item.get("models_used") or item.get("models") or {}
    if isinstance(models, dict):
        parts = []
        for key in ("unet", "clip", "vae"):
            if models.get(key):
                parts.append(f"{key.upper()}: {models[key]}")
        text = " | ".join(parts) if parts else "не зафиксированы"
    else:
        text = str(models)
    loras = item.get("loras") or []
    if isinstance(loras, str):
        loras = [loras]
    return text + " | LoRA: " + (", ".join(str(x) for x in loras) if loras else "none")


def _generation_text(item: dict[str, Any]) -> str:
    elapsed = item.get("elapsed_sec")
    kind = item.get("generation_kind") or "local_comfy"
    if kind == "local_comfy":
        kind_txt = "local ComfyUI"
    else:
        kind_txt = str(kind)
    cost = item.get("api_cost")
    cost_txt = "0" if cost in (None, 0) else str(cost)
    video = item.get("video_seconds")
    res = item.get("resolution") or (
        f"{item.get('width')}x{item.get('height')}" if item.get("width") and item.get("height") else "—")
    note = f" ({item.get('elapsed_note')})" if item.get("elapsed_note") else ""
    return (f"time: {elapsed if elapsed is not None else '—'} s{note} | API: {kind_txt} | "
            f"cost: {cost_txt} | video: {video if video is not None else '—'} s | resolution: {res}")


def make_daily_pdf(images: list[dict[str, Any]], out_path: str | Path, title: str = "DAILY",
                   page_size: str = "A4", dpi: int = 144,
                   hardware: dict[str, Any] | None = None) -> Path:
    points = PAGE_POINTS.get(str(page_size or "A4").upper(), PAGE_POINTS["A4"])
    page_w = int(round(points[0] * dpi / 72))
    page_h = int(round(points[1] * dpi / 72))
    hw = hardware or system_info.hardware_info()
    hw_line = system_info.hardware_line(hw)
    pages: list[Image.Image] = []

    # Page 1: contact sheet, all frames small.
    cover = Image.new("RGB", (page_w, page_h), (0, 0, 0))
    d = ImageDraw.Draw(cover)
    d.text((36, 30), str(title), font=_font(30, True), fill=(235, 235, 235))
    d.text((36, 74), f"{len(images)} кадров · {hw.get('hostname') or ''} · "
                     f"{hw.get('gpu', {}).get('name') or ''}",
           font=_font(17), fill=(140, 140, 140))
    grid_top = 116
    grid_bottom = page_h - 30
    gap = 16
    cols = 2
    rows = max(1, (len(images) + cols - 1) // cols)
    cell_w = (page_w - 72 - gap) // cols
    cell_h = max(80, (grid_bottom - grid_top - gap * (rows - 1)) // rows)
    for i, item in enumerate(images):
        r, c = divmod(i, cols)
        x = 36 + c * (cell_w + gap)
        y = grid_top + r * (cell_h + gap)
        try:
            with Image.open(item["path"]) as src:
                im = ImageOps.contain(src.convert("RGB"), (cell_w, cell_h - 22), Image.LANCZOS)
        except Exception:
            continue
        ox = x + (cell_w - im.width) // 2
        oy = y + (cell_h - 22 - im.height) // 2
        cover.paste(im, (ox, oy))
        label = f"{i + 1}. {item.get('title') or Path(item['path']).stem}"
        d.text((x, y + cell_h - 18), label[:80], font=_font(14), fill=(150, 150, 150))
    pages.append(cover)

    # One frame per page: full-bleed sides, black background, metadata below.
    for idx, item in enumerate(images, 1):
        page = Image.new("RGB", (page_w, page_h), (0, 0, 0))
        draw = ImageDraw.Draw(page)
        with Image.open(item["path"]) as src:
            src = src.convert("RGB")
            img_w = page_w
            img_h = int(round(img_w * src.height / max(1, src.width)))
            max_h = int(page_h * 0.60)
            if img_h > max_h:
                img_h = max_h
                img_w = int(round(img_h * src.width / max(1, src.height)))
            im = src.resize((max(1, img_w), max(1, img_h)), Image.LANCZOS)
        x = (page_w - im.width) // 2
        page.paste(im, (x, 0))
        text_y = im.height + 16
        avail_h = page_h - text_y - 20
        max_width = page_w - 56

        def draw_body(size: int, line_h: int) -> int:
            y = text_y
            draw.text((28, y), f"КАДР {idx}/{len(images)} — {item.get('title') or ''}",
                      font=_font(size + 6, True), fill=(235, 235, 235))
            y += line_h + 8
            y = _draw_field(draw, "PROMPT (EN)", item.get("prompt_en") or item.get("prompt") or "—",
                           28, y, max_width, size, line_h)
            y = _draw_field(draw, "PROMPT (RU)", item.get("prompt_ru") or "—",
                           28, y, max_width, size, line_h)
            y = _draw_field(draw, "MODELS / LoRA", _models_text(item),
                           28, y, max_width, size, line_h)
            y = _draw_field(draw, "GENERATION", _generation_text(item),
                           28, y, max_width, size, line_h)
            y = _draw_field(draw, "HARDWARE", hw_line,
                           28, y, max_width, size, line_h)
            return y

        chosen = None
        for size, line_h in ((18, 22), (17, 21), (16, 20), (15, 19), (14, 18), (13, 17)):
            # dry-run height: approximate by drawing on a copy? use text measures.
            # Use a temporary image of same size, then check final y.
            probe = Image.new("RGB", (page_w, page_h), (0, 0, 0))
            # Draw on probe to measure; then real draw if fits.
            pd = ImageDraw.Draw(probe)
            y = text_y
            y += line_h + 8
            for label, value in (
                    ("PROMPT (EN)", item.get("prompt_en") or item.get("prompt") or "—"),
                    ("PROMPT (RU)", item.get("prompt_ru") or "—"),
                    ("MODELS / LoRA", _models_text(item)),
                    ("GENERATION", _generation_text(item)),
                    ("HARDWARE", hw_line)):
                y += line_h
                for _ in _value_lines(pd, value, _font(size), max_width):
                    y += line_h
                y += max(2, line_h // 3)
            if y <= page_h - 20:
                chosen = (size, line_h)
                break
        size, line_h = chosen or (13, 17)
        draw_body(size, line_h)
        pages.append(page)

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    pages[0].save(out, "PDF", save_all=True, append_images=pages[1:], resolution=dpi)
    return out
