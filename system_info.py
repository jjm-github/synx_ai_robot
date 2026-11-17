from __future__ import annotations

import platform
import re
import subprocess
from typing import Any


def _run(cmd: list[str], timeout: int = 15) -> str:
    try:
        cp = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return cp.stdout if cp.returncode == 0 else ""
    except Exception:
        return ""


def _num(text: str, default: float | None = None) -> float | None:
    m = re.search(r"[-+]?\d+(?:[.,]\d+)?", str(text or ""))
    if not m:
        return default
    try:
        return float(m.group(0).replace(",", "."))
    except Exception:
        return default


def _meminfo() -> dict[str, int]:
    out = {}
    try:
        for line in open("/proc/meminfo", encoding="utf-8"):
            key, _, rest = line.partition(":")
            out[key.strip()] = int(rest.strip().split()[0])
    except Exception:
        pass
    return out


def _vram_type(name: str) -> str:
    upper = str(name or "").upper()
    if "RTX 40" in upper:
        return "GDDR6X"
    if "RTX 30" in upper or "RTX 20" in upper or "GTX 16" in upper:
        return "GDDR6"
    if "GTX 10" in upper:
        return "GDDR5"
    if "A100" in upper or "H100" in upper:
        return "HBM2/HBM3"
    return "не определён"


def cpu_info() -> dict[str, Any]:
    text = _run(["lscpu"])
    model = re.search(r"Model name:\s+(.+)", text)
    max_mhz = re.search(r"CPU max MHz:\s+([\d.,]+)", text)
    cores = _num(re.search(r"Core\(s\) per socket:\s+(\d+)", text).group(1)) if re.search(
        r"Core\(s\) per socket:\s+(\d+)", text) else None
    threads = _num(re.search(r"CPU\(s\):\s+(\d+)", text).group(1)) if re.search(
        r"CPU\(s\):\s+(\d+)", text) else None
    per_core = _num(re.search(r"Thread\(s\) per core:\s+(\d+)", text).group(1)) if re.search(
        r"Thread\(s\) per core:\s+(\d+)", text) else None
    if cores and per_core:
        threads = threads or cores * per_core
    return {
        "model": model.group(1).strip() if model else platform.processor() or "не определён",
        "cores": int(cores) if cores else None,
        "threads": int(threads) if threads else None,
        "max_mhz": _num(max_mhz.group(1)) if max_mhz else None,
    }


def gpu_info() -> dict[str, Any]:
    out = _run(["nvidia-smi", "--query-gpu=name,memory.total,driver_version",
                "--format=csv,noheader,nounits"])
    if out.strip():
        parts = [x.strip() for x in out.strip().splitlines()[0].split(",")]
        name = parts[0] if parts else "NVIDIA GPU"
        vram_mb = _num(parts[1]) if len(parts) > 1 else None
        driver = parts[2] if len(parts) > 2 else ""
        return {"name": name, "vram_gb": round(vram_mb / 1024.0, 1) if vram_mb else None,
                "vram_type": _vram_type(name), "driver": driver}
    out = _run(["lspci"])
    m = re.search(r"(NVIDIA|AMD/ATI).*?:\s*(.+)", out)
    name = m.group(2).strip() if m else "не определён"
    return {"name": name, "vram_gb": None, "vram_type": "не определён", "driver": ""}


def ram_info() -> dict[str, Any]:
    out = _run(["inxi", "-m", "-c", "0"])
    total = re.search(r"System RAM:\s+total:\s+([\d.]+)\s+GiB", out)
    types = re.findall(r"type:\s+(DDR\d|LPDDR\d)", out, re.I)
    sizes = re.findall(r"size:\s+([\d.]+)\s+GiB", out, re.I)
    speeds = re.findall(r"speed:\s+([\d.]+)\s+MT/s", out, re.I)
    mem = _meminfo()
    total_gb = float(total.group(1)) if total else (
        round(mem.get("MemTotal", 0) / 1024.0 / 1024.0, 1) if mem.get("MemTotal") else None)
    modules = []
    for size, typ, speed in zip(sizes, types + [""] * len(sizes), speeds + [""] * len(sizes)):
        modules.append({"size_gb": float(size), "type": typ.upper(), "speed": speed})
    return {
        "total_gb": total_gb,
        "type": (types[0].upper() if types else "не определён"),
        "speed": (speeds[0] + " MT/s" if speeds else ""),
        "modules": modules,
    }


def hardware_info() -> dict[str, Any]:
    mem = _meminfo()
    swap_gb = round(mem.get("SwapTotal", 0) / 1024.0 / 1024.0, 1) if mem.get("SwapTotal") else None
    return {
        "hostname": platform.node(),
        "os": platform.platform(),
        "cpu": cpu_info(),
        "gpu": gpu_info(),
        "ram": ram_info(),
        "swap_gb": swap_gb,
    }


def hardware_line(hw: dict[str, Any] | None = None) -> str:
    hw = hw or hardware_info()
    cpu = hw.get("cpu") or {}
    gpu = hw.get("gpu") or {}
    ram = hw.get("ram") or {}
    parts = [
        f"PC: {hw.get('hostname') or '—'}",
        f"GPU: {gpu.get('name') or '—'}, {gpu.get('vram_gb') or '—'} GB {gpu.get('vram_type') or ''}".strip(),
    ]
    cpu_txt = f"CPU: {cpu.get('model') or '—'}"
    if cpu.get("cores") or cpu.get("threads"):
        cpu_txt += f", {cpu.get('cores') or '?'}C/{cpu.get('threads') or '?'}T"
    if cpu.get("max_mhz"):
        cpu_txt += f", {cpu.get('max_mhz') / 1000.0:.1f} GHz"
    parts.append(cpu_txt)
    ram_txt = f"RAM: {ram.get('total_gb') or '—'} GB {ram.get('type') or ''}"
    if ram.get("speed"):
        ram_txt += f" {ram.get('speed')}"
    parts.append(ram_txt)
    parts.append(f"SWAP: {hw.get('swap_gb') if hw.get('swap_gb') is not None else '—'} GB")
    return " | ".join(parts)
