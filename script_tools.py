"""Validation for shot-by-shot episode scripts.

A pilot script must be a full episode broken into sequences (one per
location) and numbered shots with camera plan, action/dialogue and duration.
This keeps the robot from publishing a prose synopsis instead of a shot list.
"""
from __future__ import annotations

import re
from typing import Any

SEQ_RE = re.compile(r"^##\s+S(\d{2})\s*[·:.\-—]\s*(.+)$", re.M)
SHOT_RE = re.compile(
    r"\*\*S(\d{2})-(\d{2})\s*[·:.\-—]\s*([^*·]+?)\s*[·:.\-—]\s*(\d{1,3})\s*(?:c|сек)\*\*",
    re.I)
DURATION_RE = re.compile(r"(\d{1,3})\s*(?:c|сек)\b", re.I)


def validate_shot_script(text: str, min_sequences: int = 3, min_shots: int = 15,
                         min_seconds: int = 480, max_seconds: int = 720) -> dict[str, Any]:
    text = text or ""
    errors: list[str] = []
    warnings: list[str] = []

    sequences = []
    for m in SEQ_RE.finditer(text):
        sequences.append({"code": f"S{m.group(1)}", "title": m.group(2).strip().strip("-—· ")})
    if len(sequences) < min_sequences:
        errors.append(f"секвенций {len(sequences)}, нужно ≥{min_sequences} "
                      f"(заголовки вида «## S01 · НАТ. ЛОКАЦИЯ — ВРЕМЯ»)")

    shots = []
    seq_codes = {s["code"] for s in sequences}
    for m in SHOT_RE.finditer(text):
        code = f"S{m.group(1)}-{m.group(2)}"
        plan = m.group(3).strip()
        sec = int(m.group(4))
        shots.append({"code": code, "sequence": f"S{m.group(1)}",
                      "plan": plan, "seconds": sec})
        if f"S{m.group(1)}" not in seq_codes:
            warnings.append(f"{code}: нет заголовка секвенции S{m.group(1)}")
    if len(shots) < min_shots:
        errors.append(f"шотов {len(shots)}, нужно ≥{min_shots} "
                      f"(строки вида «**S01-01 · WS · 8 c**»)")

    total = sum(s["seconds"] for s in shots)
    if shots and not (min_seconds <= total <= max_seconds):
        errors.append(f"хронометраж {total} c, нужен {min_seconds}–{max_seconds} c "
                      f"(≈8–12 минут)")

    per_sequence: dict[str, dict[str, int]] = {}
    for s in shots:
        d = per_sequence.setdefault(s["sequence"], {"shots": 0, "seconds": 0})
        d["shots"] += 1
        d["seconds"] += s["seconds"]
    for seq in sequences:
        d = per_sequence.get(seq["code"], {"shots": 0, "seconds": 0})
        if d["shots"] < 2:
            warnings.append(f"{seq['code']}: {d['shots']} шот(ов) — для секвенции мало")

    # Structural sanity: each shot should describe something, not just a plan.
    body_after_shot = 0
    for m in SHOT_RE.finditer(text):
        tail = text[m.end():m.end() + 600]
        if len(tail.strip()) < 20:
            body_after_shot += 1
    if body_after_shot:
        warnings.append(f"{body_after_shot} шот(ов) без описания действия")

    return {
        "ok": not errors,
        "sequences": len(sequences),
        "shots": len(shots),
        "duration_sec": total,
        "duration_hms": f"{total // 60}:{total % 60:02d}",
        "per_sequence": per_sequence,
        "errors": errors,
        "warnings": warnings,
        "sequence_titles": [f"{s['code']} · {s['title']}" for s in sequences],
    }
