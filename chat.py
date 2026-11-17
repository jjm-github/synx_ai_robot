from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import requests
from PIL import Image

import db
import fs_tools
import publish_flow
import synx_tools
import system_info
from ai import AIClient, AIError, _data_url
from config import ROOT, load_config
from workflows import flux3_i2v, flux3_t2i

requests.packages.urllib3.disable_warnings()

CHAT_DIR = ROOT / "data" / "chat"
UPLOADS = CHAT_DIR / "uploads"
IMAGES = CHAT_DIR / "images"
for _p in (UPLOADS, IMAGES):
    _p.mkdir(parents=True, exist_ok=True)

CHAT_SYSTEM = """Ты — ассистент Synx AI Robot. Отвечай по-русски, кратко и по делу.

Ты умеешь:
- отвечать на вопросы о производстве, synx, версиях, задачах, проектах, ComfyUI и промптах;
- анализировать прикреплённые пользователем изображения;
- ходить в synx по API с данными из настроек приложения;
- находить конкретную версию по проекту, шоту и коду версии;
- генерировать изображения в локальном ComfyUI, в том числе из кадра версии;
- собирать плейлист Synx из опубликованных версий за период.

Инструменты synx:
- synx_projects — список проектов;
- synx_find_version — найти одну версию по project + shot + version (возвращает компактные метаданные);
- synx_versions — версии проекта за период, можно фильтровать по shot и version;
- synx_tasks — задачи проекта;
- synx_make_playlist — готовый текст плейлиста Synx за период.

Если пользователь спрашивает про состояние проектов/версий/задач — сначала получи
данные через synx-инструмент, не выдумывай. У тебя ЕСТЬ доступ к synx через API;
если в истории чата есть старые ответы «доступа нет» — это устаревшая информация,
не повторяй её. Если просит плейлист — вызови
synx_make_playlist и укажи project и даты. Сегодняшняя дата известна из контекста.

## Генерация из конкретной версии synx

Если пользователь просит найти версию (project, shot, vXXX) и сгенерировать по ней
картинку — НЕ вызывай synx_versions, synx_tasks и fs_list. Сразу вызови
generate_from_synx_version ОДИН раз и передай project, shot, version, prompt/style.
Этот инструмент сам скачает версию, возьмёт кадр (для видео — средний), сохранит
пропорции исходника и соберёт FLUX.3 workflow. Если проект указан неверно, он сам
найдёт шот в другом проекте и вернёт project_corrected=true.

Для обычной генерации по тексту или по прикреплённому изображению вызывай
generate_image. В prompt передавай подробный английский промпт. Если есть
референс — передай его путь в reference_paths и preserve_aspect=true.

## Рабочие файлы генераций

Все рабочие файлы генерации — PNG/JPG, видео, workflow JSON, PDF, meta,
README — должны лежать под задачей, в папке бота, назначенного на задачу:

`<project>/shots/<sequence>/<shot>/<task_type_lower>/work/<bot>/<task_name>_vXXX/`

Финальные изображения/видео/PDF копируй в `<shot>/render/vXXX/`. Не сохраняй
рабочие файлы в корень проекта, `render/` без `vXXX` или произвольные папки.
Превью в `data/chat` — временное; канонические файлы задачи — только в папке
задачи/бота. Перед публикацией проверь, что файлы уже лежат там.

## Публикация кадров из чата

Если пользователь просит «сделай PDF из этих кадров», «опубликуй версию»,
«создай задачу Art», «назначь бота», «разложи workflow и картинки в рабочую
папку и render» — вызови publish_images_as_version ОДИН раз. Этот инструмент:
сам возьмёт последние сгенерированные картинки из чата, соберёт PDF (одна
картинка на страницу), создаст задачу в synx, выберет и назначит бота по
скилам из synx API, скопирует картинки и workflow JSON в рабочую папку задачи
и в render шота, а затем опубликует PDF как версию от имени бота.

Не отвечай, что у тебя нет инструментов создания задачи/назначения/публикации —
они есть. Если нужен список ботов и их скилов, вызови synx_bots.

Для отдельной склейки картинок в PDF используй make_pdf_from_images (одна
картинка на страницу, стандартная страница A3, поля и проверка). Для создания
задачи, раскладки файлов и публикации PDF версией используй
publish_images_as_version. Перед публикацией PDF всегда проверяй
`verify_pdf`: число страниц, размер страницы и отсутствие пустых страниц.
После публикации проверь `media.kind` версии: если он `null`, synx не
распознал PDF для этого шота/типа задачи — плеер может показать пустоту.
В таком случае опубликуй preview-версию (контактный лист/слайдшоу) или
используй PDF-friendly задачу/asset, а PDF оставь файлом задачи/публикацией.
Если PDF-версия пустая или плохая — используй repair_pdf_task: он пересоберёт
PDF на стандартных страницах, проверит его и опубликует новую версию.

Не повторяй один и тот же вызов инструмента с теми же аргументами. Если результата
достаточно — переходи к генерации или отвечай текстом. Для списков версий
используй shot/version фильтры и limit не больше 50.

## Daily PDF

Если просят «дэйли», контактный лист, PDF с кадрами и подписями — вызывай
`publish_daily_pdf` ОДИН раз. Он делает:

- стр. 1 — все кадры мелко (контактный лист);
- далее по одному кадру на страницу, чёрный фон, кадр по ширине листа
  (боковые края совпадают с границами);
- под кадром серым текстом (белый ~50%): Prompt EN/RU, модели и LoRA,
  время генерации, PC/GPU/VRAM/CPU/RAM/SWAP, стоимость API, длина видео,
  разрешение;
- те же данные — в комментарий публикуемой версии.

Не ходи по кругу через fs_list/synx_versions: один вызов publish_daily_pdf.

### Пересборка по базовой версии

Если пользователь ссылается на версию («версия 13», «v013») и просит изменить
layout — чёрный фон, 300 dpi, текст на 20% меньше, кадр по центру, без полей —
это пересборка, а не новая генерация. Бери кадры задачи и вызывай
`publish_daily_pdf` ОДИН раз с:

- `base_version` — версия, на которую ссылаются;
- `dpi` — 300, если сказано «300 dpi»;
- `text_scale` — 0.8, если «текст на 20% меньше»;
- `image_valign` — `center`, если «по центру/по вертикали»;
- `background` — `#000000` для чёрных страниц.

Не используй `publish_images_as_version` для таких правок и не генерируй
кадры заново. Опирайся на версию, которую назвал пользователь.

## ffmpeg и MCP

`ffmpeg`/`ffprobe` установлены и являются базовыми инструментами: используй
инструмент `ffmpeg` напрямую для видео (нарезка, склейка, слайдшоу, кадры,
проверка ffprobe). В программе есть управление MCP-конфигами (поиск/установка/
список), но вызов MCP-инструментов в рантайме пока не реализован. Для PDF и
ffmpeg проще и надёжнее прямые вызовы встроенных инструментов; MCP для этого
не обязателен.

## Сводка по проекту

Если пользователь спрашивает «что нового», «новости», «сводка», «что происходит»,
«обзор» или «резюме» по проекту — СНАЧАЛА вызови synx_project_news. Этот инструмент
читает локальную таблицу уже собранных сообщений, сравнивает с прошлым просмотром
этого чата и возвращает счётчики, топ шотов, действия, упоминания @p.robot и версии.
На основе результата сделай краткое резюме: что нового за период, где ближайший
фронт работ, что требует действий, какие были новые версии. Не выдумывай факты.
"""

IMAGE_TOOL = {
    "type": "function",
    "function": {
        "name": "generate_image",
        "description": ("Generate an image in local ComfyUI. Use reference_paths for local images "
                        "and preserve_aspect=true to keep the reference proportions. For a specific "
                        "synx version prefer generate_from_synx_version."),
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "Detailed English image prompt"},
                "negative": {"type": "string", "description": "English negative prompt"},
                "reference_paths": {"type": "array", "items": {"type": "string"},
                                    "description": "Local image paths to use as references"},
                "style": {"type": "string", "description": "Style preset, e.g. cubism, watercolor, ink"},
                "workflow": {"type": "string", "enum": ["auto", "flux3_i2v", "flux3_t2i"],
                             "description": "ComfyUI workflow profile"},
                "preserve_aspect": {"type": "boolean", "default": True},
                "width": {"type": "integer"},
                "height": {"type": "integer"},
                "steps": {"type": "integer"},
                "cfg": {"type": "number"},
                "denoise": {"type": "number"},
                "frame": {"type": "string", "enum": ["first", "middle", "last"]},
            },
            "required": ["prompt"],
        },
    },
}

VERSION_GEN_TOOL = {
    "type": "function",
    "function": {
        "name": "generate_from_synx_version",
        "description": ("Find an exact synx version by project/shot/version, download it, extract a frame "
                        "(middle frame for video), and generate a new image from it in one call. "
                        "Use this for requests like 'find p002_0130 v004 and generate a cubist image'. "
                        "Do NOT call synx_versions, synx_tasks or fs_list for this."),
        "parameters": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "Project alias/name/id (may be corrected automatically)"},
                "shot": {"type": "string", "description": "Entity/shot name, e.g. p002_0130"},
                "version": {"type": "string", "description": "Version code, e.g. v004 or 4"},
                "prompt": {"type": "string", "description": "English prompt for the new image"},
                "style": {"type": "string", "description": "Style, e.g. cubism, watercolor, ink"},
                "negative": {"type": "string"},
                "frame": {"type": "string", "enum": ["first", "middle", "last"], "default": "middle"},
                "preserve_aspect": {"type": "boolean", "default": True},
                "workflow": {"type": "string", "enum": ["flux3_i2v", "flux3_t2i"], "default": "flux3_i2v"},
                "steps": {"type": "integer"},
                "cfg": {"type": "number"},
                "denoise": {"type": "number"},
                "width": {"type": "integer"},
                "height": {"type": "integer"},
                "unet": {"type": "string"},
                "clip": {"type": "string"},
                "vae": {"type": "string"},
            },
            "required": ["project", "shot", "prompt"],
        },
    },
}

PUBLISH_TOOL = {
    "type": "function",
    "function": {
        "name": "publish_images_as_version",
        "description": ("Take the latest generated images from this chat (or explicit image_paths), "
                        "build a multi-page PDF (one image per page), create a synx task "
                        "(default type Art), assign a configured bot by its synx skills, copy the "
                        "images and workflow JSONs into the bot task work folder and the shot render "
                        "folder, and publish the PDF as a new version from that bot. Use this for "
                        "requests like 'сделай PDF из этих кадров, создай задачу Art, назначь бота и "
                        "опубликуй версию'."),
        "parameters": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "Project alias/name/id; inferred from chat if omitted"},
                "shot": {"type": "string", "description": "Shot/entity name; inferred from recent chat if omitted"},
                "version": {"type": "string", "description": "Source version, e.g. v004; latest if omitted"},
                "task_name": {"type": "string", "default": "cubism_frames"},
                "task_type": {"type": "string", "default": "Art"},
                "bot": {"type": "string", "description": "Bot login; if omitted the best available bot is chosen by skills"},
                "comment": {"type": "string", "description": "Task comment"},
                "version_comment": {"type": "string", "description": "Comment for the published version"},
                "pdf_name": {"type": "string", "description": "PDF filename without extension"},
                "page_size": {"type": "string", "enum": ["A4", "A3", "A2", "LETTER"], "default": "A4"},
                "margin_mm": {"type": "number", "default": 10},
                "dpi": {"type": "integer", "default": 144},
                "image_source": {"type": "string", "enum": ["last_chat_images", "paths"],
                                 "default": "last_chat_images"},
                "image_paths": {"type": "array", "items": {"type": "string"}},
                "dry_run": {"type": "boolean", "default": False},
            },
        },
    },
}

PDF_TOOL = {
    "type": "function",
    "function": {
        "name": "make_pdf_from_images",
        "description": ("Combine several images into a multi-page PDF, one image per page. "
                        "Use it when the user asks to glue frames into a PDF without publishing. "
                        "By default takes the latest generated images from this chat."),
        "parameters": {
            "type": "object",
            "properties": {
                "image_source": {"type": "string", "enum": ["last_chat_images", "paths"],
                                 "default": "last_chat_images"},
                "image_paths": {"type": "array", "items": {"type": "string"}},
                "pdf_name": {"type": "string", "description": "Output PDF name without extension"},
                "page_size": {"type": "string", "enum": ["A4", "A3", "A2", "LETTER"], "default": "A4"},
                "margin_mm": {"type": "number", "default": 10},
                "dpi": {"type": "integer", "default": 144},
            },
        },
    },
}

VERIFY_PDF_TOOL = {
    "type": "function",
    "function": {
        "name": "verify_pdf",
        "description": ("Check a PDF: page count, page size, file size and whether any page is blank. "
                        "Use it before publishing and when a PDF looks empty in synx."),
        "parameters": {
            "type": "object",
            "properties": {
                "pdf_path": {"type": "string"},
                "task_id": {"type": "integer", "description": "Local task id; PDF is taken from its result_file"},
                "expected_pages": {"type": "integer"},
            },
        },
    },
}

REPAIR_PDF_TOOL = {
    "type": "function",
    "function": {
        "name": "repair_pdf_task",
        "description": ("Rebuild a bad/empty PDF for a local task on standard pages, verify every page, "
                        "and publish the corrected PDF as a new synx version. Use when a PDF version "
                        "is empty or looks wrong."),
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {"type": "integer"},
                "page_size": {"type": "string", "enum": ["A4", "A3", "A2", "LETTER"], "default": "A4"},
                "margin_mm": {"type": "number", "default": 10},
                "dpi": {"type": "integer", "default": 144},
                "publish": {"type": "boolean", "default": True},
                "bot": {"type": "string"},
            },
            "required": ["task_id"],
        },
    },
}

DAILY_PDF_TOOL = {
    "type": "function",
    "function": {
        "name": "publish_daily_pdf",
        "description": ("Build and publish a DAILY PDF from a task's frames. Page 1 is a contact "
                        "sheet with all frames small; the remaining pages have one frame per page on "
                        "a black background, frame side edges aligned to the page borders, and gray "
                        "text below: English and Russian prompt, models/LoRA, generation time, PC/GPU/"
                        "VRAM/CPU/RAM/SWAP, API cost, video length and resolution. The same metadata is "
                        "written into the version comment. Use ONE call for daily/contact-sheet/PDF "
                        "requests; do not loop over fs_list/synx_versions."),
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {"type": "integer", "description": "Local task id with frame_*.png in its work folder"},
                "title": {"type": "string", "default": "DAILY"},
                "page_size": {"type": "string", "enum": ["A4", "A3"], "default": "A4"},
                "dpi": {"type": "integer", "default": 144},
                "text_scale": {"type": "number", "default": 1.0},
                "image_valign": {"type": "string", "enum": ["top", "center"], "default": "top"},
                "background": {"type": "string", "default": "#000000"},
                "base_version": {"type": "string", "description": "Reference version, e.g. v013"},
                "publish": {"type": "boolean", "default": True},
                "bot": {"type": "string"},
                "prompts": {"type": "array", "items": {"type": "string"},
                            "description": "English prompts per frame, in frame order"},
                "prompts_ru": {"type": "array", "items": {"type": "string"},
                               "description": "Russian prompts per frame, in frame order"},
                "titles": {"type": "array", "items": {"type": "string"}},
                "elapsed_sec": {"type": "number", "description": "Generation time per frame"},
                "elapsed_note": {"type": "string", "description": "e.g. estimate or exact"},
                "models": {"type": "object", "description": "unet/clip/vae used"},
                "loras": {"type": "array", "items": {"type": "string"}},
                "api_cost": {"type": "number"},
                "video_seconds": {"type": "number"},
                "resolution": {"type": "string"},
            },
            "required": ["task_id"],
        },
    },
}

SYNX_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "synx_projects",
            "description": "List synx projects.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "synx_bots",
            "description": ("List configured bots (with password availability) and their synx skills. "
                            "Use it to choose an assignee for a task."),
            "parameters": {
                "type": "object",
                "properties": {
                    "project": {"type": "string"},
                    "task_id": {"type": "string"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "synx_project_news",
            "description": ("Summarise new synx messages already collected in the local scan table "
                            "for a project and period. Use this FIRST when the user asks what is new, "
                            "a digest or the current front of work. It compares with the last view of "
                            "this chat session and returns counts, top shots, actions, @p.robot mentions, "
                            "latest versions and a text digest."),
            "parameters": {
                "type": "object",
                "properties": {
                    "project": {"type": "string"},
                    "date_from": {"type": "string", "description": "YYYY-MM-DD inclusive"},
                    "date_to": {"type": "string", "description": "YYYY-MM-DD inclusive"},
                    "mark_seen": {"type": "boolean", "default": True},
                },
                "required": ["project"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "synx_find_version",
            "description": ("Find one exact version by project, shot and version code. Returns compact "
                            "metadata: media kind, dimensions, frames, file id. Searches other projects "
                            "if the shot is not in the requested project."),
            "parameters": {
                "type": "object",
                "properties": {
                    "project": {"type": "string"},
                    "shot": {"type": "string"},
                    "version": {"type": "string", "description": "v004 or 4"},
                },
                "required": ["project", "shot"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "synx_versions",
            "description": "List published versions of a synx project for a date period, optionally filtered by shot and version.",
            "parameters": {
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Project alias, name or id"},
                    "date_from": {"type": "string", "description": "YYYY-MM-DD inclusive"},
                    "date_to": {"type": "string", "description": "YYYY-MM-DD inclusive"},
                    "limit": {"type": "integer", "default": 50},
                    "playable_only": {"type": "boolean", "default": False},
                    "shot": {"type": "string", "description": "Optional exact shot/entity name filter"},
                    "version": {"type": "string", "description": "Optional version code filter, e.g. v004"},
                },
                "required": ["project"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "synx_tasks",
            "description": "List tasks of a synx project, optionally filtered by status.",
            "parameters": {
                "type": "object",
                "properties": {
                    "project": {"type": "string"},
                    "status": {"type": "string"},
                    "limit": {"type": "integer", "default": 300},
                },
                "required": ["project"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "synx_make_playlist",
            "description": "Build a Synx Player playlist text from versions published in a period.",
            "parameters": {
                "type": "object",
                "properties": {
                    "project": {"type": "string"},
                    "date_from": {"type": "string"},
                    "date_to": {"type": "string"},
                    "limit": {"type": "integer", "default": 500},
                    "playable_only": {"type": "boolean", "default": True},
                },
                "required": ["project"],
            },
        },
    },
]

FS_TOOLS = [
    {"type": "function", "function": {
        "name": "fs_list",
        "description": "List allowed filesystem roots or a directory inside them.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Empty for roots, or absolute path inside an allowed root"}
        }},
    }},
    {"type": "function", "function": {
        "name": "fs_read",
        "description": "Read a text file or binary file inside an allowed root.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"},
            "max_bytes": {"type": "integer", "default": 300000}
        }, "required": ["path"]},
    }},
    {"type": "function", "function": {
        "name": "fs_write",
        "description": "Write a file inside an allowed root. Full access inside configured roots.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
            "base64_data": {"type": "boolean", "default": False}
        }, "required": ["path", "content"]},
    }},
    {"type": "function", "function": {
        "name": "fs_delete",
        "description": "Delete a file or directory inside an allowed root.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
    }},
]

FFMPEG_TOOL = {
    "type": "function",
    "function": {
        "name": "ffmpeg",
        "description": ("Run ffmpeg or ffprobe directly. Use for video probing, cutting, joining, "
                        "extracting frames, slideshows and checks. Pass args as an array without the "
                        "binary name. Example ffprobe: args=['-v','error','-show_streams','-of','json','file.mp4']."),
        "parameters": {
            "type": "object",
            "properties": {
                "binary": {"type": "string", "enum": ["ffmpeg", "ffprobe"], "default": "ffmpeg"},
                "args": {"type": "array", "items": {"type": "string"}},
                "timeout_sec": {"type": "integer", "default": 600},
            },
            "required": ["args"],
        },
    },
}

TOOLS = [IMAGE_TOOL, VERSION_GEN_TOOL, PDF_TOOL, VERIFY_PDF_TOOL, REPAIR_PDF_TOOL, DAILY_PDF_TOOL, PUBLISH_TOOL, FFMPEG_TOOL] + SYNX_TOOLS + FS_TOOLS
GEN_RE = re.compile(
    r"(нарисуй|сгенер|сгенерируй|сделай\s+(картин|изображ|кадр|арт)|"
    r"draw|generate\s+(an?\s+)?image|make\s+(an?\s+)?image|create\s+(an?\s+)?image)",
    re.I,
)
PLAYLIST_RE = re.compile(r"(плейлист|playlist)", re.I)
NEWS_RE = re.compile(r"(что\s+нового|новост|сводк|что\s+происход|обзор|резюме|какие\s+новости)", re.I)


def _synx_guide() -> str:
    p = ROOT / "prompts" / "synx_api.md"
    try:
        return p.read_text(encoding="utf-8")
    except Exception:
        return ""


def _system() -> str:
    roots = "\n".join("- " + r for r in fs_tools.roots()) or "- (нет папок)"
    return (CHAT_SYSTEM + f"\n\nСегодня: {date.today().isoformat()}.\n\n"
            "## Synx API\n\n" + _synx_guide() +
            "\n\n## Рабочие папки (полный доступ)\n" + roots)


def _refs_for_generation(images: list[dict[str, Any]]) -> list[str]:
    out = []
    for im in images or []:
        p = im.get("path") if isinstance(im, dict) else None
        if p and Path(p).is_file():
            out.append(str(p))
    return out


def _stage_refs(refs: list[str], input_dir: Path) -> list[str]:
    input_dir.mkdir(parents=True, exist_ok=True)
    names = []
    for i, p in enumerate(refs[:4]):
        src = Path(p)
        name = f"chat_ref_{uuid.uuid4().hex[:8]}_{i}{src.suffix.lower() or '.png'}"
        shutil.copy2(src, input_dir / name)
        names.append(name)
    return names


def _submit(workflow: dict[str, Any], cfg: dict[str, Any], out_dir: Path,
            progress=None) -> list[Path]:
    if progress:
        progress({"type": "stage", "text": "Отправляю workflow в ComfyUI..."})
    base = str(cfg["comfy"].get("base_url") or "http://127.0.0.1:8199").rstrip("/")
    r = requests.post(f"{base}/prompt", json={"prompt": workflow,
                      "client_id": f"chat-{uuid.uuid4().hex[:8]}"}, timeout=60)
    if r.status_code >= 400:
        raise AIError(f"ComfyUI /prompt {r.status_code}: {r.text[:1200]}")
    pid = r.json().get("prompt_id")
    deadline = time.time() + int(cfg["comfy"].get("timeout_sec", 1800))
    out_dir.mkdir(parents=True, exist_ok=True)
    files: list[Path] = []
    if progress:
        progress({"type": "stage", "text": "Жду генерации в ComfyUI..."})
    while time.time() < deadline:
        time.sleep(2)
        h = requests.get(f"{base}/history/{pid}", timeout=30).json()
        item = h.get(pid)
        if not item:
            continue
        st = item.get("status") or {}
        if st.get("status_str") == "error":
            raise AIError("ComfyUI error: " + json.dumps(st)[:1200])
        if st.get("completed") or st.get("status_str") == "success":
            for out in (item.get("outputs") or {}).values():
                for key in ("images", "gifs", "videos"):
                    for f in out.get(key) or []:
                        name = f.get("filename")
                        if not name:
                            continue
                        rr = requests.get(f"{base}/view", params={
                            "filename": name, "subfolder": f.get("subfolder") or "",
                            "type": f.get("type", "output"),
                        }, timeout=300)
                        if rr.status_code == 200:
                            p = out_dir / f"{uuid.uuid4().hex[:10]}_{name}"
                            p.write_bytes(rr.content)
                            files.append(p)
            break
    if not files:
        raise AIError("ComfyUI завершился без изображения")
    return files


STYLE_ALIASES = {
    "кубизм": "cubism", "куби": "cubism", "cubism": "cubism", "cubist": "cubism",
    "акварел": "watercolor", "watercolor": "watercolor", "watercolour": "watercolor",
    "тушь": "ink", "ink": "ink", "график": "ink",
    "импрессионизм": "impressionism", "impressionism": "impressionism",
    "аниме": "anime", "anime": "anime", "киберпанк": "cyberpunk", "cyberpunk": "cyberpunk",
    "масло": "oil painting", "oil": "oil painting", "реализм": "photorealism",
    "photorealistic": "photorealism", "пиксель": "pixel art", "pixel": "pixel art",
}
STYLE_PROMPTS = {
    "cubism": ("cubist painting, fragmented geometric planes, multiple simultaneous viewpoints, "
               "bold angular contours, interlocking facets, visible painterly brushwork"),
    "watercolor": "loose watercolor painting, translucent washes, paper texture, soft edges",
    "ink": "black ink drawing, expressive pen and brush strokes, monochrome graphic style",
    "impressionism": "impressionist painting, visible brush strokes, light and color atmosphere",
    "anime": "anime key visual, clean line art, cel shading",
    "cyberpunk": "cyberpunk concept art, neon lighting, high contrast",
    "oil painting": "oil painting, visible brush strokes, rich texture",
    "photorealism": "photorealistic rendering, natural light, fine detail",
    "pixel art": "pixel art, limited palette, crisp pixels",
}
DEFAULT_NEG = ("different composition, changed framing, different camera angle, cropped subject, "
               "distorted proportions, extra limbs, deformed anatomy, text, watermark, logo")


def _apply_style(prompt: str, style: str | None) -> str:
    text = (prompt or "").strip()
    key = str(style or "").strip().lower()
    if key:
        key = STYLE_ALIASES.get(key, key)
        if key.lower() not in text.lower():
            text = (text + ", " if text else "") + STYLE_PROMPTS.get(key, f"{key} style")
    return text


def _reference_size(path: str, max_side: int = 1920, max_pixels: int = 1_600_000) -> tuple[int, int]:
    try:
        with Image.open(path) as im:
            w, h = im.size
    except Exception:
        return 1024, 1024
    if w <= 0 or h <= 0:
        return 1024, 1024
    scale = (float(max_pixels) / float(w * h)) ** 0.5
    scale = min(scale, float(max_side) / float(max(w, h)))
    tw = max(64, int(round(w * scale / 16.0)) * 16)
    th = max(64, int(round(h * scale / 16.0)) * 16)
    return tw, th


def _compose_restyle_prompt(prompt: str, style: str | None, has_reference: bool = True) -> str:
    text = _apply_style(prompt, style)
    if has_reference:
        low = text.lower()
        if "preserve" not in low and "composition" not in low:
            text += (", preserve the exact composition, framing, camera angle, subject placement, "
                     "and original aspect ratio of the reference frame; do not crop or reframe")
    return text


def generate_image(prompt: str, negative: str = "", images: list[dict[str, Any]] | None = None,
                   progress=None, options: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    options = options or {}
    started_ts = time.time()
    started_at = datetime.now().isoformat(timespec="seconds")
    wconf = options.get("workflow_config") or {}

    def opt(key: str, default=None):
        value = options.get(key)
        return value if value is not None else wconf.get(key, default)

    if progress:
        progress({"type": "stage", "text": "Готовлю референсы..."})
    cfg = load_config()
    refs = _refs_for_generation(images or [])
    for extra in options.get("reference_paths") or []:
        extra = str(extra)
        if Path(extra).is_file() and extra not in refs:
            refs.append(extra)
    style = opt("style") or ""
    prompt = _apply_style(prompt, style)
    preserve_aspect = opt("preserve_aspect", bool(refs))
    width = opt("width")
    height = opt("height")
    if width and height:
        width, height = int(width), int(height)
    elif refs and preserve_aspect:
        width, height = _reference_size(refs[0], int(opt("max_side") or 1920),
                                        int(opt("max_pixels") or 1_600_000))
    else:
        width = height = 1024
    if refs:
        low = prompt.lower()
        if "preserve" not in low and "composition" not in low:
            prompt += (", preserve the exact composition, framing, camera angle, subject placement, "
                       "and original aspect ratio of the reference frame; do not crop or reframe")
    target_ratio = None
    if refs and preserve_aspect:
        try:
            with Image.open(refs[0]) as im_ref:
                target_ratio = im_ref.width / im_ref.height
        except Exception:
            target_ratio = None
    negative = (negative or "").strip() or DEFAULT_NEG
    seed = int(opt("seed") or 20261117)
    input_dir = Path(str(cfg["comfy"].get("input_dir") or "~/opt/ComfyUI/input")).expanduser()
    names = _stage_refs(refs, input_dir)
    workflow = str(opt("workflow") or "auto").lower()
    if workflow in ("", "auto"):
        workflow = "flux3_i2v" if names else "flux3_t2i"
    steps = int(opt("steps") or (8 if workflow == "flux3_i2v" else 24))
    cfg_value = float(opt("cfg") if opt("cfg") is not None else (1.0 if workflow == "flux3_i2v" else 2.0))
    denoise = float(opt("denoise") if opt("denoise") is not None else 1.0)
    megapixels = float(opt("megapixels") or (width * height) / 1_000_000.0)
    models = opt("models") or {}
    if progress:
        progress({"type": "stage", "text": f"Собираю workflow {workflow}..."})
    if workflow == "flux3_i2v" and names:
        wf = flux3_i2v(prompt, negative, names, width, height, seed, cfg,
                       steps=steps, cfg_value=cfg_value, denoise=denoise,
                       megapixels=megapixels, models=models)
    else:
        wf = flux3_t2i(prompt, negative, width, height, seed, cfg,
                       steps=steps, cfg_value=cfg_value, models=models)
    files = _submit(wf, cfg, IMAGES, progress=progress)
    elapsed = round(time.time() - started_ts, 1)
    hw = system_info.hardware_info()
    cm = cfg.get("comfy", {}).get("models", {})
    used_models = {
        "unet": cm.get("unet_flux3") if workflow == "flux3_i2v" or workflow == "flux3_t2i" else cm.get("unet_wan22"),
        "clip": cm.get("clip_flux3") if workflow.startswith("flux3") else cm.get("clip_wan"),
        "vae": cm.get("vae_flux3") if workflow.startswith("flux3") else cm.get("vae_wan"),
    }
    loras = []
    for node in wf.values() if isinstance(wf, dict) else []:
        if isinstance(node, dict) and "lora" in str(node.get("class_type") or "").lower():
            lora = (node.get("inputs") or {}).get("lora_name")
            if lora and lora not in loras:
                loras.append(lora)
    result = []
    for p in files:
        dst = IMAGES / f"{uuid.uuid4().hex[:12]}.png"
        out_w, out_h = width, height
        try:
            im_out = Image.open(p).convert("RGB")
            if target_ratio:
                w, h = im_out.size
                if w / h > target_ratio:
                    nw = max(1, int(round(h * target_ratio)))
                    left = max(0, (w - nw) // 2)
                    im_out = im_out.crop((left, 0, left + nw, h))
                else:
                    nh = max(1, int(round(w / target_ratio)))
                    top = max(0, (h - nh) // 2)
                    im_out = im_out.crop((0, top, w, top + nh))
            im_out.save(dst)
            out_w, out_h = im_out.size
        except Exception:
            shutil.copy2(p, dst)
        item = {"name": dst.name, "path": str(dst), "url": f"/api/chat/file/images/{dst.name}",
                "width": out_w, "height": out_h, "workflow": workflow,
                "prompt": prompt, "negative": negative, "seed": seed, "steps": steps,
                "cfg": cfg_value, "denoise": denoise, "models": models,
                "workflow_config": {"profile": workflow, "steps": steps, "cfg": cfg_value,
                                    "denoise": denoise, "megapixels": megapixels,
                                    "preserve_aspect": bool(refs and preserve_aspect),
                                    "style": style, "models": models},
                "workflow_json": wf, "elapsed_sec": elapsed,
                "started_at": started_at, "finished_at": datetime.now().isoformat(timespec="seconds"),
                "hardware": hw, "models_used": used_models, "loras": loras,
                "api_cost": 0, "generation_kind": "local_comfy", "video_seconds": None,
                "resolution": f"{out_w}x{out_h}"}
        result.append(item)
        if progress:
            progress({"type": "image", "text": "Изображение готово", "image": item})
    return result


def _detect_style(text: str) -> str:
    low = (text or "").lower()
    for key in sorted(STYLE_ALIASES, key=len, reverse=True):
        if key in low:
            return STYLE_ALIASES[key]
    return ""


def _period_from_text(text: str) -> tuple[date, date, str]:
    today = date.today()
    low = (text or "").lower()
    m = re.search(r"с\s*(\d{4}-\d{2}-\d{2})\s*(?:по|до)\s*(\d{4}-\d{2}-\d{2})", low)
    if m:
        try:
            return (date.fromisoformat(m.group(1)), date.fromisoformat(m.group(2)), "указанный период")
        except Exception:
            pass
    if "сегодня" in low:
        return today, today, "сегодня"
    if "вчера" in low:
        y = today - timedelta(days=1)
        return y, y, "вчера"
    m = re.search(r"за\s+(\d+)\s*(дн|день|дня|недел)", low)
    if m:
        n = max(1, int(m.group(1)))
        days = 7 * n if "недел" in m.group(2) else n
        return today - timedelta(days=days - 1), today, f"за {days} дн."
    if "недел" in low:
        return today - timedelta(days=6), today, "за неделю"
    if "месяц" in low or "30 дн" in low:
        return today - timedelta(days=29), today, "за месяц"
    return today - timedelta(days=6), today, "за 7 дней"


def _news_request(text: str) -> dict[str, Any] | None:
    if not NEWS_RE.search(text or ""):
        return None
    m = re.search(r"(?:в|по)\s+проект[еа]?\s+([A-Za-z0-9_.-]+)", text or "", re.I)
    project = m.group(1) if m else None
    cfg = load_config()
    if not project:
        low = (text or "").lower()
        project = next((str(a) for a in (cfg.get("synx", {}).get("projects") or [])
                        if str(a).lower() in low), None)
    if not project:
        projects = cfg.get("synx", {}).get("projects") or []
        if len(projects) == 1:
            project = projects[0]
    if not project:
        return None
    d0, d1, label = _period_from_text(text or "")
    return {"project": project, "date_from": d0.isoformat(),
            "date_to": d1.isoformat(), "label": label}


def _pdf_remix_request(text: str) -> dict[str, Any] | None:
    low = (text or "").lower()
    if not re.search(r"(pdf|пдф)", low, re.I):
        return None
    if not any(k in low for k in ("черн", "dpi", "текст", "центр", "поля", "кадр", "страниц", "верси")):
        return None
    if not any(k in low for k in ("сделай", "собери", "исправ", "передел", "опубликуй", "сделать")):
        return None
    base_version = None
    patterns = (
        r"относительно\s+(?:верси[июя]?\s*)?v?(\d{1,3})",
        r"на\s+основе\s+(?:верси[июя]?\s*)?v?(\d{1,3})",
        r"чем\s+в\s+верси[июя]?\s*v?(\d{1,3})",
        r"(\d{1,3})\s+верси",
        r"базов[а-я]*\s+(?:верси[июя]?\s*)?v?(\d{1,3})",
    )
    for pat in patterns:
        m = re.search(pat, low)
        if m:
            base_version = f"v{int(m.group(1)):03d}"
            break
    if not base_version:
        m = re.search(r"верси[юия]?\s*(?:v)?(\d{1,3})", low)
        if not m:
            m = re.search(r"\bv(\d{1,3})\b", low)
        if m:
            base_version = f"v{int(m.group(1)):03d}"
    dpi = 300
    m = re.search(r"(\d{2,4})\s*dpi", low)
    if m:
        dpi = max(72, min(1200, int(m.group(1))))
    text_scale = 1.0
    m = re.search(r"на\s*(\d{1,2})\s*%\s*(?:меньше|меньше\s+на)?", low)
    if m:
        text_scale = max(0.4, 1.0 - int(m.group(1)) / 100.0)
    elif "текст" in low and "меньше" in low:
        text_scale = 0.8
    image_valign = "center" if any(k in low for k in (
        "по-вертикали", "по вертикали", "по центру", "середине", "строго по середине")) else "top"
    return {"base_version": base_version, "dpi": dpi, "text_scale": text_scale,
            "image_valign": image_valign, "background": "#000000"}


def _remix_task() -> dict[str, Any] | None:
    tasks = db.list_tasks(limit=100)
    for t in tasks:
        wd = Path(str(t.get("work_dir") or ""))
        if wd.is_dir() and list(wd.glob("frame_*.png")):
            if str(t.get("task_name") or "") in ("frames_pdf", "cubism_frames") or str(t.get("shot") or "") == "p002_0130":
                return t
    for t in tasks:
        wd = Path(str(t.get("work_dir") or ""))
        if wd.is_dir() and list(wd.glob("frame_*.png")):
            return t
    return None


def _daily_items_from_task(task: dict[str, Any]) -> list[dict[str, Any]]:
    work = Path(str(task.get("work_dir") or ""))
    images = sorted(work.glob("frame_*.png"))
    meta = {}
    try:
        meta = json.loads((work / "generation_meta.json").read_text(encoding="utf-8"))
    except Exception:
        meta = {}
    by_file = {str(x.get("file")): x for x in (meta.get("images") or []) if isinstance(x, dict)}
    plan = task.get("plan_json") or {}
    wconf = (plan.get("workflow_config") or {}) if isinstance(plan, dict) else {}
    hw = meta.get("hardware") or system_info.hardware_info()
    out = []
    for i, p in enumerate(images, 1):
        m = by_file.get(p.name) or {}
        resolution = m.get("resolution") or ""
        if not resolution:
            try:
                from PIL import Image as _Image
                with _Image.open(p) as _im:
                    resolution = f"{_im.width}x{_im.height}"
            except Exception:
                resolution = ""
        out.append({
            "path": str(p),
            "title": m.get("title") or p.stem,
            "prompt_en": m.get("prompt_en") or (plan.get("prompt") if isinstance(plan, dict) else "") or "",
            "prompt_ru": m.get("prompt_ru") or "",
            "models_used": m.get("models") or wconf.get("models") or {},
            "loras": m.get("loras") or [],
            "elapsed_sec": m.get("elapsed_sec") if m.get("elapsed_sec") is not None else None,
            "elapsed_note": m.get("elapsed_note") or "",
            "generation_kind": "local_comfy",
            "api_cost": 0, "video_seconds": None,
            "resolution": resolution, "hardware": hw,
        })
    return out


def _publish_request(text: str) -> bool:
    low = (text or "").lower()
    if not re.search(r"(пдф|pdf)", low, re.I):
        return False
    if not any(k in low for k in ("опублик", "верси", "задач", "render")):
        return False
    if not any(k in low for k in ("сделай", "создай", "собери", "опубликуй")):
        return False
    return any(k in low for k in ("кадр", "картин", "изображ", "эти", "этого", "пять", "5"))


def _guess_task_type(text: str) -> str:
    m = re.search(r"тип[а]?\s+([A-Za-z][A-Za-z0-9_]+)", text or "", re.I)
    if m:
        return m.group(1)
    return "Art"


def _guess_task_name(text: str) -> str:
    low = (text or "").lower()
    if "кубизм" in low or "cubis" in low:
        return "cubism_frames"
    if "пдф" in low or "pdf" in low:
        return "frames_pdf"
    return "cubism_frames"


def _guess_bot(text: str) -> str | None:
    low = (text or "").lower()
    try:
        from config import load_bots
        for b in load_bots():
            login = str(b.get("login") or "")
            if login and login.lower() in low:
                return login
    except Exception:
        pass
    return None


def _version_request(text: str) -> dict[str, Any] | None:
    """Recognise an explicit request: project + shot + version + generation intent."""
    if not GEN_RE.search(text or ""):
        return None
    m_shot = re.search(r"\b([A-Za-z][A-Za-z0-9_]*_\d{3,5}(?:_v?\d{1,4})?)\b", text or "", re.I)
    if not m_shot:
        return None
    shot = m_shot.group(1)
    tail = (text or "")[m_shot.end():m_shot.end() + 80]
    m_ver = re.search(r"\b(v\d{1,4})\b", tail, re.I)
    version = m_ver.group(1) if m_ver else None
    # p002_0130_v004 / p002_0130_004 -> split the trailing version
    m_tail = re.match(r"^(.+?)_v?(\d{1,4})$", shot, re.I)
    if m_tail and re.search(r"_\d{3,5}$", m_tail.group(1)):
        shot = m_tail.group(1)
        version = version or ("v" + m_tail.group(2).zfill(3))
    m_proj = re.search(r"(?:проект[ае]?|project)\s+([A-Za-z0-9_.-]+)", text or "", re.I)
    project = m_proj.group(1).strip(" ,.;") if m_proj else None
    if not project:
        projects = load_config().get("synx", {}).get("projects") or []
        if projects:
            project = projects[0]
    if not project:
        return None
    low = (text or "").lower()
    frame = "middle"
    if "первый кадр" in low or "first frame" in low:
        frame = "first"
    elif "последний кадр" in low or "last frame" in low:
        frame = "last"
    return {
        "project": project,
        "shot": shot,
        "version": version,
        "style": _detect_style(text),
        "frame": frame,
        "preserve_aspect": any(k in low for k in (
            "пропорц", "композиц", "кадр", "aspect", "composition", "frame", "reframe")),
    }


def _prompt_for_version(text: str, req: dict[str, Any], ref: dict[str, Any],
                        ai: AIClient, progress=None) -> tuple[str, dict[str, int]]:
    style = req.get("style") or ""
    rules = ("Preserve the exact composition, framing, camera angle, subject placement and the "
             "original aspect ratio of the reference frame. Do not crop or reframe. Keep the "
             "underlying scene structure readable while applying the requested style.")
    fallback = _compose_restyle_prompt(
        "Restyle the provided reference frame" + (f" in {style} style" if style else ""),
        style, True)
    if not ai.ready:
        return fallback, {"input": 0, "output": 0}
    if progress:
        progress({"type": "stage", "text": "Готовлю промпт по референсу..."})
    instruction = (
        "You write ONE English prompt for FLUX.3 image-to-image editing. Output only the prompt, "
        "without quotes, JSON or explanations. " + rules + "\n"
        f"User request (Russian): {text}\n"
        f"Reference: project {req.get('project')}, shot {req.get('shot')}, "
        f"version {req.get('version') or 'latest'}, {ref.get('width')}x{ref.get('height')}.\n"
        + (f"Style: {style}.\n" if style else "")
    )
    content: list[dict[str, Any]] = [{"type": "text", "text": instruction}]
    try:
        content.append({"type": "image_url", "image_url": {"url": _data_url(Path(ref["path"]))}})
    except Exception:
        pass
    try:
        out, usage = ai.chat([
            {"role": "system", "content": "You are a precise prompt engineer. Output only the prompt."},
            {"role": "user", "content": content},
        ])
        prompt = (out or "").strip().strip("`").strip().strip('"').strip()
        if not prompt:
            return fallback, usage
        if len(prompt) > 2200:
            prompt = prompt[:2200]
        low = prompt.lower()
        if style and style.lower() not in low:
            prompt += f", {style} style"
        if "preserve" not in low and "composition" not in low:
            prompt += ", preserve the exact composition, framing and aspect ratio"
        return prompt, usage
    except Exception:
        return fallback, {"input": 0, "output": 0}


def _generate_from_version(project: str, shot: str, version: str | None, prompt: str,
                           style: str | None, negative: str, frame: str,
                           options: dict[str, Any], images: list[dict[str, Any]] | None = None,
                           progress=None, ref: dict[str, Any] | None = None
                           ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if ref is None:
        if progress:
            progress({"type": "stage", "text": "Ищу версию в synx..."})
        ref = synx_tools.prepare_reference(project, shot, version, frame or "middle")
    info = ref.get("version") or {}
    if progress:
        progress({"type": "stage",
                  "text": f"Референс готов: {ref.get('width')}x{ref.get('height')} ({info.get('version')})"})
    prompt = _compose_restyle_prompt(prompt, style, True)
    opts = dict(options or {})
    opts.setdefault("style", style or "")
    opts.setdefault("preserve_aspect", True)
    opts.setdefault("workflow", "flux3_i2v")
    opts.setdefault("reference_paths", [ref["path"]])
    models = {k: opts.get(k) for k in ("unet", "clip", "vae") if opts.get(k)}
    if models:
        opts["models"] = models
    opts.setdefault("workflow_config", {
        "profile": opts.get("workflow") or "flux3_i2v",
        "steps": opts.get("steps") or 8,
        "cfg": opts.get("cfg") if opts.get("cfg") is not None else 1.0,
        "denoise": opts.get("denoise") if opts.get("denoise") is not None else 1.0,
        "megapixels": round((int(ref.get("width") or 1) * int(ref.get("height") or 1)) / 1_000_000.0, 4),
        "preserve_aspect": True,
        "style": style or "",
    })
    refs = [{"path": ref["path"]}]
    refs.extend(images or [])
    generated = generate_image(prompt, negative or DEFAULT_NEG, refs, progress=progress, options=opts)
    result = {
        "ok": True,
        "project": info.get("project"),
        "requested_project": info.get("requested_project"),
        "project_corrected": bool(info.get("project_corrected")),
        "shot": info.get("entity"),
        "version": info.get("version"),
        "media_kind": info.get("media_kind"),
        "width": ref.get("width"),
        "height": ref.get("height"),
        "aspect_ratio": ref.get("aspect_ratio"),
        "frame": ref.get("frame"),
        "reference_path": ref.get("path"),
        "style": style or "",
        "prompt": prompt,
        "workflow_config": opts.get("workflow_config"),
        "images": generated,
    }
    return result, generated


def _image_content(images: list[dict[str, Any]]) -> list[dict[str, Any]]:
    parts = []
    for im in images or []:
        p = im.get("path") if isinstance(im, dict) else None
        if p and Path(p).is_file():
            try:
                parts.append({"type": "image_url", "image_url": {"url": _data_url(Path(p))}})
            except Exception:
                pass
    return parts


def _history_messages(session_id: str) -> list[dict[str, Any]]:
    rows = db.list_chat_messages(session_id, limit=40)
    out = [{"role": "system", "content": _system()}]
    for r in rows:
        if r.get("role") == "user" and r.get("images"):
            content: list[dict[str, Any]] = [{"type": "text", "text": r.get("content") or ""}]
            content.extend(_image_content(r["images"]))
            out.append({"role": "user", "content": content})
        else:
            out.append({"role": r.get("role") or "assistant", "content": r.get("content") or ""})
    return out


def _execute_tool(name: str, args: dict[str, Any], images: list[dict[str, Any]],
                  progress=None, session_id: str | None = None
                  ) -> tuple[Any, list[dict[str, Any]], str | None]:
    """Returns (tool_result, generated_images, playlist_text)."""
    if name == "generate_image":
        prompt = (args.get("prompt") or "").strip() or "A detailed cinematic image"
        neg = args.get("negative") or ""
        generated = generate_image(prompt, neg, images, progress=progress, options=args)
        return {"ok": True, "images": generated}, generated, None
    if name == "generate_from_synx_version":
        result, generated = _generate_from_version(
            args.get("project") or "", args.get("shot") or "", args.get("version"),
            args.get("prompt") or "", args.get("style") or "", args.get("negative") or "",
            args.get("frame") or "middle", args, images=images, progress=progress)
        return result, generated, None
    if name == "synx_projects":
        return synx_tools.projects(), [], None
    if name == "synx_bots":
        return publish_flow.list_bots(args.get("project"), args.get("task_id")), [], None
    if name == "synx_project_news":
        return synx_tools.project_news(
            args.get("project") or "", args.get("date_from"), args.get("date_to"),
            session_id=session_id or "default",
            mark_seen=bool(args.get("mark_seen", True))), [], None
    if name == "make_pdf_from_images":
        if args.get("image_source") == "paths":
            imgs = [{"path": p} for p in (args.get("image_paths") or [])]
        else:
            imgs = publish_flow.last_generated_images(session_id or "default")
        imgs = publish_flow._valid_images(imgs)
        if not imgs:
            return {"error": "no images for PDF"}, [], None
        out_dir = ROOT / "data" / "chat" / "pdf"
        out_dir.mkdir(parents=True, exist_ok=True)
        name = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(args.get("pdf_name") or "frames")).strip("_") or "frames"
        pdf = publish_flow.make_pdf([i["path"] for i in imgs], out_dir / (name + ".pdf"),
                                    page_size=args.get("page_size") or "A4",
                                    margin_mm=float(args.get("margin_mm") or 10),
                                    dpi=int(args.get("dpi") or 144))
        info = publish_flow.verify_pdf(pdf, expected_pages=len(imgs))
        return {"ok": bool(info.get("ok")), "pdf": str(pdf), "pages": len(imgs),
                "pdf_info": info, "images": [i["path"] for i in imgs]}, [], None
    if name == "verify_pdf":
        pdf_path = args.get("pdf_path")
        if not pdf_path and args.get("task_id") is not None:
            task = db.get_task(int(args.get("task_id")))
            pdf_path = (task or {}).get("result_file")
        if not pdf_path:
            return {"error": "pdf_path or task_id with result_file is required"}, [], None
        return publish_flow.verify_pdf(pdf_path, args.get("expected_pages")), [], None
    if name == "repair_pdf_task":
        result = publish_flow.repair_pdf_task(
            int(args.get("task_id")), page_size=args.get("page_size") or "A4",
            margin_mm=float(args.get("margin_mm") or 10),
            dpi=int(args.get("dpi") or 144),
            publish=bool(args.get("publish", True)),
            bot_login=args.get("bot"), progress=progress)
        return result, [], None
    if name == "publish_daily_pdf":
        task_id = int(args.get("task_id") or 0)
        if not task_id:
            rows = db.list_tasks(limit=50)
            for t in rows:
                wd = Path(str(t.get("work_dir") or ""))
                if wd.is_dir() and list(wd.glob("frame_*.png")):
                    task_id = int(t["id"]); break
        task = db.get_task(task_id)
        if not task:
            return {"error": f"task not found: {task_id}"}, [], None
        work = Path(str(task.get("work_dir") or ""))
        images = sorted(work.glob("frame_*.png"))
        if not images:
            images = sorted(Path(str(task.get("render_dir") or "")).glob("frame_*.png"))
        if not images:
            return {"error": "no frame_*.png in task work folder"}, [], None
        prompts = args.get("prompts") or []
        prompts_ru = args.get("prompts_ru") or []
        titles = args.get("titles") or []
        plan = task.get("plan_json") or {}
        wconf = (plan.get("workflow_config") or {}) if isinstance(plan, dict) else {}
        models = args.get("models") or wconf.get("models") or {}
        loras = args.get("loras") or []
        hw = system_info.hardware_info()
        items = []
        for i, p in enumerate(images):
            items.append({
                "path": str(p),
                "title": titles[i] if i < len(titles) else p.stem,
                "prompt_en": prompts[i] if i < len(prompts) else (plan.get("prompt") or ""),
                "prompt_ru": prompts_ru[i] if i < len(prompts_ru) else "",
                "models_used": models, "loras": loras,
                "elapsed_sec": args.get("elapsed_sec"),
                "elapsed_note": args.get("elapsed_note") or "",
                "generation_kind": args.get("generation_kind") or "local_comfy",
                "api_cost": args.get("api_cost", 0),
                "video_seconds": args.get("video_seconds"),
                "resolution": args.get("resolution") or "",
                "hardware": hw,
            })
        result = publish_flow.publish_daily_pdf(
            task_id, items, title=args.get("title") or "DAILY",
            page_size=args.get("page_size") or "A4",
            dpi=int(args.get("dpi") or 144),
            text_scale=float(args.get("text_scale") if args.get("text_scale") is not None else 1.0),
            image_valign=args.get("image_valign") or "top",
            background=args.get("background") or "#000000",
            base_version=args.get("base_version"),
            publish=bool(args.get("publish", True)), bot_login=args.get("bot"),
            progress=progress)
        return result, [], None
    if name == "publish_images_as_version":
        explicit = None
        if args.get("image_source") == "paths":
            explicit = [{"path": p} for p in (args.get("image_paths") or [])]
        result = publish_flow.publish_images_as_version(
            project=args.get("project"), shot=args.get("shot"), version=args.get("version"),
            images=explicit, session_id=session_id or "default",
            task_name=args.get("task_name") or "cubism_frames",
            task_type=args.get("task_type") or "Art",
            bot_login=args.get("bot"), comment=args.get("comment") or "",
            version_comment=args.get("version_comment") or "",
            pdf_name=args.get("pdf_name") or "",
            page_size=args.get("page_size") or "A4",
            margin_mm=float(args.get("margin_mm") or 10),
            dpi=int(args.get("dpi") or 144),
            dry_run=bool(args.get("dry_run")), progress=progress)
        return result, [], None
    if name == "synx_find_version":
        try:
            return synx_tools.find_version(args.get("project") or "", args.get("shot") or "",
                                           args.get("version")), [], None
        except Exception as exc:
            return {"error": str(exc)}, [], None
    if name == "synx_versions":
        return synx_tools.versions(args.get("project") or "", args.get("date_from"),
                                   args.get("date_to"), int(args.get("limit") or 50),
                                   bool(args.get("playable_only")),
                                   shot=args.get("shot"), version=args.get("version")), [], None
    if name == "synx_tasks":
        return synx_tools.tasks(args.get("project") or "", args.get("status"),
                                int(args.get("limit") or 300)), [], None
    if name == "synx_make_playlist":
        d = synx_tools.make_playlist(args.get("project") or "", args.get("date_from"),
                                     args.get("date_to"), int(args.get("limit") or 500),
                                     bool(args.get("playable_only", True)))
        return {"ok": True, "count": d["count"], "from": d["from"], "to": d["to"],
                "items": d["items"]}, [], d["text"]
    if name == "fs_list":
        return fs_tools.list_dir(args.get("path") or ""), [], None
    if name == "fs_read":
        r = fs_tools.read_file(args.get("path") or "", int(args.get("max_bytes") or 300000))
        gen = []
        if r.get("kind") == "binary" and str(r.get("mime", "")).startswith("image/"):
            from urllib.parse import quote
            gen = [{"name": Path(r["path"]).name, "path": r["path"],
                    "url": "/api/fs/file?path=" + quote(r["path"])}]
        return r, gen, None
    if name == "fs_write":
        return fs_tools.write_file(args.get("path") or "", args.get("content") or "",
                                   bool(args.get("base64_data"))), [], None
    if name == "fs_delete":
        return fs_tools.delete_path(args.get("path") or ""), [], None
    if name == "ffmpeg":
        binary = str(args.get("binary") or "ffmpeg").lower()
        if binary not in ("ffmpeg", "ffprobe"):
            return {"error": "binary must be ffmpeg or ffprobe"}, [], None
        argv = [binary] + [str(x) for x in (args.get("args") or [])]
        try:
            cp = subprocess.run(argv, capture_output=True, text=True,
                                timeout=int(args.get("timeout_sec") or 600))
            return {"ok": cp.returncode == 0, "returncode": cp.returncode,
                    "stdout": (cp.stdout or "")[-12000:],
                    "stderr": (cp.stderr or "")[-12000:]}, [], None
        except Exception as exc:
            return {"error": str(exc)}, [], None
    return {"error": "unknown tool " + name}, [], None


def chat_turn(session_id: str, text: str, images: list[dict[str, Any]] | None = None,
              progress=None) -> dict[str, Any]:
    images = images or []
    stages: list[dict[str, Any]] = []

    def emit(evt: dict[str, Any]):
        evt = dict(evt)
        evt["seq"] = len(stages) + 1
        stages.append(evt)
        if progress:
            progress(evt)

    emit({"type": "stage", "text": "Анализирую запрос..."})
    db.add_chat_message(session_id, "user", text, images=images)
    history = _history_messages(session_id)
    cfg = load_config()
    ai = AIClient(cfg)
    usage = {"input": 0, "output": 0}
    generated: list[dict[str, Any]] = []
    playlist_text: str | None = None
    content = ""
    tool_used = False
    ai_error = False

    # PDF layout remix: "based on v013, make vN with black pages, 300 dpi,
    # 20% smaller text, image centered". This must run before the generic
    # PDF-publish path, otherwise the robot falls back to a white A4 PDF.
    remix = _pdf_remix_request(text or "")
    if remix:
        try:
            task = _remix_task()
            if not task:
                raise ValueError("не нашёл задачу с кадрами frame_*.png")
            items = _daily_items_from_task(task)
            if not items:
                raise ValueError("в задаче нет кадров")
            emit({"type": "stage", "text": "Беру базовую версию и кадры задачи..."})
            result = publish_flow.publish_daily_pdf(
                int(task["id"]), items, title="DAILY PDF",
                page_size="A4", dpi=remix["dpi"],
                text_scale=remix["text_scale"],
                image_valign=remix["image_valign"],
                background=remix["background"],
                base_version=remix.get("base_version"),
                publish=True, progress=emit)
            corrected = (f" Базовая версия: {remix.get('base_version')}." if remix.get("base_version") else "")
            content = (
                f"Готово. Собрал PDF: {result.get('pages')} страниц, "
                f"{result.get('page_size')}, {result.get('dpi')} dpi.{corrected}\n\n"
                f"- Фон: {result.get('background')}; текст: {int(round(float(result.get('text_scale') or 1) * 100))}% от базы; "
                f"кадр: {result.get('image_valign')} по вертикали.\n"
                f"- PDF: `{result.get('pdf')}`\n"
                f"- Рабочая папка: `{result.get('work_dir')}`\n"
                f"- Render: `{result.get('render_dir')}`\n"
                f"- Версия: **{result.get('version_code')}**\n"
                f"- Ссылка: {result.get('url') or '—'}"
                + (f"\n- Внимание: {result.get('warning')}" if result.get("warning") else "")
            )
        except Exception as exc:
            content = f"Не удалось пересобрать PDF по базовой версии: {exc}"
        emit({"type": "stage", "text": "Готово"})
        db.add_chat_message(session_id, "assistant", content, generated=[],
                            usage=usage, playlist_text=None, progress=stages)
        return {"messages": db.list_chat_messages(session_id), "usage": usage,
                "generated": [], "playlist_text": None, "stages": stages}

    # Explicit "make a PDF from these frames and publish a version" requests.
    if _publish_request(text or ""):
        try:
            emit({"type": "stage", "text": "Собираю PDF и готовлю публикацию..."})
            result = publish_flow.publish_images_as_version(
                project=None, shot=None, version=None, images=None,
                session_id=session_id,
                task_name=_guess_task_name(text or ""),
                task_type=_guess_task_type(text or ""),
                bot_login=_guess_bot(text or ""),
                comment=(text or "")[:700],
                version_comment="PDF: кадры по одному на страницу",
                pdf_name=_guess_task_name(text or ""),
                progress=emit)
            if result.get("dry_run"):
                content = (f"PDF собран: {result.get('pdf')} "
                           f"({result.get('source_version')} → v{result.get('target_version')}).")
            else:
                corrected = (f" В запросе был проект {result.get('requested_project')}, "
                             f"шот нашёлся в {result.get('project')}." if result.get("project_corrected") else "")
                content = (
                    f"Готово. Собрал PDF: {len(result.get('images') or [])} страниц, "
                    f"размер страницы {result.get('page_size')}.{corrected}\n\n"
                    f"- Задача: **{result.get('task_type')}** «{result.get('task_name')}» "
                    f"(synx `{result.get('synx_task_id')}`)\n"
                    f"- Исполнитель: **{result.get('bot')}** ({result.get('bot_skills') or 'бот'})\n"
                    f"- Рабочая папка: `{result.get('work_dir')}`\n"
                    f"- Render: `{result.get('render_dir')}`\n"
                    f"- PDF: `{result.get('pdf')}`\n"
                    f"- Опубликована версия: **{result.get('version_code')}**\n"
                    f"- Ссылка: {result.get('url') or '—'}"
                )
        except Exception as exc:
            content = f"Не удалось собрать PDF и опубликовать версию: {exc}"
        emit({"type": "stage", "text": "Готово"})
        db.add_chat_message(session_id, "assistant", content, generated=[],
                            usage=usage, playlist_text=None, progress=stages)
        return {"messages": db.list_chat_messages(session_id), "usage": usage,
                "generated": [], "playlist_text": None, "stages": stages}

    # Questions like "what is new in project X" are answered from the local
    # scan table (with a per-chat seen checkpoint), not by a blind tool loop.
    news_req = _news_request(text or "")
    if news_req:
        try:
            emit({"type": "stage", "text": "Смотрю таблицу сообщений synx..."})
            news = synx_tools.project_news(
                news_req["project"], news_req["date_from"], news_req["date_to"],
                session_id=session_id, mark_seen=True)
            emit({"type": "stage", "text": "Сравниваю с прошлым просмотром и готовлю резюме..."})
            content = news.get("digest") or "В таблице нет сообщений за этот период."
            if ai.ready:
                summary, u = ai.chat([
                    {"role": "system", "content":
                     "Ты — продюсер студии Synx. Отвечай по-русски, кратко и структурно. "
                     "Используй только данные из таблицы ниже, не выдумывай факты."},
                    {"role": "user", "content":
                     f"Запрос пользователя: {text}\n\n"
                     f"Данные локальной таблицы сообщений synx:\n{news.get('digest') or ''}\n\n"
                     "Сделай резюме: 1) что нового за период; 2) ближайший фронт работ по шотам; "
                     "3) что требует действий, отдельно упоминания @p.robot; 4) новые версии. "
                     "Если новых с прошлого просмотра 0 — скажи это. Не перечисляй всё подряд."},
                ])
                usage["input"] += int(u.get("input") or 0)
                usage["output"] += int(u.get("output") or 0)
                if (summary or "").strip():
                    content = summary.strip()
        except Exception as exc:
            content = f"Не удалось посмотреть таблицу сообщений: {exc}"
        emit({"type": "stage", "text": "Готово"})
        db.add_chat_message(session_id, "assistant", content, generated=[],
                            usage=usage, playlist_text=None, progress=stages)
        return {"messages": db.list_chat_messages(session_id), "usage": usage,
                "generated": [], "playlist_text": None, "stages": stages}

    # Explicit "find version X and generate from it" requests are handled
    # deterministically: lookup + reference frame + FLUX.3 restyle in one pass.
    version_req = _version_request(text or "")
    if version_req and not PLAYLIST_RE.search(text or ""):
        try:
            emit({"type": "stage", "text": "Ищу версию в synx..."})
            ref = synx_tools.prepare_reference(
                version_req["project"], version_req["shot"],
                version_req.get("version"), version_req.get("frame") or "middle")
            prompt, pun = _prompt_for_version(text or "", version_req, ref, ai, progress=emit)
            usage["input"] += int(pun.get("input") or 0)
            usage["output"] += int(pun.get("output") or 0)
            result, generated = _generate_from_version(
                version_req["project"], version_req["shot"], version_req.get("version"),
                prompt, version_req.get("style"), "", version_req.get("frame") or "middle",
                {"workflow": "flux3_i2v", "steps": 8, "cfg": 1.0, "denoise": 1.0,
                 "preserve_aspect": True, "style": version_req.get("style") or ""},
                images=images, progress=emit, ref=ref)
            info = result
            requested = info.get("requested_project")
            corrected = (f" В запросе был проект {requested}, версия нашлась в "
                         f"{info.get('project')}." if info.get("project_corrected") else "")
            kind = "видео, средний кадр" if info.get("media_kind") == "video" else "изображение"
            wf = info.get("workflow_config") or {}
            content = (
                f"Готово. Нашёл {info.get('project')}/{info.get('shot')} {info.get('version')} "
                f"({kind}, {info.get('width')}x{info.get('height')}).{corrected} "
                f"Сгенерировал изображение" + (f" в стиле {info.get('style')}" if info.get("style") else "") +
                " с сохранением композиции и пропорций исходника. "
                f"Workflow: {wf.get('profile')}, steps={wf.get('steps')}, "
                f"cfg={wf.get('cfg')}, denoise={wf.get('denoise')}."
            )
        except Exception as exc:
            content = f"Не удалось найти версию или сгенерировать изображение: {exc}"
        emit({"type": "stage", "text": "Готово"})
        db.add_chat_message(session_id, "assistant", content, generated=generated,
                            usage=usage, playlist_text=None, progress=stages)
        return {"messages": db.list_chat_messages(session_id), "usage": usage,
                "generated": generated, "playlist_text": None, "stages": stages}

    seen_calls: set[str] = set()
    try:
        for _ in range(8):
            emit({"type": "stage", "text": "Думаю..."})
            msg, u = ai.chat_message(history, tools=TOOLS)
            usage["input"] += u.get("input", 0)
            usage["output"] += u.get("output", 0)
            calls = msg.get("tool_calls") or []
            if not calls:
                content = (msg.get("content") or "").strip()
                break
            tool_used = True
            emit({"type": "stage", "text": "Вызываю инструмент..."})
            history.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
            for call in calls:
                fn = (call.get("function") or {})
                name = fn.get("name") or ""
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except Exception:
                    args = {}
                signature = name + json.dumps(args, ensure_ascii=False, sort_keys=True)
                if signature in seen_calls:
                    result = {"error": "duplicate_tool_call",
                              "message": "Этот вызов уже выполнен. Используй предыдущий результат; "
                                         "не повторяй его. Если данных достаточно — переходи к "
                                         "generate_image / generate_from_synx_version или ответу."}
                else:
                    seen_calls.add(signature)
                    try:
                        emit({"type": "stage", "text": f"Инструмент: {name}"})
                        result, gen, pl = _execute_tool(name, args, images, progress=emit,
                                                        session_id=session_id)
                        generated.extend(gen)
                        if pl:
                            playlist_text = pl
                    except Exception as exc:
                        result = {"error": str(exc)}
                history.append({"role": "tool", "tool_call_id": call.get("id") or name,
                                "content": json.dumps(result, ensure_ascii=False)[:12000]})
        else:
            content = ("Слишком много шагов. Уточни проект, шот и версию. "
                       "Для генерации из версии используй один вызов generate_from_synx_version.")
    except AIError as exc:
        ai_error = True
        content = f"AI недоступен: {exc}"
    if not content and playlist_text:
        content = "Готово. Плейлист сформирован."
    if not content and generated:
        content = "Готово. Сгенерировал изображение."
    if (ai_error or not tool_used) and PLAYLIST_RE.search(text or ""):
        try:
            d = synx_tools.make_playlist(load_config()["synx"]["projects"][0])
            playlist_text = d["text"]
            content = f"Готово. Плейлист из {d['count']} версий."
        except Exception as exc:
            if ai_error and not content:
                content = f"Не удалось собрать плейлист: {exc}"
    elif (ai_error or not tool_used) and GEN_RE.search(text or ""):
        try:
            generated = generate_image(text.strip(), "", images)
            content = "Готово. Сгенерировал изображение по твоему запросу."
        except Exception as exc:
            if ai_error and not content:
                content = f"Не удалось сгенерировать изображение: {exc}"
    if not content and not tool_used:
        if PLAYLIST_RE.search(text or ""):
            try:
                d = synx_tools.make_playlist(load_config()["synx"]["projects"][0])
                playlist_text = d["text"]
                content = f"Готово. Плейлист из {d['count']} версий."
            except Exception as exc:
                content = f"Не удалось собрать плейлист: {exc}"
        elif GEN_RE.search(text or ""):
            try:
                generated = generate_image(text.strip(), "", images)
                content = "Готово. Сгенерировал изображение по твоему запросу."
            except Exception as exc:
                content = f"Не удалось сгенерировать изображение: {exc}"
        elif not content:
            content = "Не смог получить ответ от модели. Проверь настройки AI."
    emit({"type": "stage", "text": "Готово"})
    db.add_chat_message(session_id, "assistant", content, generated=generated,
                        usage=usage, playlist_text=playlist_text, progress=stages)
    return {"messages": db.list_chat_messages(session_id), "usage": usage,
            "generated": generated, "playlist_text": playlist_text, "stages": stages}


# --- background chat jobs for live progress ---------------------------------
import threading

_JOBS: dict[str, dict[str, Any]] = {}
_JOBS_LOCK = threading.Lock()


def start_chat_job(session_id: str, text: str, images: list[dict[str, Any]]) -> str:
    job_id = uuid.uuid4().hex[:16]
    job = {"id": job_id, "session_id": session_id, "status": "running",
           "events": [], "result": None, "error": None}
    with _JOBS_LOCK:
        _JOBS[job_id] = job

    def progress(evt: dict[str, Any]):
        with _JOBS_LOCK:
            job["events"].append(evt)

    def run():
        try:
            result = chat_turn(session_id, text, images, progress=progress)
            with _JOBS_LOCK:
                job["result"] = result
                job["status"] = "done"
        except Exception as exc:
            with _JOBS_LOCK:
                job["error"] = str(exc)
                job["status"] = "error"

    threading.Thread(target=run, daemon=True, name="chat-job").start()
    return job_id


def get_chat_job(job_id: str) -> dict[str, Any] | None:
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if not job:
            return None
        return {"id": job["id"], "status": job["status"], "events": list(job["events"]),
                "error": job["error"],
                "result": job["result"]}
