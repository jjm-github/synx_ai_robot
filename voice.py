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
import platform
import queue
import subprocess
import threading
import time
import wave
from pathlib import Path
from typing import Any

import numpy as np
import requests

from config import DATA_DIR, load_config, save_config

MODELS = DATA_DIR / "voice" / "models"
T_ONE = MODELS / "t_one"
GIGAAM = MODELS / "gigaam_v3"

GLOSSARY = ("synx, синкс, шот, секвенция, локация, ассет, персонаж, раскадровка, "
            "воркфлоу, ComfyUI, LoRA, рендер, пилот, эпизод")

CORRECT_SYSTEM = (
    "Ты корректор расшифровки русской речи. Сохрани ВСЕ слова исходного текста "
    "и их порядок. Разрешено только: расставить знаки препинания, изменить "
    "регистр букв, исправить орфографию и написание терминов (synx, шот, "
    "секвенция, локация, ассет, персонаж, раскадровка, воркфлоу, ComfyUI, LoRA, "
    "рендер, пилот, эпизод). Запрещено удалять, добавлять или заменять слова, "
    "менять числа и их форму. Верни только исправленный текст, без пояснений.\n\n"
    "Пример:\nвход: сделай апскейл шота ноль один и опубликуй версию\n"
    "выход: Сделай апскейл шота ноль один и опубликуй версию."
)


class VoiceEngine:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._online = None
        self._offline = None
        self._proc: subprocess.Popen | None = None
        self._input_stream = None
        self._audio_q: queue.Queue[bytes] = queue.Queue(maxsize=200)
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
        self._warming = False
        self._corrector = ""
        self._correct_seconds = 0.0
        self._llm_warming = False

    # --- config ---------------------------------------------------------
    def cfg(self) -> dict[str, Any]:
        return (load_config().get("voice") or {})

    def enabled(self) -> bool:
        return bool(self.cfg().get("enabled", True))

    def set_enabled(self, value: bool) -> dict[str, Any]:
        cfg = load_config()
        cfg.setdefault("voice", {})["enabled"] = bool(value)
        save_config(cfg)
        if value:
            self.warmup()
        else:
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
            input_stream = self._input_stream
            self._input_stream = None
            proc = self._proc
            self._proc = None
            self._running = False
            self._online = None
            self._offline = None
            gc.collect()
            if self._status not in ("recording", "finalizing"):
                self._status = "idle"
        if input_stream is not None:
            try:
                input_stream.stop()
                input_stream.close()
            except Exception:
                pass
        if proc is not None:
            try:
                proc.terminate()
            except Exception:
                pass
        self._unload_corrector()

    def _unload_corrector(self) -> None:
        """Free the local LLM from VRAM when voice input is turned off."""
        c = self._correct_cfg()
        if str(c.get("provider") or "").lower() != "ollama":
            return
        try:
            base = str(c.get("base_url") or "http://127.0.0.1:11434").rstrip("/")
            requests.post(base + "/api/generate", json={
                "model": str(c.get("model") or "qwen2.5:7b-instruct-q4_K_M"),
                "keep_alive": 0,
            }, timeout=5)
        except Exception:
            pass

    def unload_model(self, model: str | None = None) -> dict[str, Any]:
        c = self._correct_cfg()
        name = str(model or c.get("model") or "").strip()
        if not name:
            return {"unloaded": []}
        base = str(c.get("base_url") or "http://127.0.0.1:11434").rstrip("/")
        r = requests.post(base + "/api/generate", json={
            "model": name, "keep_alive": 0}, timeout=10)
        r.raise_for_status()
        return {"unloaded": [name]}

    # --- warm-up --------------------------------------------------------
    def warmup(self) -> None:
        """Preload models and run a dummy inference in the background."""
        if not self.enabled():
            return
        if self._online is not None and self._offline is not None:
            return
        with self._lock:
            if self._warming or self._running:
                return
            self._warming = True
        threading.Thread(target=self._warm_worker, daemon=True, name="voice-warmup").start()

    def _warm_worker(self) -> None:
        try:
            self._load()
            dummy = np.zeros(8000, dtype=np.float32)
            if self._online is not None:
                st = self._online.create_stream()
                st.accept_waveform(8000, dummy)
                while self._online.is_ready(st):
                    self._online.decode_stream(st)
            if self._offline is not None:
                st = self._offline.create_stream()
                st.accept_waveform(16000, np.zeros(16000, dtype=np.float32))
                self._offline.decode_stream(st)
        except Exception as exc:
            with self._lock:
                self._error = str(exc)[:300]
                self._status = "error"
        finally:
            with self._lock:
                self._warming = False

    # --- capture --------------------------------------------------------
    def start(self) -> dict[str, Any]:
        if not self.enabled():
            raise RuntimeError("голосовой ввод выключен")
        windows = platform.system() == "Windows"
        with self._lock:
            if self._running:
                return self.status()
            self._partial = ""
            self._samples = np.zeros(0, dtype=np.float32)
            self._stream = None
            self._proc = None
            self._input_stream = None
            self._audio_q = queue.Queue(maxsize=200)
            device = str(self.cfg().get("audio_device") or "").strip()
            if windows:
                try:
                    self._start_windows_capture(device)
                except Exception as exc:
                    self._status = "error"
                    self._error = str(exc)[:300]
                    raise RuntimeError(self._error)
                target = self._reader_windows
            else:
                cmd = ["arecord", "-q", "-f", "S16_LE", "-r", "16000", "-c", "1", "-t", "raw"]
                if device:
                    cmd = ["arecord", "-q", "-D", device, "-f", "S16_LE", "-r", "16000",
                           "-c", "1", "-t", "raw"]
                self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                              stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
                target = self._reader
            self._running = True
            self._status = "recording"
            self._started_at = time.time()
            self._error = None
            self._thread = threading.Thread(target=target, daemon=True, name="voice-reader")
            self._thread.start()
        self.warmup()
        # Load the correction model on the GPU while the user is speaking, so
        # the final pass does not pay the model load time after release.
        self.warm_corrector()
        time.sleep(0.15)
        if windows:
            if self._input_stream is None:
                self._running = False
                self._status = "error"
                self._error = "микрофон недоступен (sounddevice не запустился)"
                raise RuntimeError(self._error)
        elif self._proc is not None and self._proc.poll() is not None:
            self._running = False
            self._status = "error"
            self._error = "микрофон недоступен (arecord завершился)"
            raise RuntimeError(self._error)
        return self.status()

    def _start_windows_capture(self, device: str) -> None:
        """WASAPI capture through sounddevice (Windows only)."""
        try:
            import sounddevice as sd
        except Exception as exc:
            raise RuntimeError("для голосового ввода установи sounddevice") from exc
        dev = None
        if device:
            dev = int(device) if device.isdigit() else device
        q = self._audio_q

        def callback(indata, frames, time_info, status) -> None:  # noqa: ANN001
            try:
                q.put_nowait(bytes(indata))
            except queue.Full:
                pass

        stream = sd.RawInputStream(
            samplerate=16000, channels=1, dtype="int16", blocksize=3200,
            device=dev, callback=callback,
        )
        stream.start()
        self._input_stream = stream

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
            self._process_chunk(chunk)

    def _reader_windows(self) -> None:
        while True:
            with self._lock:
                if not self._running:
                    return
            try:
                chunk = self._audio_q.get(timeout=0.3)
            except queue.Empty:
                continue
            self._process_chunk(chunk)

    def _process_chunk(self, chunk: bytes) -> None:
        x = np.frombuffer(chunk, dtype=np.int16).astype(np.float32) / 32768.0
        if len(x) < 2:
            return
        with self._lock:
            self._samples = np.concatenate([self._samples, x])[-60 * 16000:]
            online = self._online
            stream = self._stream
            need_init = online is not None and stream is None
            buffered = self._samples.copy() if need_init else None
        if online is None:
            # models are still warming up: keep the audio buffered
            return
        try:
            if need_init:
                stream = online.create_stream()
                with self._lock:
                    self._stream = stream
                data = buffered if buffered is not None else x
                n = len(data) - (len(data) % 2)
                data8 = (data[0:n:2] + data[1:n:2]) * 0.5
                stream.accept_waveform(8000, data8)
                while online.is_ready(stream):
                    online.decode_stream(stream)
                with self._lock:
                    if self._running:
                        self._partial = (online.get_result(stream) or "").strip()
                return
            # 16k -> 8k with a cheap 2-tap anti-alias (pair average)
            n = len(x) - (len(x) % 2)
            x8 = (x[0:n:2] + x[1:n:2]) * 0.5
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
            input_stream = self._input_stream
            self._input_stream = None
            samples = self._samples.copy()
            partial = self._partial
        if input_stream is not None:
            try:
                input_stream.stop()
            except Exception:
                pass
            try:
                input_stream.close()
            except Exception:
                pass
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
        deadline = time.time() + 12
        while self._offline is None and time.time() < deadline:
            time.sleep(0.1)
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
                  "corrector": self._corrector,
                  "correct_seconds": self._correct_seconds,
                  "seconds": round(len(samples) / 16000.0, 2)}
        with self._lock:
            self._status = "ready"
            self._stream = None
            self._last_result = result
            self._final_cv.notify_all()
        return result

    # --- final text correction -----------------------------------------
    def _correct_cfg(self) -> dict[str, Any]:
        return dict(self.cfg().get("correct") or {})

    def _ollama_correct(self, text: str, c: dict[str, Any]) -> str:
        base = str(c.get("base_url") or "http://127.0.0.1:11434").rstrip("/")
        model = str(c.get("model") or "qwen2.5:7b-instruct-q4_K_M")
        # Enough room for the same text plus punctuation, but never runaway.
        base_predict = int(c.get("num_predict") or 1024)
        num_predict = max(base_predict, min(4096, len(text) // 2 + 64))
        payload = {
            "model": model,
            "stream": False,
            "think": False,
            "keep_alive": str(c.get("keep_alive") or "5m"),
            "options": {
                "temperature": float(c.get("temperature", 0)),
                "num_predict": num_predict,
            },
            "messages": [
                {"role": "system", "content": CORRECT_SYSTEM},
                {"role": "user", "content": text},
            ],
        }
        r = requests.post(base + "/api/chat", json=payload,
                          timeout=float(c.get("timeout", 30)))
        r.raise_for_status()
        data = r.json()
        return str(((data.get("message") or {}).get("content")) or "").strip()

    def _ai_correct(self, text: str) -> str:
        from ai import AIClient
        ai = AIClient()
        if not ai.ready:
            return ""
        messages = [
            {"role": "system", "content": CORRECT_SYSTEM},
            {"role": "user", "content": text},
        ]
        tokens = min(2048, max(512, len(text) // 2 + 64))
        out = ""
        # Disable thinking for the corrector: reasoning models otherwise spend
        # the whole budget on "thinking" and return an empty final message.
        # Retry a couple of times because providers occasionally ignore it.
        for attempt in range(3):
            extra = ({"thinking": {"type": "disabled"}}
                     if attempt < 2 else {"reasoning_effort": "none"})
            try:
                out, _ = ai.chat(messages, max_tokens=tokens, extra=extra)
            except Exception:
                out = ""
            if out:
                break
        return (out or "").strip()

    def _correct(self, text: str) -> str:
        self._correct_seconds = 0.0
        if not text:
            return text
        cfg = self.cfg()
        if not bool(cfg.get("llm_correct", True)):
            self._corrector = "off"
            return text
        c = self._correct_cfg()
        provider = str(c.get("provider") or "ollama").strip().lower()
        if provider in ("none", "off", "disabled"):
            self._corrector = "off"
            return text
        self._corrector = provider
        started = time.time()
        try:
            if provider == "ollama":
                out = self._ollama_correct(text, c)
            else:
                out = self._ai_correct(text)
        except Exception:
            self._correct_seconds = round(time.time() - started, 3)
            return text
        self._correct_seconds = round(time.time() - started, 3)
        out = (out or "").strip().strip('"').strip("«»").strip()
        # Never let a broken model replace the transcript with garbage.
        if not out or len(out) < len(text) * 0.5 or len(out) > len(text) * 2:
            return text
        if out[0].islower():
            out = out[0].upper() + out[1:]
        return out

    def warm_corrector(self) -> None:
        """Ask Ollama to load the correction model into VRAM in background."""
        c = self._correct_cfg()
        if str(c.get("provider") or "").lower() != "ollama":
            return
        if not bool(self.cfg().get("llm_correct", True)):
            return
        with self._lock:
            if self._llm_warming:
                return
            self._llm_warming = True

        def worker() -> None:
            try:
                base = str(c.get("base_url") or "http://127.0.0.1:11434").rstrip("/")
                requests.post(base + "/api/chat", json={
                    "model": str(c.get("model") or "qwen2.5:7b-instruct-q4_K_M"),
                    "stream": False,
                    "think": False,
                    "keep_alive": str(c.get("keep_alive") or "5m"),
                    "messages": [{"role": "user", "content": "ok"}],
                    "options": {"temperature": 0, "num_predict": 1},
                }, timeout=min(60.0, float(c.get("timeout", 30))))
            except Exception:
                pass
            finally:
                with self._lock:
                    self._llm_warming = False

        threading.Thread(target=worker, daemon=True, name="voice-llm-warmup").start()

    def test_correct(self, text: str | None = None) -> dict[str, Any]:
        sample = (text or "это тест коррекции распознавания речи шот секвенция локация "
                          "персонаж и рендер").strip()
        out = self._correct(sample)
        c = self._correct_cfg()
        provider = str(c.get("provider") or "ollama")
        main_model = str((load_config().get("ai") or {}).get("model") or "AI")
        info: dict[str, Any] = {
            "provider": provider,
            "model": c.get("model") if provider == "ollama" else main_model,
            "seconds": self._correct_seconds,
            "text": out,
            "ok": bool(out and out != sample),
        }
        if provider == "ollama":
            try:
                base = str(c.get("base_url") or "http://127.0.0.1:11434").rstrip("/")
                ps = requests.get(base + "/api/ps", timeout=3).json()
                for m in ps.get("models") or []:
                    if str(m.get("name") or "").startswith(str(c.get("model") or "")):
                        info["vram_mb"] = int(int(m.get("size_vram") or 0) / 1048576)
                        info["gpu"] = bool(m.get("size_vram"))
                        break
            except Exception:
                pass
        return info

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
                    "warming": self._warming, "error": self._error,
                    "corrector": self._corrector,
                    "correct_seconds": self._correct_seconds}


engine = VoiceEngine()


def warmup() -> None:
    engine.warmup()


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
