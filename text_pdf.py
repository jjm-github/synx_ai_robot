"""Vector text PDF builder: real selectable text, white pages, embedded fonts.

Used for scripts, plans, .md documents and any text deliverable. Never
renders text into bitmaps — the pages contain only text operators, so
pdftotext/copy/paste and screen readers work.
"""
from __future__ import annotations

import html
import re
import subprocess
from pathlib import Path
from typing import Any

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4, A3, LETTER
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (BaseDocTemplate, Frame, PageTemplate, Paragraph,
                                Preformatted, Spacer, Table, TableStyle)

FONT_DIR = Path("/usr/share/fonts/truetype/dejavu")
_FONTS = {
    "DVS": "DejaVuSans.ttf",
    "DVS-Bold": "DejaVuSans-Bold.ttf",
    "DVS-Mono": "DejaVuSansMono.ttf",
    "DVS-MonoBold": "DejaVuSansMono-Bold.ttf",
}
_registered = False


def _register_fonts() -> None:
    global _registered
    if _registered:
        return
    for name, filename in _FONTS.items():
        path = FONT_DIR / filename
        if path.is_file():
            pdfmetrics.registerFont(TTFont(name, str(path)))
    _registered = True


PAGE_SIZES = {"A4": A4, "A3": A3, "LETTER": LETTER}


def page_geometry(page_size: str = "A4", landscape: bool = False) -> tuple[float, float]:
    size = PAGE_SIZES.get(str(page_size).upper(), A4)
    if landscape:
        return size[1], size[0]
    return size[0], size[1]


INLINE_CODE_RE = re.compile(r"`([^`]+)`")
BOLD_RE = re.compile(r"\*\*([^*]+)\*\*")
ITALIC_RE = re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)")
LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")


def _inline(text: str) -> str:
    """Markdown inline -> ReportLab paragraph markup (XML-escaped)."""
    s = html.escape(text or "", quote=False)
    s = LINK_RE.sub(lambda m: f'<link href="{html.escape(m.group(2))}">{m.group(1)}</link>', s)
    s = INLINE_CODE_RE.sub(lambda m: f'<font name="DVS-Mono" size="9.5">{m.group(1)}</font>', s)
    s = BOLD_RE.sub(r"<b>\1</b>", s)
    s = ITALIC_RE.sub(r"<i>\1</i>", s)
    return s


def _is_table_row(line: str) -> bool:
    return line.strip().startswith("|") and line.strip().endswith("|") and line.count("|") >= 2


def _table(lines: list[str], styles: dict[str, ParagraphStyle],
           width: float) -> Table | None:
    rows: list[list[str]] = []
    for line in lines:
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
            continue  # separator row
        rows.append(cells)
    if not rows:
        return None
    header = rows[0]
    cols = max(len(r) for r in rows)
    data = [[Paragraph(_inline(c), styles["table_head"] if i == 0 else styles["table_cell"])
             for c in (r + [""] * (cols - len(r)))] for i, r in enumerate(rows)]
    table = Table(data, colWidths=[width / cols] * cols, repeatRows=1, hAlign="LEFT")
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#EDEFF2")),
        ("TEXTCOLOR", (0, 0), (-1, -1), colors.HexColor("#1A1A1A")),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#C9CED6")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    return table


def _code_block(code: str, width: float, styles: dict[str, ParagraphStyle]) -> Table:
    pre = Preformatted(code.rstrip("\n") or " ", styles["code"])
    table = Table([[pre]], colWidths=[width], hAlign="LEFT")
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F3F4F6")),
        ("BOX", (0, 0), (-1, -1), 0.4, colors.HexColor("#D5D9DF")),
        ("LEFTPADDING", (0, 0), (-1, -1), 7),
        ("RIGHTPADDING", (0, 0), (-1, -1), 7),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    return table


def markdown_to_flowables(text: str, width: float, font_size: float = 10.5
                          ) -> tuple[list[Any], ParagraphStyle]:
    base = font_size
    styles: dict[str, ParagraphStyle] = {
        "title": ParagraphStyle("title", fontName="DVS-Bold", fontSize=base + 9,
                                leading=base + 13, textColor=colors.HexColor("#111111"),
                                spaceAfter=10, alignment=TA_LEFT),
        "h1": ParagraphStyle("h1", fontName="DVS-Bold", fontSize=base + 5,
                             leading=base + 8, spaceBefore=12, spaceAfter=6),
        "h2": ParagraphStyle("h2", fontName="DVS-Bold", fontSize=base + 2.5,
                             leading=base + 5, spaceBefore=10, spaceAfter=4),
        "h3": ParagraphStyle("h3", fontName="DVS-Bold", fontSize=base + 1,
                             leading=base + 3.5, spaceBefore=8, spaceAfter=3),
        "body": ParagraphStyle("body", fontName="DVS", fontSize=base,
                               leading=base * 1.45, spaceAfter=6),
        "shot": ParagraphStyle("shot", fontName="DVS-Bold", fontSize=base,
                               leading=base * 1.35, spaceBefore=6, spaceAfter=1,
                               textColor=colors.HexColor("#111111")),
        "bullet": ParagraphStyle("bullet", fontName="DVS", fontSize=base,
                                 leading=base * 1.42, leftIndent=14, bulletIndent=4,
                                 spaceAfter=2),
        "num": ParagraphStyle("num", fontName="DVS", fontSize=base,
                              leading=base * 1.42, leftIndent=18, bulletIndent=4,
                              spaceAfter=2),
        "quote": ParagraphStyle("quote", fontName="DVS", fontSize=base - 0.5,
                                leading=base * 1.4, leftIndent=12, textColor=colors.HexColor("#444444")),
        "code": ParagraphStyle("code", fontName="DVS-Mono", fontSize=base - 1.2,
                               leading=(base - 1.2) * 1.35),
        "table_head": ParagraphStyle("th", fontName="DVS-Bold", fontSize=base - 1,
                                     leading=(base - 1) * 1.3, textColor=colors.HexColor("#111111")),
        "table_cell": ParagraphStyle("tc", fontName="DVS", fontSize=base - 1,
                                     leading=(base - 1) * 1.3),
    }
    flow: list[Any] = []
    lines = (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    i = 0
    para: list[str] = []
    in_code = False
    code_lines: list[str] = []
    table_lines: list[str] = []

    def flush_para():
        nonlocal para
        if para:
            flow.append(Paragraph(_inline(" ".join(x.strip() for x in para)), styles["body"]))
            para = []

    def flush_table():
        nonlocal table_lines
        if table_lines:
            t = _table(table_lines, styles, width)
            if t is not None:
                flow.append(t)
                flow.append(Spacer(1, 5))
            table_lines = []

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if in_code:
            if stripped.startswith("```"):
                in_code = False
                flow.append(_code_block("\n".join(code_lines), width, styles))
                flow.append(Spacer(1, 6))
                code_lines = []
            else:
                code_lines.append(line)
            i += 1
            continue
        if stripped.startswith("```"):
            flush_para(); flush_table()
            in_code = True
            i += 1
            continue
        if _is_table_row(line):
            flush_para()
            table_lines.append(line)
            i += 1
            if i >= len(lines) or not _is_table_row(lines[i]):
                flush_table()
            continue
        if not stripped:
            flush_para(); flush_table()
            i += 1
            continue
        # A line that is entirely bold is a block of its own (shot headers).
        if re.fullmatch(r"\*\*.+\*\*", stripped):
            flush_para(); flush_table()
            flow.append(Paragraph(_inline(stripped), styles["shot"]))
            i += 1
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if m:
            flush_para(); flush_table()
            level = len(m.group(1))
            key = "title" if level == 1 else ("h1" if level == 2 else ("h2" if level == 3 else "h3"))
            if level == 1 and not flow:
                flow.append(Paragraph(_inline(m.group(2)), styles["title"]))
            else:
                flow.append(Paragraph(_inline(m.group(2)), styles[key]))
            i += 1
            continue
        m = re.match(r"^([-*+])\s+(.*)$", stripped)
        if m:
            flush_para(); flush_table()
            flow.append(Paragraph(_inline(m.group(2)), styles["bullet"], bulletText="•"))
            i += 1
            continue
        m = re.match(r"^(\d+)[.)]\s+(.*)$", stripped)
        if m:
            flush_para(); flush_table()
            flow.append(Paragraph(_inline(m.group(2)), styles["num"], bulletText=m.group(1) + "."))
            i += 1
            continue
        if stripped.startswith(">"):
            flush_para(); flush_table()
            flow.append(Paragraph(_inline(stripped.lstrip("> ").strip()), styles["quote"]))
            i += 1
            continue
        if re.fullmatch(r"[-*_]{3,}", stripped):
            flush_para(); flush_table()
            flow.append(Spacer(1, 8))
            i += 1
            continue
        para.append(line)
        i += 1
    flush_para(); flush_table()
    if in_code and code_lines:
        flow.append(_code_block("\n".join(code_lines), width, styles))
    if not flow:
        flow.append(Paragraph("(пустой документ)", styles["body"]))
    return flow, styles["body"]


class _NumberedDoc(BaseDocTemplate):
    def __init__(self, filename: str, title: str, page_size: tuple[float, float],
                 font_size: float, **kwargs):
        self.doc_title = title or ""
        super().__init__(filename, pagesize=page_size, **kwargs)
        left = right = 20 * mm
        top = 18 * mm
        bottom = 20 * mm
        frame = Frame(left, bottom, page_size[0] - left - right,
                      page_size[1] - top - bottom, id="body",
                      leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
        self.addPageTemplates([PageTemplate(id="page", frames=[frame],
                                            onPage=self._decorate)])
        self._font_size = font_size

    def _decorate(self, canvas, doc):
        canvas.saveState()
        canvas.setFont("DVS", 7.5)
        canvas.setFillColor(colors.HexColor("#8A8F98"))
        canvas.drawString(20 * mm, 10 * mm, self.doc_title[:90])
        canvas.drawRightString(doc.pagesize[0] - 20 * mm, 10 * mm,
                               f"стр. {doc.page}")
        canvas.restoreState()


def make_text_pdf(text: str, out_path: str | Path, title: str | None = None,
                  page_size: str = "A4", font_size: float = 10.5,
                  landscape: bool = False) -> dict[str, Any]:
    """Build a text PDF (real vector text, white pages, embedded DejaVu)."""
    _register_fonts()
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    size = page_geometry(page_size, landscape)
    margin = 20 * mm
    width = size[0] - 2 * margin
    flow, _ = markdown_to_flowables(text, width, font_size=font_size)
    doc = _NumberedDoc(str(out), title or out.stem, size, font_size,
                       author="Synx AI Robot", subject="Text document")
    doc.build(flow)
    info = verify_text_pdf(out)
    info.update({"path": str(out), "title": title or out.stem,
                 "page_size": f"{int(size[0])}x{int(size[1])}",
                 "font_size": font_size})
    return info


def verify_text_pdf(path: str | Path, min_chars: int = 40) -> dict[str, Any]:
    """Check that the PDF contains real extractable text and embedded fonts."""
    p = Path(path)
    info: dict[str, Any] = {"path": str(p), "exists": p.is_file(), "ok": False,
                            "errors": []}
    if not p.is_file():
        info["errors"].append("PDF не создан")
        return info
    try:
        out = subprocess.run(["pdfinfo", str(p)], capture_output=True, text=True, timeout=30)
        pages = re.search(r"^Pages:\s+(\d+)", out.stdout or "", re.M)
        info["pages"] = int(pages.group(1)) if pages else 0
    except Exception as exc:
        info["errors"].append(f"pdfinfo: {exc}")
    try:
        fonts = subprocess.run(["pdffonts", str(p)], capture_output=True, text=True, timeout=30)
        lines = [l for l in (fonts.stdout or "").splitlines()[2:] if l.strip()]
        info["fonts"] = len(lines)
        if not lines:
            info["errors"].append("нет встроенных шрифтов — текст, вероятно, растровый")
    except Exception as exc:
        info["errors"].append(f"pdffonts: {exc}")
    try:
        txt = subprocess.run(["pdftotext", str(p), "-"], capture_output=True,
                             text=True, timeout=60).stdout or ""
        info["text_chars"] = len(txt.strip())
        if info["text_chars"] < min_chars:
            info["errors"].append("текст не извлекается — страницы растровые")
    except Exception as exc:
        info["errors"].append(f"pdftotext: {exc}")
    info["ok"] = not info["errors"] and info.get("pages", 0) > 0
    return info
