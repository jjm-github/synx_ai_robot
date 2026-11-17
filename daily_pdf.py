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
        return ImageFont.truetype(FONT_BOLD if bold else FONT_REG, max(6, size))
    except Exception:
        return ImageFont.load_default(size=max(6, size))


def _rgb(color: str) -> tuple[int, int, int]:
    text = str(color or "#000000").lstrip("#")
    if len(text) == 3:
        text = "".join(c * 2 for c in text)
    try:
        return tuple(int(text[i:i + 2], 16) for i in (0, 2, 4))
    except Exception:
        return (0, 0, 0)


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
    kind_txt = "local ComfyUI" if kind == "local_comfy" else str(kind)
    cost = item.get("api_cost")
    cost_txt = "0" if cost in (None, 0) else str(cost)
    video = item.get("video_seconds")
    res = item.get("resolution") or (
        f"{item.get('width')}x{item.get('height')}" if item.get("width") and item.get("height") else "—")
    note = f" ({item.get('elapsed_note')})" if item.get("elapsed_note") else ""
    return (f"time: {elapsed if elapsed is not None else '—'} s{note} | API: {kind_txt} | "
            f"cost: {cost_txt} | video: {video if video is not None else '—'} s | resolution: {res}")


def _blocks(item: dict[str, Any], hw_line: str) -> list[tuple[str, str]]:
    return [
        ("PROMPT (EN)", item.get("prompt_en") or item.get("prompt") or "—"),
        ("PROMPT (RU)", item.get("prompt_ru") or "—"),
        ("MODELS / LoRA", _models_text(item)),
        ("GENERATION", _generation_text(item)),
        ("HARDWARE", hw_line),
    ]


def _cover_label(draw: ImageDraw.ImageDraw, label: str, x: int, y: int,
                 max_width: int, font: ImageFont.FreeTypeFont) -> None:
    text = label
    if draw.textlength(text, font=font) > max_width:
        while text and draw.textlength(text + "…", font=font) > max_width:
            text = text[:-1]
        text += "…"
    draw.text((x, y), text, font=font, fill=(150, 150, 150))


def _fit_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont,
              max_width: int) -> str:
    text = str(text or "")
    if draw.textlength(text, font=font) <= max_width:
        return text
    while text and draw.textlength(text + "…", font=font) > max_width:
        text = text[:-1]
    return text + "…"


def _draw_block(draw: ImageDraw.ImageDraw, label: str, value: str, x: int, y: int,
                width: int, fs: int, lh: int, label_color, value_color) -> int:
    draw.text((x, y), label, font=_font(fs, True), fill=label_color)
    y += lh
    for line in _wrap(draw, value, _font(fs), width):
        draw.text((x, y), line, font=_font(fs), fill=value_color)
        y += lh
    return y + max(1, lh // 4)


def make_daily_pdf(images: list[dict[str, Any]], out_path: str | Path, title: str = "DAILY",
                   page_size: str = "A4", dpi: int = 144,
                   hardware: dict[str, Any] | None = None,
                   background: str = "#000000", text_scale: float = 1.0,
                   image_valign: str = "top", text_layout: str = "stack") -> Path:
    points = PAGE_POINTS.get(str(page_size or "A4").upper(), PAGE_POINTS["A4"])
    page_w = int(round(points[0] * dpi / 72))
    page_h = int(round(points[1] * dpi / 72))
    bg = _rgb(background)
    dpi_scale = dpi / 144.0
    text_scale = max(0.45, min(1.8, float(text_scale or 1.0)))
    scale = dpi_scale * text_scale
    label_color = (153, 153, 153)   # 60% of white on black
    value_color = (102, 102, 102)   # 40% of white on black
    title_color = (153, 153, 153)
    hw = hardware or system_info.hardware_info()
    hw_line = system_info.hardware_line(hw)
    pages: list[Image.Image] = []

    # Page 1: contact sheet, all frames small.
    cover = Image.new("RGB", (page_w, page_h), bg)
    d = ImageDraw.Draw(cover)
    d.text((int(36 * dpi_scale), int(30 * dpi_scale)), str(title),
           font=_font(int(30 * scale), True), fill=title_color)
    d.text((int(36 * dpi_scale), int(74 * dpi_scale)),
           f"{len(images)} кадров · {hw.get('hostname') or ''} · "
           f"{hw.get('gpu', {}).get('name') or ''}",
           font=_font(int(17 * scale)), fill=value_color)
    grid_top = int(116 * dpi_scale)
    grid_bottom = page_h - int(30 * dpi_scale)
    gap = int(16 * dpi_scale)
    cols = 2
    rows = max(1, (len(images) + cols - 1) // cols)
    side = int(36 * dpi_scale)
    cell_w = (page_w - side * 2 - gap) // cols
    cell_h = max(80, (grid_bottom - grid_top - gap * (rows - 1)) // rows)
    label_h = int(22 * scale)
    for i, item in enumerate(images):
        r, c = divmod(i, cols)
        x = side + c * (cell_w + gap)
        y = grid_top + r * (cell_h + gap)
        try:
            with Image.open(item["path"]) as src:
                im = ImageOps.contain(src.convert("RGB"),
                                      (cell_w, max(40, cell_h - label_h)), Image.LANCZOS)
        except Exception:
            continue
        ox = x + (cell_w - im.width) // 2
        oy = y + (cell_h - label_h - im.height) // 2
        cover.paste(im, (ox, oy))
        _cover_label(d, f"{i + 1}. {item.get('title') or Path(item['path']).stem}",
                     x, y + cell_h - label_h, cell_w, _font(int(14 * scale)))
    pages.append(cover)

    # One frame per page: full-bleed width, black background, text below.
    for idx, item in enumerate(images, 1):
        page = Image.new("RGB", (page_w, page_h), bg)
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
        img_x = (page_w - im.width) // 2
        img_y = 0 if str(image_valign).lower() != "center" else (page_h - im.height) // 2
        page.paste(im, (img_x, img_y))
        text_x = int(24 * dpi_scale)
        text_y = img_y + im.height + int(6 * scale)
        max_width = page_w - text_x * 2
        blocks = _blocks(item, hw_line)
        title_txt = f"КАДР {idx}/{len(images)} — {item.get('title') or ''}"
        columns = str(text_layout).lower() == "columns"
        left_blocks = blocks[:2] if columns else blocks
        right_blocks = blocks[2:] if columns else []
        col_gap = int(24 * dpi_scale)
        col_w = (max_width - col_gap) // 2 if columns else max_width
        bottom_limit = page_h - int(8 * dpi_scale)

        chosen = None
        for base_size in (20, 19, 18, 17, 16, 15, 14, 13, 12, 11, 10, 9):
            fs = max(7, int(round(base_size * scale)))
            lh = max(9, int(round(base_size * 1.18 * scale)))
            probe = ImageDraw.Draw(Image.new("RGB", (page_w, page_h), bg))
            title_lh = lh + int(3 * scale)
            y0 = text_y + title_lh
            yl = y0
            for label, value in left_blocks:
                yl += lh
                yl += len(_wrap(probe, value, _font(fs), col_w)) * lh
                yl += max(1, lh // 4)
            yr = y0
            for label, value in right_blocks:
                yr += lh
                yr += len(_wrap(probe, value, _font(fs), col_w)) * lh
                yr += max(1, lh // 4)
            if max(yl, yr) <= bottom_limit:
                chosen = (fs, lh)
                break
        fs, lh = chosen or (max(7, int(round(10 * scale))), max(9, int(round(12 * scale))))

        ft = _font(min(fs + max(3, int(round(3 * scale))), int(40 * scale)), True)
        draw.text((text_x, text_y),
                  _fit_text(draw, title_txt, ft, max_width), font=ft, fill=title_color)
        y0 = text_y + lh + int(3 * scale)
        if columns:
            yl = y0
            for label, value in left_blocks:
                yl = _draw_block(draw, label, value, text_x, yl, col_w, fs, lh,
                                 label_color, value_color)
            yr = y0
            for label, value in right_blocks:
                yr = _draw_block(draw, label, value, text_x + col_w + col_gap, yr,
                                 col_w, fs, lh, label_color, value_color)
        else:
            y = y0
            for label, value in blocks:
                y = _draw_block(draw, label, value, text_x, y, max_width, fs, lh,
                                label_color, value_color)
        pages.append(page)

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    pages[0].save(out, "PDF", save_all=True, append_images=pages[1:], resolution=dpi)
    return out
