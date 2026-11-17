from __future__ import annotations

import base64
import io
import json
import re
from pathlib import Path
from typing import Any

import requests
from PIL import Image

from config import DATA_DIR, load_config
from synx import SynxClient

requests.packages.urllib3.disable_warnings()


class AIError(RuntimeError):
    pass


def _data_url(path: Path) -> str:
    im = Image.open(path).convert("RGB")
    im.thumbnail((1400, 1400), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=86)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def _extract_json(text: str) -> dict[str, Any]:
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
    text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except Exception:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        return json.loads(text[start:end + 1])
    raise AIError("AI did not return JSON: " + text[:500])


class AIClient:
    def __init__(self, cfg: dict[str, Any] | None = None):
        self.cfg = cfg or load_config()
        self.ai = self.cfg["ai"]

    @property
    def ready(self) -> bool:
        return bool(self.ai.get("api_key") and self.ai.get("base_url") and self.ai.get("model"))

    def chat_message(self, messages: list[dict[str, Any]],
                     tools: list[dict[str, Any]] | None = None) -> tuple[dict[str, Any], dict[str, int]]:
        if not self.ready:
            raise AIError("AI provider is not configured: set base_url, api_key and model")
        base = str(self.ai["base_url"]).rstrip("/")
        url = base + "/chat/completions"
        headers = {"Authorization": f"Bearer {self.ai['api_key']}", "Content-Type": "application/json"}
        payload = {
            "model": self.ai["model"],
            "messages": messages,
            "temperature": float(self.ai.get("temperature", 0.2)),
            "max_tokens": int(self.ai.get("max_tokens", 4000)),
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        r = requests.post(url, headers=headers, json=payload,
                          timeout=int(self.ai.get("timeout", 180)))
        if r.status_code >= 400:
            raise AIError(f"AI HTTP {r.status_code}: {r.text[:1500]}")
        data = r.json()
        try:
            msg = data["choices"][0]["message"]
        except Exception:
            raise AIError("unexpected AI response: " + json.dumps(data)[:1200])
        usage = data.get("usage") or {}
        return msg, {
            "input": int(usage.get("prompt_tokens") or 0),
            "output": int(usage.get("completion_tokens") or 0),
        }

    def chat(self, messages: list[dict[str, Any]]) -> tuple[str, dict[str, int]]:
        msg, usage = self.chat_message(messages)
        return msg.get("content") or "", usage

    def test(self) -> dict[str, Any]:
        text, usage = self.chat([
            {"role": "system", "content": "Reply with exactly: OK"},
            {"role": "user", "content": "test"},
        ])
        return {"ok": True, "reply": text[:200], "usage": usage, "model": self.ai.get("model")}

    def analyze_publication(self, pub: dict[str, Any], project: dict[str, Any],
                            task_types: list[str], bots: list[dict[str, Any]],
                            synx: SynxClient) -> dict[str, Any]:
        attachments = pub.get("attachments") or []
        ref_dir = DATA_DIR / "refs" / str(pub.get("comment_id") or pub.get("id"))
        ref_dir.mkdir(parents=True, exist_ok=True)
        image_parts: list[dict[str, Any]] = []
        saved_refs: list[dict[str, Any]] = []
        for a in attachments[:6]:
            fid = a.get("file_id")
            if not fid:
                continue
            name = a.get("name") or f"{fid}.png"
            dst = ref_dir / re.sub(r"[^A-Za-z0-9_.-]+", "_", name)
            try:
                synx.download_file(fid, dst)
                mime = (a.get("mime") or "").lower()
                if dst.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".avif", ".bmp"} or mime.startswith("image/"):
                    if dst.suffix.lower() == ".avif":
                        # Pillow may not decode AVIF; ffmpeg conversion is handled by caller if needed
                        pass
                    image_parts.append({"type": "image_url", "image_url": {"url": _data_url(dst)}})
                saved_refs.append({"file_id": fid, "name": name, "path": str(dst), "mime": mime})
            except Exception:
                saved_refs.append({"file_id": fid, "name": name, "error": "download failed"})

        system = self.ai.get("system_prompt") or ""
        context = {
            "project": {"id": project.get("id"), "alias": project.get("alias"), "name": project.get("name")},
            "shot": pub.get("shot"),
            "thread_kind": pub.get("thread_kind"),
            "version": pub.get("version_number"),
            "author": pub.get("author"),
            "comment": pub.get("body"),
            "task_types": task_types,
            "bots": [b.get("login") for b in bots if b.get("enabled", True)],
            "attachments": [a.get("name") for a in attachments],
            "saved_references": saved_refs,
        }
        prompt = (
            "Проанализируй публикацию synx и верни только JSON по заданной схеме. "
            "Если задача не нужна — needs_task=false. Контекст:\n" +
            json.dumps(context, ensure_ascii=False, indent=2)
        )
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        content.extend(image_parts)
        text, usage = self.chat([
            {"role": "system", "content": system},
            {"role": "user", "content": content},
        ])
        plan = _extract_json(text)
        plan["_usage"] = usage
        plan["_raw"] = text
        plan["_references_saved"] = saved_refs
        return plan
