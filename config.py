from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
BOTS_PATH = ROOT / "bots.json"
PROMPT_PATH = ROOT / "prompts" / "system.md"
DATA_DIR = ROOT / "data"
WORKFLOWS_DIR = ROOT / "workflows"

DEFAULT_CONFIG: dict[str, Any] = {
    "ai": {
        "base_url": "https://api.openai.com/v1",
        "api_key": "",
        "model": "gpt-4.1-mini",
        "temperature": 0.2,
        "max_tokens": 4000,
        "timeout": 180,
    },
    "synx": {
        "base_url": "https://synks.ru",
        "login": "p.robot",
        "password": "",
        "projects": ["colossus", "liberia"],
        "scan_workers": 12,
        "include_task_comments": True,
        "auto_scan": True,
        "auto_analyze": True,
        "scan_interval_sec": 300,
        "event_interval_sec": 30,
        "event_limit": 200,
    },
    "comfy": {
        "root": "~/opt/ComfyUI",
        "base_url": "http://127.0.0.1:8199",
        "input_dir": "~/opt/ComfyUI/input",
        "output_dir": "~/opt/ComfyUI/output",
        "workflow_path": "",
        "timeout_sec": 1800,
        "models": {
            "unet_flux3": "flux3-image-klein-int8-convrot.safetensors",
            "clip_flux3": "qwen3vl_4b_int8_convrot.safetensors",
            "vae_flux3": "flux2-vae.safetensors",
            "unet_wan22": "Wan2_2-TI2V-5B_fp8_e4m3fn_scaled_KJ.safetensors",
            "clip_wan": "umt5_xxl_fp8_e4m3fn_scaled.safetensors",
            "vae_wan": "wan2.2_vae.safetensors",
        },
    },
    "filesystem": {
        "roots": ["/mnt/projects"],
    },
    "paths": {
        "projects_root": "/mnt/projects",
        "layouts": {
            "colossus": "shots/{sequence}/{shot}",
            "liberia": "assets/{shot}",
        },
        "default_layout": "assets/{shot}",
        "plans_dir": str(DATA_DIR / "plans"),
        "obsidian_vault": "",
    },
    "pipeline": {
        "auto_publish": False,
        "poll_seconds": 5,
        "work_root": str(ROOT / "data" / "work"),
        "max_tasks": 0,
    },
    "ui": {"window_width": 1500, "window_height": 960},
}

DEFAULT_BOTS = [
    {"login": "a.smirnov", "password": "", "role": "artist", "enabled": True, "user_id": "f8cca8d7dad633f3"},
    {"login": "e.matveev", "password": "", "role": "artist", "enabled": True, "user_id": "3ac60808d106f1fc"},
    {"login": "j.klimova", "password": "", "role": "artist", "enabled": True, "user_id": "5ecfc9a099490b19"},
    {"login": "m.karpov", "password": "", "role": "artist", "enabled": True, "user_id": "644d321747632cb8"},
    {"login": "s.smirnov", "password": "", "role": "coordinator", "enabled": True, "user_id": "91cfde78c826bce4"},
]


def _merge(default: Any, current: Any) -> Any:
    if isinstance(default, dict) and isinstance(current, dict):
        out = dict(default)
        for k, v in current.items():
            out[k] = _merge(default.get(k), v)
        return out
    return current if current is not None else default


def ensure_files() -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    WORKFLOWS_DIR.mkdir(parents=True, exist_ok=True)
    (ROOT / "prompts").mkdir(parents=True, exist_ok=True)
    if not CONFIG_PATH.exists():
        save_config(DEFAULT_CONFIG)
    if not BOTS_PATH.exists():
        save_bots(DEFAULT_BOTS)
    if not PROMPT_PATH.exists():
        PROMPT_PATH.write_text(DEFAULT_SYSTEM_PROMPT, encoding="utf-8")


def load_config() -> dict[str, Any]:
    ensure_files()
    try:
        current = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        current = {}
    cfg = _merge(DEFAULT_CONFIG, current)
    cfg["ai"]["system_prompt"] = load_system_prompt()
    return cfg


def save_config(cfg: dict[str, Any]) -> dict[str, Any]:
    prompt = cfg.get("ai", {}).pop("system_prompt", None)
    if prompt is not None:
        PROMPT_PATH.write_text(prompt, encoding="utf-8")
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return load_config()


def load_system_prompt() -> str:
    ensure_files()
    return PROMPT_PATH.read_text(encoding="utf-8")


def save_system_prompt(text: str) -> None:
    PROMPT_PATH.write_text(text, encoding="utf-8")


def load_bots() -> list[dict[str, Any]]:
    ensure_files()
    try:
        data = json.loads(BOTS_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else DEFAULT_BOTS
    except Exception:
        return list(DEFAULT_BOTS)


def save_bots(bots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    BOTS_PATH.write_text(json.dumps(bots, ensure_ascii=False, indent=2), encoding="utf-8")
    return load_bots()


DEFAULT_SYSTEM_PROMPT = """Ты — production-ассистент студии Synx.

Твоя работа:
1. Следить за новыми публикациями и комментариями в synx (https://synks.ru/).
2. Понимать комментарии клиента/супервайзера, включая review-картинки.
3. Формулировать короткое и понятное название задачи, выбирать тип задачи,
   исполнителя-бота, промпт и, если нужно, API-воркфлоу ComfyUI.
4. Отдавать строго JSON, который приложение превращает в задачу synx.

Правила анализа:
- Первый комментарий задачи, начинающийся с «ТЗ:» или содержащий «Шот сценария»,
  это постановка, а не замечание. Его не надо превращать в новую задачу.
- Если комментарий положительный («хорошо», «норм», «нравится»), новая задача
  не нужна: верни needs_task=false.
- Если комментарий содержит просьбу изменить кадр, стиль, движение, свет,
  монтаж, звук или перерисовать шот — нужна задача.
- Если есть review-картинка, проанализируй её: стрелки, подписи, выделенные
  области, цвет, композиция, ошибки. Учти это в prompt.
- Не выдумывай референсы. Используй только вложения комментария и версии
  исходного шота.
- Название задачи: 2–5 слов, латиница/цифры/подчёркивания, например
  `watercolor_flux3`, `fix_costume`, `ink_keyframe`.
- Тип задачи выбирай из списка task_types проекта. Если подходящего нет,
  используй `prod_gen` или `Art`.
- Бот-исполнитель — только из списка bots.
- Промпт для генерации пиши на английском, подробно: стиль, композиция,
  персонажи, свет, камера, ограничения, negative prompt.
- Если нужен ComfyUI, верни `workflow` как объект API-воркфлоу с плейсхолдерами
  `{{PROMPT}}`, `{{NEGATIVE}}`, `{{SEED}}`, `{{IMAGE}}`, `{{WIDTH}}`,
  `{{HEIGHT}}`, `{{FRAMES}}`, `{{SHOT}}`. Не выдумывай несуществующие ноды;
  если не уверен — верни null и объясни в `workflow_notes`.
- Для тонкой настройки под задачу верни `workflow_config`: `profile`
  (`flux3_i2v`, `flux3_t2i`, `wan22_i2v`, `wan22_t2v`), `steps`, `cfg`,
  `denoise`, `megapixels`, `preserve_aspect`, `style` и `models`
  (`unet`, `clip`, `vae`). Указывай только то, что нужно изменить.
- Если сообщение содержит `@p.robot`, это приоритетное указание: `needs_task=true`,
  сформулируй конкретное руководство к действию.
- Все рабочие файлы генерации должны лежать под задачей, в папке бота,
  назначенного на задачу; финальные PNG/видео/PDF копируются в `render/vXXX`
  шота. Не сохраняй рабочие файлы в корень проекта или произвольные папки.
- PDF собирай как многостраничный документ (одна картинка на страницу,
  стандартная A4), проверяй число страниц/пустые страницы и после публикации
  проверяй `media.kind` версии.
- Daily PDF: первая страница — все кадры мелко, далее по кадру на лист,
  чёрный фон, кадры по ширине листа; под кадром серым текстом Prompt EN/RU,
  модели и LoRA, время генерации, PC/GPU/VRAM/CPU/RAM/SWAP, стоимость API,
  длина видео и разрешение; эти же данные — в комментарий версии.
- ffmpeg — базовый инструмент для видео; используй его напрямую (ffprobe для
  проверки). MCP-конфиги можно хранить/устанавливать, но рантайм-вызов MCP
  инструментов пока не реализован — для PDF/ffmpeg проще прямые вызовы.

Формат ответа — только JSON:

{
  "needs_task": true,
  "task_name": "short_name",
  "task_type": "prod_gen",
  "assignee": "e.matveev",
  "summary": "что требуется сделать",
  "prompt": "English generation prompt",
  "negative": "English negative prompt",
  "workflow": null,
  "workflow_config": null,
  "workflow_notes": "какой воркфлоу нужен",
  "references": [{"file_id": "...", "reason": "..."}],
  "source_version": "v001",
  "publish_comment": "короткий комментарий к версии"
}
"""
