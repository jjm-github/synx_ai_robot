"""Local Russian voice input: streaming T-one partials + GigaAM final pass.

Architecture:
  arecord 16 kHz -> T-one streaming CTC (8 kHz) -> live partial text
  release        -> GigaAM v3 e2e CTC (16 kHz) -> clean final text
                 -> optional LLM punctuation/term correction

Everything runs locally on CPU; models are lazy-loaded and can be unloaded
from the UI to free memory.
"""
from __future__ import annotations

import gc
import json
import subprocess
import threading
import time
import wave
from pathlib import Path
from typing import Any

import numpy as np

from config import ROOT, load_config, save_config

MODELS = ROOT / "data" / "voice" / "models"
T_ONE = MODELS / "t_one"
GIGAAM = MODELS / "gigaam_v3"

GLOSSARY = ("synx, синкс, шот, секвенция, локация, ассет, персонаж, раскадровка, "
            "воркфлоу, ComfyUI, LoRA, рендер, пилот, эпизод")


class VoiceEngine:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._online = None
        self._offline = None
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._running = False
        self._samples = np.zeros(0, dtype=np.float32)
        self._stream = None
        self._partial = ""
        self._status = "idle"
        self._error: str | None = None
        self._started_at = 0.0
        self._final_cv = threading.Condition(self._lock)
        self._last_result: dict[str, Any] | None = None

    # --- config ---------------------------------------------------------
    def cfg(self) -> dict[str, Any]:
        return (load_config().get("voice") or {})

    def enabled(self) -> bool:
        return bool(self.cfg().get("enabled", True))

    def set_enabled(self, value: bool) -> dict[str, Any]:
        cfg = load_config()
        cfg.setdefault("voice", {})["enabled"] = bool(value)
        save_config(cfg)
        if not value:
            self.unload()
        return self.status()

    # --- models ---------------------------------------------------------
    def _load(self) -> None:
        if self._online is not None and self._offline is not None:
            return
        with self._lock:
            if self._online is not None and self._offline is not None:
                return
            self._status = "loading"
            self._error = None
            try:
                import sherpa_onnx
                if not (T_ONE / "model.onnx").is_file() or not (T_ONE / "tokens.txt").is_file():
                    raise FileNotFoundError(f"T-one model not found in {T_ONE}")
                if not (GIGAAM / "gigaam_v3_e2e_ctc_int8.onnx").is_file():
                    raise FileNotFoundError(f"GigaAM CTC model not found in {GIGAAM}")
                self._online = sherpa_onnx.OnlineRecognizer.from_t_one_ctc(
                    tokens=str(T_ONE / "tokens.txt"),
                    model=str(T_ONE / "model.onnx"),
                    num_threads=2, sample_rate=8000, provider="cpu")
                self._offline = sherpa_onnx.OfflineRecognizer.from_nemo_ctc(
                    model=str(GIGAAM / "gigaam_v3_e2e_ctc_int8.onnx"),
                    tokens=str(GIGAAM / "gigaam_v3_e2e_ctc_tokens.txt"),
                    num_threads=2, sample_rate=16000, provider="cpu")
                self._status = "ready"
            except Exception as exc:
                self._status = "error"
                self._error = str(exc)[:500]
                self._online = None
                self._offline = None
                raise

    def unload(self) -> None:
        with self._lock:
            self._online = None
            self._offline = None
            gc.collect()
            if self._status not in ("recording", "finalizing"):
                self._status = "idle"

    # --- capture --------------------------------------------------------
    def start(self) -> dict[str, Any]:
        if not self.enabled():
            raise RuntimeError("голосовой ввод выключен")
        with self._lock:
            if self._running:
                return self.status()
            self._load()
            self._partial = ""
            self._samples = np.zeros(0, dtype=np.float32)
            self._stream = self._online.create_stream()
            device = str(self.cfg().get("audio_device") or "").strip()
            cmd = ["arecord", "-q", "-f", "S16_LE", "-r", "16000", "-c", "1", "-t", "raw"]
            if device:
                cmd = ["arecord", "-q", "-D", device, "-f", "S16_LE", "-r", "16000",
                       "-c", "1", "-t", "raw"]
            self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                          stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
            self._running = True
            self._status = "recording"
            self._started_at = time.time()
            self._error = None
            self._thread = threading.Thread(target=self._reader, daemon=True, name="voice-reader")
            self._thread.start()
        time.sleep(0.15)
        if self._proc is not None and self._proc.poll() is not None:
            self._running = False
            self._status = "error"
            self._error = "микрофон недоступен (arecord завершился)"
            raise RuntimeError(self._error)
        return self.status()

    def _reader(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        while True:
            chunk = proc.stdout.read(6400)  # 0.2 s of 16 kHz int16
            if not chunk:
                return
            with self._lock:
                if not self._running:
                    return
            x = np.frombuffer(chunk, dtype=np.int16).astype(np.float32) / 32768.0
            if len(x) < 2:
                continue
            with self._lock:
                self._samples = np.concatenate([self._samples, x])[-60 * 16000:]
                stream = self._stream
                online = self._online
            if stream is None or online is None:
                return
            # 16k -> 8k with a cheap 2-tap anti-alias (pair average)
            n = len(x) - (len(x) % 2)
            x8 = (x[0:n:2] + x[1:n:2]) * 0.5
            try:
                stream.accept_waveform(8000, x8)
                while online.is_ready(stream):
                    online.decode_stream(stream)
                text = online.get_result(stream)
                with self._lock:
                    if self._running:
                        self._partial = (text or "").strip()
            except Exception as exc:
                with self._lock:
                    self._error = str(exc)[:300]

    def stop(self) -> dict[str, Any]:
        with self._lock:
            if self._status == "finalizing":
                deadline = time.time() + 40
                while self._status == "finalizing" and time.time() < deadline:
                    self._final_cv.wait(timeout=0.2)
                return self._last_result or {**self.status(), "final": self._partial}
            if not self._running:
                return {**self.status(), "final": (self._last_result or {}).get("final", "")}
            self._running = False
            self._status = "finalizing"
            proc = self._proc
            self._proc = None
            samples = self._samples.copy()
            partial = self._partial
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        if self._thread is not None:
            self._thread.join(timeout=2)
        final = partial
        if len(samples) >= 3200 and self._offline is not None:
            try:
                st = self._offline.create_stream()
                st.accept_waveform(16000, samples)
                self._offline.decode_stream(st)
                final = (st.result.text or "").strip() or partial
            except Exception as exc:
                self._error = str(exc)[:300]
        corrected = self._correct(final) if final else ""
        result = {**self.status(), "status": "ready",
                  "final": corrected or final, "partial": partial, "raw": final,
                  "seconds": round(len(samples) / 16000.0, 2)}
        with self._lock:
            self._status = "ready"
            self._stream = None
            self._last_result = result
            self._final_cv.notify_all()
        return result

    def _correct(self, text: str) -> str:
        if not text or not bool(self.cfg().get("llm_correct", True)):
            return text
        try:
            from ai import AIClient
            ai = AIClient()
            if not ai.ready:
                return text
            prompt = (
                "Ты редактируешь текст, распознанный с микрофона по-русски. "
                "Исправь только пунктуацию, регистр, очевидные ошибки распознавания "
                "и термины. Не переписывай смысл, не добавляй и не удаляй факты. "
                f"Термины проекта: {GLOSSARY}. Верни только исправленный текст, без пояснений.\n\n"
                f"Текст: {text}"
            )
            out, _ = ai.chat([
                {"role": "system", "content": "Ты аккуратный редактор расшифровки речи."},
                {"role": "user", "content": prompt},
            ])
            out = (out or "").strip().strip('"').strip("«»").strip()
            if not out:
                return text
            if len(out) < len(text) * 0.5 or len(out) > len(text) * 2:
                return text
            return out
        except Exception:
            return text

    def transcribe_file(self, path: str | Path) -> dict[str, Any]:
        self._load()
        with wave.open(str(path), "rb") as w:
            sr = w.getframerate()
            data = w.readframes(w.getnframes())
        samples = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
        if sr != 16000:
            # linear resample to 16 kHz (enough for a test endpoint)
            n = int(len(samples) * 16000 / sr)
            idx = np.linspace(0, len(samples) - 1, n)
            samples = np.interp(idx, np.arange(len(samples)), samples).astype(np.float32)
        st = self._offline.create_stream()
        st.accept_waveform(16000, samples)
        self._offline.decode_stream(st)
        text = (st.result.text or "").strip()
        return {"text": text, "corrected": self._correct(text),
                "seconds": round(len(samples) / 16000.0, 2)}

    # --- status ---------------------------------------------------------
    def status(self) -> dict[str, Any]:
        with self._lock:
            secs = 0.0
            if self._status == "recording" and self._started_at:
                secs = round(time.time() - self._started_at, 1)
            return {"enabled": self.enabled(), "status": self._status,
                    "partial": self._partial, "seconds": secs,
                    "models_loaded": self._online is not None and self._offline is not None,
                    "error": self._error}


engine = VoiceEngine()


def status() -> dict[str, Any]:
    return engine.status()


def start() -> dict[str, Any]:
    return engine.start()


def stop() -> dict[str, Any]:
    return engine.stop()


def partial() -> dict[str, Any]:
    return engine.status()


def toggle(enabled: bool) -> dict[str, Any]:
    return engine.set_enabled(enabled)
