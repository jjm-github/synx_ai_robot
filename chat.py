from __future__ import annotations

import html as html_lib
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

import comfy_server
import db
import doc_flow
import fs_tools
import mcp_manager
import mcp_runtime
import obsidian_export
import passport_batch
import pdf_cache
import pipeline_queue
import publish_flow
import synx_tools
import script_tools
import shot_script
import system_info
import text_pdf
from ai import AIClient, AIError, _data_url
from config import DATA_DIR, ROOT, load_config
from workflows import flux3_i2v, flux3_t2i

requests.packages.urllib3.disable_warnings()

CHAT_DIR = DATA_DIR / "chat"
UPLOADS = CHAT_DIR / "uploads"
IMAGES = CHAT_DIR / "images"
for _p in (UPLOADS, IMAGES):
    _p.mkdir(parents=True, exist_ok=True)

CHAT_SYSTEM = """Ты — ассистент IIRobot. Отвечай по-русски, кратко и по делу.

Ты умеешь:
- отвечать на вопросы о производстве, synx, версиях, задачах, проектах, ComfyUI и промптах;
- анализировать прикреплённые пользователем изображения;
- работать с прикреплёнными файлами-референсами любого типа: читать их через
  fs_read по путям из сообщения (PDF, .md, .txt, .json, таблицы, архивы и т. п.);
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

Для просмотра/проверки PDF используй pdf_render_pages: он параллельно рендерит
все страницы в кэш PNG, а UI показывает уже готовые страницы. Для отдельной
склейки картинок в PDF используй make_pdf_from_images (одна
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

## Фокус и публикация

- Работай только с тем шотом/версией, который назван в текущем сообщении или
  в блоке «Текущий фокус». Не переноси шоты и версии из предыдущих сообщений.
- Упоминание версии (`p002_0410_art_v002`, «версия 13») — это референс для
  работы, а НЕ разрешение публиковать.
- Публикуй версию по явной просьбе: «опубликуй», «сделай версию», «запушь»,
  «выложи». Разрешение действует на всю сессию: после него «продолжай»
  означает «доделай и опубликуй», повторное подтверждение не нужно.
- Цель публикации задаётся текстом в чате, переключать фокус в интерфейсе
  НЕ нужно: называй её в аргументах инструмента явно (project, shot, version).

## Массовые публикации и дэйли (опыт из практики)

- Несколько документов = несколько версий = несколько вызовов
  `publish_images_as_version` подряд, по одному на документ, с явными
  project/shot в аргументах. Делай все публикации за один заход.
- Не переспрашивай подтверждение, если пользователь уже назвал, что и куда
  публиковать. Не проси «сменить фокус в приложении» и не рассказывай про
  focus_mismatch — цель задаётся текстом: «в test_proekt_vfx / main_script»,
  «фокус: <проект>/<сущность>», «шот p002_0130».
- «Продолжай» = продолжай прежний план до конца, включая публикацию.
- Бота выбирай сам по скилам (AI/Pipeline — генерация, текст/сценарий — любой
  AI). Уточняй исполнителя только при явной неоднозначности.
- Имена сущностей — латинский slug, совпадающий с папкой: `loc_...`, `obj_...`,
  `p002_0130`. `display_name` — человекочитаемое (латиница/английский), `info` —
  полное ТЗ на русском: что это, геометрия, свет, материалы, цвет, что в кадре,
  зачем. Пустое ТЗ недопустимо.
- Имя задачи — slug, совпадающий с папкой/сущностью (`loc_ocean_seiner`), а не
  тип (`art`) и не кириллица. Для каждой задачи в первом комментарии — ТЗ:
  задача, исходные данные, формат сдачи, стиль, что сдать, критерии приёмки.
  Заглушки вида «Art task for ...» недопустимы.
- Если ТЗ нет — заполни его через `synx_update_entity` (info) и `synx_task_comment`.
- СВЯЗИ: `EntityCreate` игнорирует `linked_asset_ids`/`task_types`. После создания
  ассетов связывай их через `synx_link_assets` (PATCH, в обе стороны): каждая
  локация → все её объекты и персонажи, каждый объект/персонаж → все его
  локации. Объект, который встречается в нескольких локациях, линкуй ко всем.
  Проверяй, что `linked_assets` не пуст.
- Удаление сущностей — только по явной просьбе («удали ассет/шот X», «удали все
  ассеты, что создал робот»). Сначала найди и покажи список (id, имя, тип,
  версии/задачи), затем вызывай `synx_delete_entity`. Непустые сущности
  (с версиями/задачами) удаляй только с `force=true` после явного подтверждения.
- ЛЮБАЯ производственная работа из чата (генерация, серия кадров, задачи на
  локации/объекты) идёт через конвейер: `queue_generation_tasks` создаёт
  видимые задачи, запускает очередь и возвращает ID. Не генерируй серии инлайн.
- После постановки обязательно сообщай: сколько задач создано, их ID, проект,
  сущности и где смотреть (раздел «Задачи», статус конвейера). Без вопросов.
- «Просчитай все локации/шоты через конвейер» = `queue_generation_tasks`
  с `entities=["all_locations"]` и запуском, не спрашивая подтверждения.
- Карта ассетов в Obsidian: `export_obsidian_map` (project, section). Каждый
  ассет — отдельная статья, связи — wikilinks, плюс map-нота и `.canvas`.
  Секцию бери из запроса (например TEST_PROJ), не спрашивай.
- Структура шотов из сценария — `create_shots_from_script` (или сообщение
  «создай структуру шотов по сценарию»). Имена — канонические ID из строк
  `s001_0010 · 200 кадров`; display_name РАВЕН имени шота (чтобы в списке были
  имена s001_0010…), тезис и диалоги — в `info`; `cut_out = cut_in + кадры - 1`;
  задачи и версии НЕ создаются; шоты линкуются к локации и к участвующим
  персонажам/объектам из сцены. Не придумывай имена и лишние шоты.
- «По 9 видов + 1 превью на локацию» / «паспорт 10 страниц» = запусти
  `start_passport_batch` (project, locations=all, views=9). Он в фоне генерирует
  9 ракурсов, собирает контактный лист первой страницей и публикует 10-страничный
  PDF новой версией. Не делай это вручную по одному кадру и не спрашивай. Назначение ботов на
  существующие задачи — `synx_assign_task` (не пересоздавай задачу).
- Версия для АССЕТА (локация/объект/персонаж) публикуется через
  `publish_pdf_as_version` (любой PDF, растровый или текстовый). Не ищи шот и
  не говори, что для ассетов публикация невозможна: `publish_images_as_version`
  только для шотов, `publish_pdf_as_version` — для всех.
- «Пересобери PDF версии vNNN» = содержимое ИМЕННО этой версии, не склейка
  других документов. Сначала прочитай версию (PDF → pdftotext, видео → кадры),
  затем собери текстовый PDF с тем же содержимым и опубликуй новой версией.
- Статус задачи меняется инструментом `synx_set_task_status` — не говори, что
  не можешь закрыть задачу; публикация текстового документа и так ставит done.
- Один и тот же файл не прикрепляй дважды: публикация задачи дедуплицируется
  автоматически, но не вызывай `synx_attach_files` перед `publish_text_document`
  с тем же файлом.
- После каждой публикации проверяй media.kind версии и возвращай ссылку вида
  `?tab=tasks&t=<kind>-<name>-<type>-<task>&v=<kind>-<name>-<vXXX>`.
- Рабочие файлы (PNG страниц, .md, PDF, meta, README) раскладывай в папку
  задачи `.../work/<bot>/<task>_vXXX/`, финальный PDF — ещё и в `render/vXXX`.
- Если что-то упало на середине (лимит шагов, таймаут) — на «продолжай»
  продолжай с того же места, а не начинай заново и не жди подтверждения.

## Веб-поиск и LoRA

Интернет-поиск — стандартный навык. Любой информационный вопрос про ComfyUI,
LoRA, ноды, модели, ошибки и решения воркфлоу решай через `web_search`
(DuckDuckGo MCP) и `web_fetch` по самому релевантному URL. Сначала ищи, потом
читай страницу, затем отвечай конкретными шагами со ссылками.

- Не отвечай «я не умею искать в интернете» — инструмент доступен.
- Не выдумывай названия нод и параметры: если не уверен, ищи.
- Для LoRA обычно нужны: `Load LoRA` (model/clip/lora_name/strength_model/
  strength_clip), цепочка нескольких Load LoRA, совместимость LoRA с базовой
  моделью и папка `ComfyUI/models/loras`.
- Хватает 1–2 поисков и 1–2 страниц; не зацикливайся на инструментах.

## ComfyUI сервер

ComfyUI установлен локально; путь берётся из `comfy.root` (сейчас
`/home/jaroslav/opt/ComfyUI`), API — `comfy.base_url`
(`http://127.0.0.1:8199`). Робот умеет им управлять:

- `comfyui_status` — путь установки, python, main.py, PID, лог, версия, GPU;
- `comfyui_start` — запустить сервер и дождаться ответа API;
- `comfyui_stop` — остановить (только по явной просьбе).

Генерация (`generate_image`, `generate_from_synx_version`, конвейер) сама
поднимает сервер, если он выключен. Если генерация упала с
`Connection refused` — не проси пользователя запускать вручную: вызови
`comfyui_status`, затем `comfyui_start` и повтори генерацию.

## ComfyUI MCP

В программе включён MCP `comfyui-mcp` (artokun/comfyui-mcp). Это основной
инструмент для работы с ComfyUI:

- `mcp_list_tools` server=comfyui-mcp — список возможностей;
- `mcp_call` tool=create_workflow — создать/modify/validate workflow JSON
  (txt2img, img2img, upscale, inpaint, controlnet, ip_adapter, ltx_video и др.);
- `mcp_call` tool=download_model — поиск и скачивание моделей/LoRA
  (HuggingFace, CivitAI; target_subfolder `checkpoints`, `loras`, `vae`, ...);
- `mcp_call` tool=list_local_models — что уже установлено;
- `mcp_call` tool=search_custom_nodes / install_custom_node — кастомные ноды;
- `mcp_call` tool=get_workflow / save_workflow / visualize_workflow.

Перед первым вызовом незнакомого инструмента запроси схему через
`mcp_call` tool=describe_tool. Для видео также доступен `ffmpeg-skill`
(`mcp_call` tool=probe/cut/fit/...). Для PDF проще прямой PIL-пайплайн.

## Создание ассетов, шотов и задач

Для «создай ассет/шот в проекте X» используй `synx_create_entity` ОДИН раз:
project, name, entity_kind (`asset`/`shot`), `asset_type` (можно label, инструмент
сам сопоставит с кодом; список — `synx_asset_types`), `info` — описание.
Для «создай задачу» — `synx_create_task` с project, shot/entity, task_name,
task_type (список — `synx_task_types`), assignee и comment. Не ходи по кругу
через fs_list/synx_versions/verify_pdf: это операции записи, а не поиска.

## Сценарий по шотам (пилот, эпизод)

Если просят «сценарий пилота по шотам», «расписать по секвенциям/шотам»,
«полный эпизод» — это НЕ прозаический пересказ. Формат обязательный:

- каждая секвенция = одна локация, заголовок `## S01 · НАТ. ЛОКАЦИЯ — ВРЕМЯ`;
- внутри — пронумерованные шоты `**S01-01 · WS · 8 c**`, ниже действие и реплики;
- у каждого шота есть план (WS/MS/CU/OTS), действие и длительность в секундах;
- эпизод целиком: 8–12 минут, сумма длительностей сходится с хронометражем;
- в конце — «Правила пилота» и таблица хронометража по секвенциям.

Порядок: напиши сценарий → `check_shot_script` (должен вернуть ok) →
`publish_shot_script` (сам соберёт текстовый PDF, опубликует версию, приложит
.md и PDF публикацией к задаче). Не публикуй прозу и не склеивай чужие версии.

## Текстовые документы (сценарии, планы, .md)

- Текстовый документ публикуй через `publish_text_document`: он собирает
  **векторный текстовый PDF** (настоящий выделяемый текст, белый фон,
  встроенные DejaVu), проверяет извлекаемость текста и публикует версию.
- НИКОГДА не перегоняй текст в картинки и не используй для текста чёрный
  daily-фон: чёрные страницы — только для PDF из кадров.
- Отдельный PDF без публикации — `make_text_pdf` (markdown → text PDF).
- Исходные .md/.txt/.json прикрепляй публикацией к задаче: параметр
  `attach_files` у `publish_text_document` или `synx_attach_files`
  (`target=task`, `target_id=<task_id>`). Synx принимает в комментарии
  только картинки, поэтому текстовые файлы идут публикацией (p001, p002…)
  и видны в задаче вместе с комментарием.
- После публикации проверь `media.kind`, число страниц и что `pdftotext`
  извлекает текст (инструмент делает это сам).

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

`ffmpeg`/`ffprobe` установлены и являются базовыми инструментами. Для видео
можно использовать и прямой инструмент `ffmpeg`, и установленный MCP
`ffmpeg-skill`: `mcp_list_tools` покажет инструменты, `mcp_call` вызовет
`probe`, `cut`, `fit`, `caption`, `graphics`, `render` и др. Для PDF проще
прямой PIL-пайплайн; MCP для PDF не обязателен.

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

PDF_RENDER_TOOL = {
    "type": "function",
    "function": {
        "name": "pdf_render_pages",
        "description": ("Render every page of a local PDF in parallel to cached PNGs. Returns a job id, "
                        "page count and status; page images are served by /api/pdf/page/{job_id}/{n}. "
                        "Use this to verify a PDF visually or to show cached high-resolution pages."),
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"},
            "dpi": {"type": "integer", "default": 200},
            "workers": {"type": "integer", "default": 4},
        }, "required": ["path"]},
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
                "text_layout": {"type": "string", "enum": ["stack", "columns"], "default": "stack"},
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
            "name": "synx_asset_types",
            "description": "List valid asset types (code and label) for a synx project.",
            "parameters": {"type": "object", "properties": {"project": {"type": "string"}},
                           "required": ["project"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "synx_task_types",
            "description": "List valid task types for a synx project.",
            "parameters": {"type": "object", "properties": {"project": {"type": "string"}},
                           "required": ["project"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "synx_assign_task",
            "description": ("Assign a bot/user to an EXISTING synx task by login (e.g. a.smirnov). "
                            "Use it instead of trying to recreate the task."),
            "parameters": {"type": "object", "properties": {
                "task_id": {"type": "string"},
                "bot": {"type": "string"},
            }, "required": ["task_id", "bot"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "synx_link_assets",
            "description": ("Link assets in synx. EntityCreate ignores linked_asset_ids, so links "
                            "are written by PATCH here, in both directions by default. Use for "
                            "location <-> objects/characters. Multi-location assets get all links."),
            "parameters": {"type": "object", "properties": {
                "project": {"type": "string"},
                "entity": {"type": "string", "description": "Entity name, e.g. loc_ocean_seiner"},
                "linked": {"type": "array", "items": {"type": "string"},
                           "description": "Names of assets to link (objects/characters/locations)"},
                "bidirectional": {"type": "boolean", "default": True},
                "replace": {"type": "boolean", "default": False},
            }, "required": ["project", "entity", "linked"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "synx_update_entity",
            "description": ("Update an entity: info (the ТЗ/description), display_name, "
                            "asset_type. Use to fill a missing ТЗ instead of leaving it empty."),
            "parameters": {"type": "object", "properties": {
                "project": {"type": "string"},
                "name": {"type": "string"},
                "info": {"type": "string", "description": "Full technical description (ТЗ)"},
                "display_name": {"type": "string"},
                "asset_type": {"type": "string"},
                "linked_assets": {"type": "array", "items": {"type": "string"}},
                "linked_asset_ids": {"type": "array", "items": {"type": "string"}},
            }, "required": ["project", "name"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "synx_task_comment",
            "description": ("Add a comment to a synx task. Use to write the task ТЗ if it is "
                            "missing or to document decisions."),
            "parameters": {"type": "object", "properties": {
                "task_id": {"type": "string"},
                "body": {"type": "string"},
                "files": {"type": "array", "items": {"type": "string"},
                          "description": "Images only (synx comments reject other files)"},
            }, "required": ["task_id", "body"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "synx_entities",
            "description": ("List entities of a synx project (id, name, display_name, kind, "
                            "asset_type, created_by, versions/tasks count). Use before bulk "
                            "cleanup or to find an asset to delete."),
            "parameters": {"type": "object", "properties": {
                "project": {"type": "string"},
                "entity_kind": {"type": "string", "enum": ["asset", "shot"]},
                "query": {"type": "string", "description": "Substring of name/display name"},
                "limit": {"type": "integer", "default": 500},
            }, "required": ["project"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "synx_delete_entity",
            "description": ("Delete an asset or shot from a synx project. Use ONLY on an explicit "
                            "user request («удали ассет X»). Refuses entities with versions/tasks "
                            "unless force=true; show the user what will be deleted first."),
            "parameters": {"type": "object", "properties": {
                "project": {"type": "string"},
                "name": {"type": "string", "description": "Entity name, e.g. loc_ocean_seiner"},
                "entity_id": {"type": "string"},
                "force": {"type": "boolean", "default": False,
                          "description": "Allow deleting an entity that has versions/tasks"},
            }, "required": ["project"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "synx_create_entity",
            "description": ("Create an asset or shot in a synx project. asset_type can be a code or label "
                            "(e.g. Script -> script). Use synx_asset_types to discover valid types."),
            "parameters": {"type": "object", "properties": {
                "project": {"type": "string"},
                "name": {"type": "string"},
                "entity_kind": {"type": "string", "enum": ["asset", "shot"], "default": "asset"},
                "asset_type": {"type": "string"},
                "info": {"type": "string"},
                "display_name": {"type": "string"},
                "cut_in": {"type": "integer"},
                "cut_out": {"type": "integer"},
                "task_types": {"type": "array", "items": {"type": "string"}},
                "skill_codes": {"type": "array", "items": {"type": "string"}},
                "task_name": {"type": "string", "description": "Task name (slug = folder name; defaults to entity name)"},
                "linked_assets": {"type": "array", "items": {"type": "string"},
                                  "description": "Assets to link (links are written by PATCH after creation)"},
            }, "required": ["project", "name"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "synx_set_task_status",
            "description": ("Change a synx task status (todo, wip, Pending_Review, done, "
                            "hold, omit, ...). Use for «закрой задачу», «переведи в done»."),
            "parameters": {"type": "object", "properties": {
                "task_id": {"type": "string"},
                "status": {"type": "string"},
            }, "required": ["task_id", "status"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "synx_create_task",
            "description": ("Create a task in synx for an existing shot/asset, optionally assign a bot/user. "
                            "task_type must be valid for the project."),
            "parameters": {"type": "object", "properties": {
                "project": {"type": "string"},
                "shot": {"type": "string", "description": "Entity name, e.g. main_script or p002_0130"},
                "task_name": {"type": "string"},
                "task_type": {"type": "string"},
                "assignee": {"type": "string", "description": "Bot/user short_name"},
                "comment": {"type": "string"},
                "status": {"type": "string", "default": "wip"},
                "bid": {"type": "number", "default": 3.0},
            }, "required": ["project", "shot", "task_name", "task_type"]},
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

MCP_TOOLS = [
    {"type": "function", "function": {
        "name": "mcp_list_tools",
        "description": ("List tools of enabled MCP servers. ffmpeg-skill is installed and enabled; "
                        "use it for video probe/cut/fit/caption/graphics etc."),
        "parameters": {"type": "object", "properties": {
            "server": {"type": "string", "description": "MCP server name/id; omit for all enabled"}
        }},
    }},
    {"type": "function", "function": {
        "name": "mcp_call",
        "description": ("Call a tool on an enabled MCP server. For ffmpeg-skill use e.g. "
                        "tool='probe' with arguments={'inputs':[path], 'json_brief':true}, or "
                        "tool='fit'/'cut'/'caption' with their schema. Results are returned as JSON."),
        "parameters": {"type": "object", "properties": {
            "server": {"type": "string"},
            "tool": {"type": "string"},
            "arguments": {"type": "object"},
            "timeout_sec": {"type": "integer", "default": 600},
        }, "required": ["tool"]},
    }},
]

TEXT_DOC_TOOLS = [
    {"type": "function", "function": {
        "name": "make_text_pdf",
        "description": ("Build a PDF from text/markdown with REAL selectable text, white pages "
                        "and embedded fonts (reportlab). Never renders text into bitmaps. "
                        "Use this for scripts, plans, .md documents — not the image/PDF pipeline."),
        "parameters": {"type": "object", "properties": {
            "text": {"type": "string", "description": "Markdown or plain text"},
            "path": {"type": "string", "description": "Output .pdf path (optional)"},
            "title": {"type": "string"},
            "page_size": {"type": "string", "enum": ["A4", "A3", "LETTER"], "default": "A4"},
            "font_size": {"type": "number", "default": 10.5},
        }, "required": ["text"]},
    }},
    {"type": "function", "function": {
        "name": "queue_generation_tasks",
        "description": ("Create visible pipeline tasks for generation (one per entity) and start "
                        "the pipeline. Use for ANY production work from chat instead of inline "
                        "generation; always report created task IDs to the user."),
        "parameters": {"type": "object", "properties": {
            "project": {"type": "string"},
            "entities": {"type": "array", "items": {"type": "string"},
                         "description": "Entity names; use [\"all_locations\"] for every location"},
            "prompt": {"type": "string", "description": "Base prompt; {entity} is replaced"},
            "negative": {"type": "string"},
            "task_name": {"type": "string", "default": "chat_generation"},
            "task_type": {"type": "string", "default": "Art"},
            "bot": {"type": "string"},
            "start": {"type": "boolean", "default": True},
        }, "required": ["project", "entities"]},
    }},
    {"type": "function", "function": {
        "name": "export_obsidian_map",
        "description": ("Export a synx project to Obsidian: one note per asset, a map note "
                        "with wikilinks and a JSON Canvas relationship map, under the given "
                        "section folder (e.g. TEST_PROJ). No questions."),
        "parameters": {"type": "object", "properties": {
            "project": {"type": "string", "default": "test_proekt_vfx"},
            "section": {"type": "string", "default": "TEST_PROJ"},
            "vault": {"type": "string", "description": "Vault path (optional)"},
        }},
    }},
    {"type": "function", "function": {
        "name": "create_shots_from_script",
        "description": ("Create a synx shot structure from a shot-by-shot script: canonical IDs "
                        "(s001_0010) as name AND display name, thesis in info, exact frame "
                        "durations, no tasks/versions, shots linked to location + participating "
                        "characters/props."),
        "parameters": {"type": "object", "properties": {
            "project": {"type": "string"},
            "script_path": {"type": "string"},
            "fps": {"type": "integer", "default": 25},
            "location_map": {"type": "object", "description": "sequence -> location asset name"},
            "link_locations": {"type": "boolean", "default": True},
            "create_tasks": {"type": "boolean", "default": True,
                             "description": "Create one Conform task per shot with TZ"},
            "task_name": {"type": "string", "default": "conform"},
            "task_type": {"type": "string", "default": "Conform"},
            "bot": {"type": "string", "default": "a.smirnov"},
        }, "required": ["project", "script_path"]},
    }},
    {"type": "function", "function": {
        "name": "create_tasks_for_all_shots",
        "description": ("Create one missing task per shot in a synx project in a single call. "
                        "Use for «создай задачи Conform на все шоты» instead of creating tasks "
                        "one by one. Idempotent: existing task_name/task_type pairs are skipped."),
        "parameters": {"type": "object", "properties": {
            "project": {"type": "string"},
            "task_name": {"type": "string", "default": "conform"},
            "task_type": {"type": "string", "default": "Conform"},
            "bot": {"type": "string", "default": "a.smirnov"},
        }, "required": ["project"]},
    }},
    {"type": "function", "function": {
        "name": "start_passport_batch",
        "description": ("Queue visible pipeline tasks for location passports: 9 views + 1 preview "
                        "page = 10-page A3 PDF per location, published as a new version. "
                        "Use for «по 9 видов + превью для всех локаций» — no extra questions."),
        "parameters": {"type": "object", "properties": {
            "project": {"type": "string"},
            "locations": {"type": "array", "items": {"type": "string"},
                          "description": "Location names or [\"all\"]"},
            "views": {"type": "integer", "default": 9},
            "bot": {"type": "string", "default": "a.smirnov"},
        }, "required": ["project"]},
    }},
    {"type": "function", "function": {
        "name": "publish_pdf_as_version",
        "description": ("Publish an existing PDF as a synx version of ANY entity (asset or shot): "
                        "locations, objects, characters. Use this for location passports and raster "
                        "PDFs — publish_images_as_version works only for shots."),
        "parameters": {"type": "object", "properties": {
            "project": {"type": "string"},
            "entity": {"type": "string", "description": "Asset/shot name, e.g. loc_ferma_late_summer"},
            "pdf_path": {"type": "string"},
            "task_name": {"type": "string", "description": "e.g. location_develop"},
            "task_type": {"type": "string", "description": "e.g. Concept"},
            "bot": {"type": "string"},
            "comment": {"type": "string"},
        }, "required": ["project", "entity", "pdf_path"]},
    }},
    {"type": "function", "function": {
        "name": "publish_text_document",
        "description": ("Publish a text document as a synx version: builds a vector text PDF "
                        "(or uses pdf_path), verifies that text is extractable, publishes from "
                        "the chosen bot, attaches the .md and extra files to the version comment. "
                        "Use for scripts/plans/documents instead of publish_images_as_version."),
        "parameters": {"type": "object", "properties": {
            "project": {"type": "string"},
            "entity": {"type": "string", "description": "Asset/shot name, e.g. main_script"},
            "text": {"type": "string", "description": "Markdown source (optional if pdf_path)"},
            "pdf_path": {"type": "string", "description": "Existing text PDF (optional)"},
            "title": {"type": "string"},
            "task_name": {"type": "string", "description": "e.g. write_script"},
            "task_type": {"type": "string", "description": "e.g. Script"},
            "bot": {"type": "string", "description": "Bot login, e.g. a.smirnov"},
            "comment": {"type": "string"},
            "attach_files": {"type": "array", "items": {"type": "string"},
                             "description": "Files to attach to the version comment (.md, .json, ...)"},
            "page_size": {"type": "string", "enum": ["A4", "A3", "LETTER"], "default": "A4"},
        }, "required": ["project", "entity"]},
    }},
    {"type": "function", "function": {
        "name": "check_shot_script",
        "description": ("Validate a shot-by-shot episode script: sequences (one per location), "
                        "numbered shots with plan/duration, total runtime 8-12 minutes."),
        "parameters": {"type": "object", "properties": {
            "text": {"type": "string"},
        }, "required": ["text"]},
    }},
    {"type": "function", "function": {
        "name": "publish_shot_script",
        "description": ("Publish a full pilot/episode script broken down by sequences and shots: "
                        "validates the structure, builds a vector text PDF, publishes it as a "
                        "version and attaches the .md + PDF to the task as a publication."),
        "parameters": {"type": "object", "properties": {
            "project": {"type": "string"},
            "entity": {"type": "string"},
            "text": {"type": "string", "description": "Markdown with ## S01 ... and **S01-01 · WS · 8 c**"},
            "title": {"type": "string"},
            "task_name": {"type": "string"},
            "task_type": {"type": "string", "default": "Script"},
            "bot": {"type": "string"},
            "comment": {"type": "string"},
            "attach_files": {"type": "array", "items": {"type": "string"}},
        }, "required": ["project", "entity", "text"]},
    }},
    {"type": "function", "function": {
        "name": "synx_attach_files",
        "description": ("Attach files in synx. target=task -> task publication (any file type, "
                        "use for .md/.txt/.json/PDF); target=version|entity -> comment "
                        "(images only, synx rejects other types)."),
        "parameters": {"type": "object", "properties": {
            "target": {"type": "string", "enum": ["version", "task", "entity"]},
            "target_id": {"type": "string"},
            "body": {"type": "string"},
            "files": {"type": "array", "items": {"type": "string"}},
        }, "required": ["target", "target_id", "files"]},
    }},
]

COMFY_SERVER_TOOLS = [
    {"type": "function", "function": {
        "name": "comfyui_status",
        "description": ("Show where ComfyUI is installed (root, python, main.py), whether the "
                        "server is running, its URL/port, PID, log path, version and GPU devices."),
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "comfyui_start",
        "description": ("Start the local ComfyUI server from its install directory and wait until "
                        "the API responds. Use when generation fails with 'Connection refused'. "
                        "Generation tools also start it automatically when it is down."),
        "parameters": {"type": "object", "properties": {
            "wait": {"type": "integer", "default": 120,
                     "description": "Seconds to wait for the API to become ready"},
        }},
    }},
    {"type": "function", "function": {
        "name": "comfyui_stop",
        "description": "Stop the local ComfyUI server. Only use when the user explicitly asks.",
        "parameters": {"type": "object", "properties": {}},
    }},
]

WEB_TOOLS = [
    {"type": "function", "function": {
        "name": "web_search",
        "description": ("Search the web through the enabled DuckDuckGo MCP. Use for ComfyUI/LoRA "
                        "workflow questions, node errors, model usage and current docs. "
                        "Set fetch_top=true to also read the first result as Markdown."),
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"},
            "max_results": {"type": "integer", "default": 8},
            "fetch_top": {"type": "boolean", "default": False},
            "max_length": {"type": "integer", "default": 8000},
        }, "required": ["query"]},
    }},
    {"type": "function", "function": {
        "name": "web_fetch",
        "description": ("Fetch a web page through DuckDuckGo MCP and extract readable text/Markdown. "
                        "Use after web_search for the most relevant URL."),
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string"},
            "max_length": {"type": "integer", "default": 8000},
            "parse_mode": {"type": "string", "enum": ["text", "main", "markdown"], "default": "markdown"},
        }, "required": ["url"]},
    }},
]

TOOLS = [IMAGE_TOOL, VERSION_GEN_TOOL, PDF_TOOL, PDF_RENDER_TOOL, VERIFY_PDF_TOOL, REPAIR_PDF_TOOL, DAILY_PDF_TOOL, PUBLISH_TOOL, FFMPEG_TOOL] + TEXT_DOC_TOOLS + COMFY_SERVER_TOOLS + WEB_TOOLS + MCP_TOOLS + SYNX_TOOLS + FS_TOOLS
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
        if not _is_image_ref(im):
            continue
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
    if not comfy_server.status().get("running"):
        if progress:
            progress({"type": "stage",
                      "text": f"ComfyUI не запущен — запускаю {comfy_server.install_root()}"})
        started = comfy_server.ensure(progress=progress)
        if not started.get("running") and started.get("error"):
            raise AIError("ComfyUI не запустился: " + str(started["error"]))
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


def _show_request(text: str) -> str | None:
    """"покажи картинку" / "покажи исходник" — return 'last' or 'source'."""
    low = (text or "").lower()
    if not re.search(r"(покажи|показать|выведи|открой)", low):
        return None
    if re.search(r"(нарисуй|сгенерир|сделай\s+(?:мне\s+)?(?:картинк|изображени|кадр))", low):
        return None
    if re.search(r"(исходник|референс|оригинал|source|reference)", low):
        return "source"
    if re.search(r"(картинк|изображени|кадр|фото|image|результат)", low):
        return "last"
    return None


def _entity_create_request(text: str) -> dict[str, Any] | None:
    low = (text or "").lower()
    if "созда" not in low or not ("ассет" in low or "шот" in low or "shot" in low):
        return None
    m_project = re.search(r"в\s+проект[еа]?\s+([A-Za-z0-9_.-]+)", text or "", re.I)
    if not m_project:
        return None
    m_name = re.search(r"называться\s+[«\"']?([A-Za-z0-9_.-]+)", text or "", re.I)
    if not m_name:
        m_name = re.search(r"название\s*[:—-]\s*[«\"']?([A-Za-z0-9_.-]+)", text or "", re.I)
    if not m_name:
        return None
    kind = "shot" if ("шот" in low or "shot" in low) else "asset"
    m_type = re.search(r"тип[а]?\s+[«\"']?([A-Za-z0-9_.-]+)", text or "", re.I)
    m_desc = re.search(r"в\s+описании\s+(?:напиши\s+)?[«\"']?(.+?)[»\"']?(?:\.|$)",
                       text or "", re.I | re.S)
    return {"project": m_project.group(1).rstrip(".,;"),
            "name": m_name.group(1).rstrip(".,;"),
            "entity_kind": kind,
            "asset_type": m_type.group(1).rstrip(".,;") if m_type else None,
            "info": m_desc.group(1).strip() if m_desc else ""}


def _latest_shot_script(project: str) -> Path | None:
    cfg = load_config()
    projects_root = Path((cfg.get("paths") or {}).get("projects_root") or "/mnt/projects")
    base = projects_root / project / "assets" / "main_script" / "art" / "work"
    files: list[Path] = []
    for pat in ("*/*pilot_shot_script*/*.md", "*/*shot_script*/*.md"):
        files += [p for p in base.glob(pat)
                  if p.is_file() and "shot_script" in p.name.lower()
                  and not p.name.lower().startswith("readme")]
    if not files:
        files = [p for p in (DATA_DIR / "plans").glob("*shot_script*.md") if p.is_file()]
    if not files:
        return None
    def ver(p: Path):
        m = re.search(r"v(\d{3})", str(p))
        return (int(m.group(1)) if m else 0, p.stat().st_mtime)
    return max(files, key=ver)


def _shot_structure_request(text: str, session_id: str = "default") -> dict[str, Any] | None:
    low = (text or "").lower()
    if not re.search(r"(созда|пересозда|сделай|собери|построй)", low):
        return None
    if "шот" not in low:
        return None
    if not ("сценар" in low or "структур" in low):
        return None
    if "задач" in low and "верси" not in low:
        # "создай задачи по шотам" is not this branch
        if "структур" not in low:
            return None
    project = _project_from_text(text)
    if not project:
        focus = db.get_chat_focus(session_id) or {}
        project = focus.get("project") or "test_proekt_vfx"
    m = re.search(r"([\w./~-]+\.md)", text or "")
    path = str(Path(m.group(1)).expanduser()) if m else None
    if not path:
        latest = _latest_shot_script(project)
        path = str(latest) if latest else None
    return {"project": project, "script_path": path}


def _all_shot_tasks_request(text: str, session_id: str = "default") -> dict[str, Any] | None:
    """«Создай задачи Conform на все шоты» — one bulk deterministic call."""
    low = (text or "").lower()
    if "задач" not in low or "шот" not in low:
        return None
    if not re.search(r"(все|кажд|для\s+всех|на\s+все)", low):
        return None
    if "conform" not in low and "конформ" not in low:
        return None
    project = _project_from_text(text)
    if not project:
        focus = db.get_chat_focus(session_id) or {}
        project = focus.get("project") or "test_proekt_vfx"
    return {"project": project, "task_name": "conform", "task_type": "Conform",
            "bot": "a.smirnov"}


def _passport_batch_request(text: str, session_id: str = "default") -> dict[str, Any] | None:
    """«Паспорта всех локаций: 9 видов + превью, 10 страниц» — background batch."""
    low = (text or "").lower()
    if "паспорт" not in low:
        return None
    if not re.search(r"(\b9\b|девять|10\s*страниц|десять\s*страниц|превью|\d+\s*вид)", low):
        return None
    project = _project_from_text(text)
    if not project:
        focus = db.get_chat_focus(session_id) or {}
        project = focus.get("project") or "test_proekt_vfx"
    views = 9
    m = re.search(r"(\d{1,2})\s*вид", low)
    if m:
        views = max(1, min(12, int(m.group(1))))
    bot = "a.smirnov"
    try:
        from config import load_bots
        for b in load_bots():
            login = str(b.get("login") or "")
            if login and login.lower() in low:
                bot = login
                break
    except Exception:
        pass
    return {"project": project, "locations": ["all"], "views": views, "bot": bot}


def _pdf_remix_request(text: str) -> dict[str, Any] | None:
    low = (text or "").lower()
    # Text deliverables are handled by make_text_pdf / publish_text_document,
    # not by the frame-layout remix pipeline.
    if re.search(r"(из\s+текста|текстов(?:ый|ого|ом)\s+pdf|markdown|\.md\b|"
                 r"документ|сценари|план[ае]?\s+сери|паспорт|превью|"
                 r"\d+\s*вид)", low):
        return None
    has_pdf = bool(re.search(r"(pdf|пдф)", low, re.I))
    layout = any(k in low for k in ("черн", "чёрн", "dpi", "текст", "центр", "поля",
                                    "кадр", "страниц", "лист", "верси"))
    action = any(k in low for k in ("сделай", "собери", "исправ", "передел",
                                    "опубликуй", "сделать", "получаю"))
    if not layout or not action:
        return None
    if not has_pdf:
        # Version/layout edits without the word "pdf": require a strong page
        # layout signal and a version reference, and never steal workflow or
        # image-generation requests.
        if re.search(r"(воркфлоу|workflow|сгенерир|генерац|нарисуй)", low, re.I):
            return None
        strong = any(k in low for k in ("черн", "чёрн", "dpi", "текст", "центр",
                                        "поля", "кадр", "страниц", "лист"))
        if not strong or not re.search(r"(верси|\bv\d{1,3}\b)", low, re.I):
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
        for m in re.finditer(r"верси[юия]?\s*(?:v)?(\d{1,3})", low):
            before = low[max(0, m.start() - 20):m.start()]
            if re.search(r"(сделай|создай|опубликуй|нужна|нужна\s+новая)\s*$", before):
                continue
            base_version = f"v{int(m.group(1)):03d}"
            break
        if not base_version:
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
    elif "текст" in low and ("меньше" in low or "мельче" in low):
        text_scale = 0.8
    image_valign = "center" if any(k in low for k in (
        "по-вертикали", "по вертикали", "по центру", "середине", "строго по середине")) else "top"
    text_layout = "stack"
    if any(k in low for k in ("разборчив", "читаем", "читаются", "буквы", "мелкий текст")):
        text_layout = "columns"
        text_scale = max(text_scale, 1.0)
    return {"base_version": base_version, "dpi": dpi, "text_scale": text_scale,
            "image_valign": image_valign, "text_layout": text_layout,
            "background": "#000000"}


def _remix_task(shot: str | None = None) -> dict[str, Any] | None:
    tasks = db.list_tasks(limit=200)
    if shot:
        tasks = [t for t in tasks if str(t.get("shot") or "").lower() == shot.lower()]
    for t in tasks:
        wd = Path(str(t.get("work_dir") or ""))
        if wd.is_dir() and list(wd.glob("frame_*.png")):
            if str(t.get("task_name") or "") in ("frames_pdf", "cubism_frames",
                                                 "frames_pdf_fix", "frames_pdf_a4") \
                    or str(t.get("shot") or "") == "p002_0130":
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


def _workflow_ref_request(text: str, session_id: str = "default") -> dict[str, Any] | None:
    """"Build a workflow from version X" — resolve it, never publish blindly."""
    t = text or ""
    if not re.search(r"(воркфлоу|workflow)", t, re.I):
        return None
    if not re.search(r"(возьми|бер[её]м|за основу|на основе|используй|рендер|render)", t, re.I):
        return None
    m = FOCUS_CODE_RE.search(t)
    if m:
        shot = m.group(1).lower()
        version = "v" + str(int(m.group(3))).zfill(3)
    else:
        m = FOCUS_SHOT_RE.search(t)
        if not m:
            return None
        shot = m.group(1).lower()
        version = ("v" + str(int(m.group(2))).zfill(3)) if m.group(2) else None
    project = _project_from_text(t)
    if not project:
        focus = db.get_chat_focus(session_id) or {}
        project = focus.get("project") or ""
    return {"project": project, "shot": shot, "version": version}


def _comfy_alive() -> bool:
    return bool(comfy_server.status().get("running"))


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


def _is_image_ref(ref: dict[str, Any]) -> bool:
    if not isinstance(ref, dict):
        return False
    if ref.get("kind") == "file":
        return False
    if ref.get("kind") == "image":
        return True
    mime = str(ref.get("mime") or "").lower()
    if mime:
        return mime.startswith("image/")
    p = str(ref.get("path") or ref.get("name") or "")
    return Path(p).suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"}


def _file_refs_note(refs: list[dict[str, Any]]) -> str:
    files = [r for r in (refs or []) if not _is_image_ref(r)]
    if not files:
        return ""
    lines = ["", "", "Прикреплённые файлы-референсы (не изображения):"]
    for r in files:
        size = r.get("size")
        size_txt = ""
        if isinstance(size, (int, float)) and size > 0:
            mb = size / 1048576.0
            size_txt = f", {mb:.1f} МБ" if mb >= 1 else f", {int(size / 1024)} КБ"
        lines.append(f"- {r.get('name') or 'file'} — `{r.get('path') or ''}` "
                     f"({r.get('mime') or 'application/octet-stream'}{size_txt})")
    lines += ["", "Эти файлы доступны по указанным путям: читай их через fs_read. "
                  "Не передавай не-изображения в generate_image/reference_paths."]
    return "\n".join(lines)


def _image_content(images: list[dict[str, Any]]) -> list[dict[str, Any]]:
    parts = []
    for im in images or []:
        if not _is_image_ref(im):
            continue
        p = im.get("path") if isinstance(im, dict) else None
        if p and Path(p).is_file():
            try:
                parts.append({"type": "image_url", "image_url": {"url": _data_url(Path(p))}})
            except Exception:
                pass
    return parts


def _history_messages(session_id: str) -> list[dict[str, Any]]:
    # Short window: long PDF/chat histories made the model reuse old shots and
    # versions instead of the one named in the current request.
    rows = db.list_chat_messages(session_id, limit=16)
    # On an explicit shot/version switch keep only messages from that point:
    # otherwise old shots and old PDF experiments leak into the new task.
    cut = 0
    for i, r in enumerate(rows):
        if r.get("role") == "user":
            body = r.get("content") or ""
            if FOCUS_CODE_RE.search(body) or FOCUS_SHOT_RE.search(body):
                cut = i
    rows = rows[cut:]
    out = [{"role": "system", "content": _system()}]
    focus = db.get_chat_focus(session_id)
    if focus and (focus.get("shot") or focus.get("project")):
        out.append({"role": "system", "content": (
            "## Текущий фокус\n"
            f"- project: {focus.get('project') or '—'}\n"
            f"- shot: {focus.get('shot') or '—'}\n"
            f"- version: {focus.get('version') or '—'}\n"
            "Работай только с этим шотом, пока пользователь явно не назовёт другой. "
            "Упоминание версии — референс, а не разрешение публиковать.")})
    ctx = db.get_chat_context(session_id)
    if ctx:
        out.append({"role": "system",
                    "content": "## Импортированный контекст предыдущего чата\n\n" + ctx[:120000]})
    with_images = [i for i, r in enumerate(rows) if r.get("images")]
    keep_images = set(with_images[-2:])
    for i, r in enumerate(rows):
        text_body = (r.get("content") or "")[:12000]
        refs = r.get("images") or []
        if r.get("role") == "user" and refs:
            text_body += _file_refs_note(refs)
        if r.get("role") == "user" and refs and i in keep_images:
            content: list[dict[str, Any]] = [{"type": "text", "text": text_body}]
            content.extend(_image_content(refs))
            out.append({"role": "user", "content": content})
        else:
            out.append({"role": r.get("role") or "assistant", "content": text_body})
    return out


FOCUS_CODE_RE = re.compile(
    r"\b((?:[a-z]{1,4}\d{2,4}[a-z]?|\d{2,4}[a-z])_\d{3,5})_([a-z][a-z0-9]*)_v(\d{1,4})\b", re.I)
FOCUS_SHOT_RE = re.compile(
    r"\b((?:[a-z]{1,4}\d{2,4}[a-z]?|\d{2,4}[a-z])_\d{3,5})(?:_v(\d{1,4}))?\b", re.I)
FOCUS_VERSION_RE = re.compile(r"\b(?:верси[яию]|version)\s*v?(\d{1,4})\b", re.I)
PROJECT_HINTS = {
    "либерия": "liberia", "liberia": "liberia",
    "колоссус": "colossus", "colossus": "colossus",
    "тестовый проект": "test_proekt_vfx", "test_proekt_vfx": "test_proekt_vfx",
    "экспериментальный проект": "test_proekt_vfx",
    "экспериментальном проекте": "test_proekt_vfx",
}


def _project_from_text(text: str) -> str | None:
    low = (text or "").lower()
    try:
        for alias in load_config().get("synx", {}).get("projects") or []:
            if str(alias).lower() in low:
                return str(alias)
    except Exception:
        pass
    for hint, alias in PROJECT_HINTS.items():
        if hint in low:
            return alias
    return None


FOCUS_IN_PROJECT_RE = re.compile(
    r"\b(?:в|на|проект[ае]?)\s+([a-z0-9][a-z0-9_.-]*)\s*/\s*([a-z0-9][a-z0-9_.-]*)", re.I)
FOCUS_NAMED_ENTITY_RE = re.compile(
    r"\b(?:сущность|ассет|шота|шот|shot|asset|entity)\s+[«\"']?([A-Za-z][A-Za-z0-9_.-]{2,})", re.I)


def _focus_ids_from_line(text: str) -> list[str]:
    """Identifiers after «фокус:» / «focus:» (project, entity, shot code)."""
    m = re.search(r"(?:фокус|focus)\s*[:\-—]?\s*(.+)", text or "", re.I)
    if not m:
        return []
    ids = re.findall(r"[A-Za-z0-9][A-Za-z0-9_.-]{2,}", m.group(1))
    out: list[str] = []
    for i in ids:
        il = i.lower().rstrip(".;,")
        if re.fullmatch(r"v?\d+", il):
            continue
        out.append(il)
    return out


def _update_focus(session_id: str, text: str) -> dict[str, Any]:
    """Remember the entity/version named in the current message.

    Recognises shot codes (p002_0130_v017), named assets (main_script),
    focus lines («фокус: test_proekt_vfx / main_script») and Russian project
    display names («тестовый проект»). Also persists the publish intent: once
    the user said «опубликуй», it stays for the session, so «продолжай»
    still allows publishing without re-asking.
    """
    t = text or ""
    low = t.lower()
    project = _project_from_text(t)
    shot = None
    version = None

    # explicit focus line: «фокус: test_proekt_vfx / main_script»
    m_ids = _focus_ids_from_line(t)
    if m_ids:
        proj_l = str(project or "").lower()
        for cand in m_ids:
            if project and cand == proj_l:
                continue
            if FOCUS_SHOT_RE.fullmatch(cand) or FOCUS_CODE_RE.fullmatch(cand):
                shot = cand
                break
        if not shot:
            ents = [c for c in m_ids if c != proj_l
                    and not (FOCUS_SHOT_RE.fullmatch(cand := c) or FOCUS_CODE_RE.fullmatch(c))]
            if ents:
                shot = ents[0]
    # «в <project> / <entity>»
    if not shot:
        for mp, me in FOCUS_IN_PROJECT_RE.findall(t):
            if project and mp.lower() == str(project).lower():
                shot = me.lower()
                break
    # «сущность/ассет/шот <name>»
    if not shot:
        m = FOCUS_NAMED_ENTITY_RE.search(t)
        if m:
            shot = m.group(1).lower()
    # shot codes with version
    if not shot:
        m = FOCUS_CODE_RE.search(t)
        if m:
            shot = m.group(1).lower()
            version = "v" + str(int(m.group(3))).zfill(3)
        else:
            m = FOCUS_SHOT_RE.search(t)
            if m:
                shot = m.group(1).lower()
                if m.group(2):
                    version = "v" + str(int(m.group(2))).zfill(3)
                else:
                    mv = re.search(r"\bv(\d{1,4})\b", t[m.end():m.end() + 60], re.I)
                    if mv:
                        version = "v" + str(int(mv.group(1))).zfill(3)
    if not version:
        mv = FOCUS_VERSION_RE.search(t)
        if mv:
            version = "v" + str(int(mv.group(1))).zfill(3)
    intent = None
    if NO_PUBLISH_RE.search(low):
        intent = False
    elif PUBLISH_INTENT_RE.search(low):
        intent = True
    db.set_chat_focus(session_id, project=project or None, shot=shot,
                      version=version, last_text=t, publish_intent=intent)
    return db.get_chat_focus(session_id) or {}


def _recent_user_texts(session_id: str, n: int = 8) -> str:
    rows = db.all(
        "SELECT content FROM chat_messages WHERE session_id=? AND role='user' "
        "ORDER BY id DESC LIMIT ?", (session_id, int(n)))
    return "\n".join(str(r.get("content") or "") for r in reversed(rows))


def _user_named_target(session_id: str, name: str, n: int = 10) -> bool:
    """True when the target entity was named by the user in recent messages."""
    name = str(name or "").strip().lower()
    if not name:
        return True
    try:
        recent = _recent_user_texts(session_id, n).lower()
    except Exception:
        return False
    return re.search(r"(?<![\w])" + re.escape(name) + r"(?![\w])", recent) is not None


PUBLISH_INTENT_RE = re.compile(
    r"(опублику|публикуй|публикац|запуш|выложи|залей|publish|"
    r"сделай\s+(?:новую\s+)?верси|создай\s+(?:новую\s+)?верси)", re.I)
NO_PUBLISH_RE = re.compile(r"(не\s+публик|не\s+надо\s+публик|без\s+публикац)", re.I)


def _publish_intent(text: str) -> bool:
    low = (text or "").lower()
    if NO_PUBLISH_RE.search(low):
        return False
    return bool(PUBLISH_INTENT_RE.search(low))


def _mcp_text(result: dict[str, Any]) -> str:
    res = result.get("result") or {}
    parts = []
    for c in res.get("content") or []:
        if isinstance(c, dict) and c.get("type") == "text":
            parts.append(str(c.get("text") or ""))
    return "\n".join(parts)


def _ddg_server() -> str:
    try:
        for s in mcp_manager.load_servers():
            name = str(s.get("name") or "").lower()
            if s.get("enabled") and ("ddg" in name or "duckduckgo" in name):
                return str(s.get("name"))
    except Exception:
        pass
    return "ddg-search"


WEB_HINT_RE = re.compile(
    r"(в\s+интернете|в\s+сети|из\s+интернета|погугл|загугл|web[\s-]*search|онлайн)", re.I)
SEARCH_VERB_RE = re.compile(r"(поищи|найди|ищи|search|проверь)", re.I)
TECH_TOPIC_RE = re.compile(
    r"(comfy|комфи|lora|loras|лора|лоры|нод[аыуе]?|node|workflow|воркфлоу|"
    r"модел[ьия]|model|flux|sdxl|sd1\.5|stable\s*diffusion|hunyuan|wan2|"
    r"upscal|апскейл|inpaint|controlnet|ipadapter|ip-adapter|safetensors)", re.I)
SYNX_TOPIC_RE = re.compile(
    r"(synx|синкс|верси[яиюей]|шот[аыуе]?|ассет|задач[аиуей]|публик|publish|проект)", re.I)
QUESTION_RE = re.compile(
    r"(^\s*(как|что|какой|какие|какая|каков|почему|зачем|можно\s+ли|где|сколько|чем|"
    r"подскажи|расскажи|объясни|есть\s+ли|существуют\s+ли)|\?\s*$)", re.I)


def _web_request(text: str) -> bool:
    """True when the message asks for internet research."""
    t = (text or "").strip()
    if not t:
        return False
    if WEB_HINT_RE.search(t):
        return True
    if SEARCH_VERB_RE.search(t) and TECH_TOPIC_RE.search(t) and not SYNX_TOPIC_RE.search(t):
        return True
    if re.search(r"(что\s+пишут|какие\s+(?:есть\s+)?(?:lora|loras|лора|лоры))", t, re.I):
        return True
    # Local ComfyUI server questions are answered by comfyui_status, not search.
    if re.search(r"comfy(?:ui)?", t, re.I) and re.search(
            r"(установлен|запущен|запустить|сервер|порт|статус|лог|pid|верси|папк|путь|"
            r"connection\s*refused|не\s+запуска)", t, re.I):
        return False
    if TECH_TOPIC_RE.search(t) and QUESTION_RE.search(t) and not SYNX_TOPIC_RE.search(t):
        return True
    return False


def _web_queries(text: str, ai: Any, usage: dict[str, int]) -> list[str]:
    queries: list[str] = []
    try:
        raw, u = ai.chat([
            {"role": "system", "content": (
                "Extract 1 or 2 short web search queries for the user request. "
                "Each query: max 12 words, keep exact product/node/model names, "
                "add 'ComfyUI' when the question is about it. "
                "Return ONLY a JSON array of strings, e.g. [\"ComfyUI Flux LoRA support\"].")},
            {"role": "user", "content": (text or "")[:2000]},
        ])
        usage["input"] += int(u.get("input") or 0)
        usage["output"] += int(u.get("output") or 0)
        m = re.search(r"\[.*?\]", raw or "", re.S)
        if m:
            arr = json.loads(m.group(0))
            queries = [str(x).strip() for x in arr if str(x).strip()][:2]
    except Exception:
        pass
    if not queries:
        q = re.sub(r"(поищи|найди|ищи|погугли|загугли|пожалуйста|в\s+интернете)", " ",
                   text or "", flags=re.I)
        q = " ".join(q.split())[:220]
        queries = [q] if q else [(text or "")[:220]]
    return queries


def _clean_html(raw: str) -> str:
    s = re.sub(r"(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", raw or "")
    s = re.sub(r"(?is)<br\s*/?>", "\n", s)
    s = re.sub(r"(?is)</(p|div|section|article|li|h[1-6]|tr|td)>", "\n", s)
    s = re.sub(r"(?s)<[^>]+>", " ", s)
    s = html_lib.unescape(s)
    s = re.sub(r"[ \t\r\f\v]+", " ", s)
    s = re.sub(r"\n\s*\n+", "\n\n", s)
    return s.strip()


def _fetch_page(url: str, max_length: int = 6000) -> str:
    """Fetch a page through DuckDuckGo MCP, falling back to a direct request."""
    text = ""
    try:
        f = mcp_runtime.call(_ddg_server(), "fetch_content",
                             {"url": url, "max_length": max_length, "parse_mode": "markdown"},
                             timeout=60)
        text = _mcp_text(f)
    except Exception as exc:
        text = f"Error: {exc}"
    low = text.strip().lower()
    if len(text.strip()) < 300 or low.startswith("error"):
        try:
            r = requests.get(url, timeout=20, headers={
                "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                               "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
                "Accept-Language": "en-US,en;q=0.8,ru;q=0.7",
            })
            if r.status_code < 400 and r.text:
                cleaned = _clean_html(r.text)
                if len(cleaned) > len(text.strip()):
                    text = cleaned
        except Exception as exc:
            if not text.strip() or low.startswith("error"):
                text = f"Error: {exc}"
    return text[:max_length]


def _web_research(text: str, ai: Any, emit: Any, usage: dict[str, int]) -> dict[str, Any]:
    server = _ddg_server()
    queries = _web_queries(text, ai, usage)
    blocks: list[str] = []
    sources: list[str] = []
    results: list[dict[str, Any]] = []
    for q in queries:
        emit({"type": "stage", "text": f"Поиск: {q[:70]}"})
        try:
            d = mcp_runtime.call(server, "search", {"query": q, "max_results": 6}, timeout=60)
            txt = _mcp_text(d)
        except Exception as exc:
            txt = f"search failed: {exc}"
        results.append({"query": q, "results": txt[:6000]})
        blocks.append(f"### Результаты поиска: {q}\n{txt[:6000]}")
        for url in re.findall(r"URL:\s*(https?://\S+)", txt):
            if url not in sources:
                sources.append(url)
    pages: list[dict[str, Any]] = []
    good = 0
    for url in sources[:4]:
        if good >= 2:
            break
        emit({"type": "stage", "text": f"Читаю {url[:70]}..."})
        c = _fetch_page(url, 6000)
        if len(c.strip()) >= 300 and not c.strip().lower().startswith("error"):
            pages.append({"url": url, "content": c})
            blocks.append(f"### Страница: {url}\n{c}")
            good += 1
        else:
            pages.append({"url": url, "error": c[:200]})
    return {"queries": queries, "results": results, "sources": sources,
            "pages": pages, "context": "\n\n".join(blocks)[:24000]}


def _publish_guard(session_id: str, args: dict[str, Any]
                   ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Check that publishing is allowed and align the target with the focus.

    Returns (args_to_use, error). Publishing requires an explicit request
    remembered for the session; a target named by the user in recent messages
    is allowed and becomes the new focus.
    """
    sid = session_id or "default"
    focus = db.get_chat_focus(sid) or {}
    intent = bool(focus.get("publish_intent")) or _publish_intent(str(focus.get("last_text") or ""))
    if not intent:
        return None, {"error": "publish_not_requested",
                      "message": ("Пользователь не просил публиковать версию. Собери PDF и "
                                  "покажи результат; публикуй только по явной команде "
                                  "«опубликуй», «сделай версию», «запушь», «сделай публикацию».")}
    out = dict(args)
    fshot = str(focus.get("shot") or "")
    requested = str(out.get("shot") or out.get("entity") or "").strip()
    if fshot:
        if requested and requested.lower() != fshot.lower():
            if _user_named_target(sid, requested):
                db.set_chat_focus(sid, shot=requested)
            else:
                return None, {"error": "focus_mismatch",
                              "message": (f"Текущий фокус — {fshot}, а выбран {requested}. "
                                          "Пользователь не называл эту цель в последних "
                                          "сообщениях. Если цель верная, назови её текстом: "
                                          "«фокус: <проект>/<сущность>».")}
        elif not requested:
            out.setdefault("shot", fshot)
            out.setdefault("entity", fshot)
            if focus.get("project") and not out.get("project"):
                out["project"] = focus["project"]
            if focus.get("version") and not out.get("version"):
                out["version"] = focus["version"]
    return out, None


def _execute_tool(name: str, args: dict[str, Any], images: list[dict[str, Any]],
                  progress=None, session_id: str | None = None
                  ) -> tuple[Any, list[dict[str, Any]], str | None]:
    """Returns (tool_result, generated_images, playlist_text)."""
    if name == "comfyui_status":
        return comfy_server.status(), [], None
    if name == "comfyui_start":
        wait = int(args.get("wait") or 0) or None
        return comfy_server.start(wait=wait, progress=progress), [], None
    if name == "comfyui_stop":
        return comfy_server.stop(), [], None
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
    if name == "synx_asset_types":
        return synx_tools.asset_types(args.get("project") or ""), [], None
    if name == "synx_task_types":
        return synx_tools.task_types(args.get("project") or ""), [], None
    if name == "synx_create_entity":
        try:
            return synx_tools.create_entity(
                args.get("project") or "", args.get("name") or "",
                args.get("entity_kind") or "asset", args.get("asset_type"),
                args.get("info"), args.get("display_name"), args.get("cut_in"),
                args.get("cut_out"), args.get("task_types"), args.get("skill_codes"),
                args.get("task_name"), args.get("linked_assets")), [], None
        except Exception as exc:
            return {"error": str(exc)}, [], None
    if name == "synx_assign_task":
        try:
            return synx_tools.assign_task(str(args.get("task_id") or ""),
                                          str(args.get("bot") or "")), [], None
        except Exception as exc:
            return {"error": str(exc)}, [], None
    if name == "synx_link_assets":
        try:
            return synx_tools.link_assets(
                args.get("project") or "", args.get("entity") or "",
                list(args.get("linked") or []),
                bool(args.get("bidirectional", True)),
                bool(args.get("replace", False))), [], None
        except Exception as exc:
            return {"error": str(exc)}, [], None
    if name == "synx_update_entity":
        try:
            return synx_tools.update_entity(
                args.get("project") or "", args.get("name") or "",
                args.get("info"), args.get("display_name"),
                args.get("asset_type"), args.get("linked_asset_ids"),
                args.get("linked_assets")), [], None
        except Exception as exc:
            return {"error": str(exc)}, [], None
    if name == "synx_task_comment":
        try:
            import synx as synx_mod
            cfg = load_config()
            client = synx_mod.SynxClient(cfg["synx"]["base_url"], cfg["synx"]["login"],
                                         cfg["synx"]["password"])
            files = [str(f) for f in (args.get("files") or [])]
            return {"ok": True, "comment": client.task_comment(
                str(args.get("task_id") or ""), args.get("body") or "", files=files or None)}, [], None
        except Exception as exc:
            return {"error": str(exc)}, [], None
    if name == "synx_entities":
        try:
            return synx_tools.entities(
                args.get("project") or "", args.get("entity_kind"),
                args.get("query"), int(args.get("limit") or 500)), [], None
        except Exception as exc:
            return {"error": str(exc)}, [], None
    if name == "synx_delete_entity":
        try:
            return synx_tools.delete_entity(
                args.get("project") or "", args.get("name"),
                args.get("entity_id"), bool(args.get("force"))), [], None
        except Exception as exc:
            return {"error": str(exc)}, [], None
    if name == "synx_set_task_status":
        try:
            import synx as synx_mod
            cfg = load_config()
            client = synx_mod.SynxClient(cfg["synx"]["base_url"], cfg["synx"]["login"],
                                         cfg["synx"]["password"])
            client.task_status(str(args.get("task_id") or ""), str(args.get("status") or "done"))
            return {"ok": True, "task_id": args.get("task_id"),
                    "status": args.get("status")}, [], None
        except Exception as exc:
            return {"error": str(exc)}, [], None
    if name == "synx_create_task":
        try:
            return synx_tools.create_task(
                args.get("project") or "", args.get("shot") or "",
                args.get("task_name") or "", args.get("task_type") or "",
                args.get("assignee"), args.get("comment") or "",
                args.get("status") or "wip", float(args.get("bid") or 3.0)), [], None
        except Exception as exc:
            return {"error": str(exc)}, [], None
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
        out_dir = DATA_DIR / "chat" / "pdf"
        out_dir.mkdir(parents=True, exist_ok=True)
        name = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(args.get("pdf_name") or "frames")).strip("_") or "frames"
        pdf = publish_flow.make_pdf([i["path"] for i in imgs], out_dir / (name + ".pdf"),
                                    page_size=args.get("page_size") or "A4",
                                    margin_mm=float(args.get("margin_mm") or 10),
                                    dpi=int(args.get("dpi") or 144))
        info = publish_flow.verify_pdf(pdf, expected_pages=len(imgs))
        return {"ok": bool(info.get("ok")), "pdf": str(pdf), "pages": len(imgs),
                "pdf_info": info, "images": [i["path"] for i in imgs]}, [], None
    if name == "pdf_render_pages":
        try:
            return pdf_cache.start(args.get("path") or "", dpi=int(args.get("dpi") or 200),
                                   workers=int(args.get("workers") or 4)), [], None
        except Exception as exc:
            return {"error": str(exc)}, [], None
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
        focus = db.get_chat_focus(session_id or "default") or {}
        if not (bool(focus.get("publish_intent")) or _publish_intent(str(focus.get("last_text") or ""))):
            return {"error": "publish_not_requested",
                    "message": ("Пользователь не просил публиковать версию. Собери PDF и "
                                "покажи результат; публикуй только по явной команде.")}, [], None
        fshot = str(focus.get("shot") or "")
        task_id = int(args.get("task_id") or 0)
        if not task_id:
            rows = db.list_tasks(limit=50)
            for t in rows:
                if fshot and str(t.get("shot") or "") != fshot:
                    continue
                wd = Path(str(t.get("work_dir") or ""))
                if wd.is_dir() and list(wd.glob("frame_*.png")):
                    task_id = int(t["id"]); break
        task = db.get_task(task_id)
        if not task:
            return {"error": f"task not found: {task_id}"}, [], None
        if fshot and str(task.get("shot") or "") != fshot:
            if _user_named_target(session_id or "default", str(task.get("shot") or "")):
                db.set_chat_focus(session_id or "default", shot=str(task.get("shot") or ""))
            else:
                return {"error": "focus_mismatch",
                        "message": (f"Задача относится к шоту {task.get('shot')}, а текущий "
                                    f"фокус — {fshot}. Не публикуй другой шот.")}, [], None
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
            text_layout=args.get("text_layout") or "stack",
            background=args.get("background") or "#000000",
            base_version=args.get("base_version"),
            publish=bool(args.get("publish", True)), bot_login=args.get("bot"),
            progress=progress)
        return result, [], None
    if name == "make_text_pdf":
        try:
            title = args.get("title") or "document"
            out = Path(str(args.get("path") or ""))
            if not out:
                out = CHAT_DIR / "pdf" / (re.sub(r"[^A-Za-z0-9_.-]+", "_", title)[:60] + ".pdf")
            info = text_pdf.make_text_pdf(
                args.get("text") or "", out, title=title,
                page_size=args.get("page_size") or "A4",
                font_size=float(args.get("font_size") or 10.5))
            return info, [], None
        except Exception as exc:
            return {"error": str(exc)}, [], None
    if name == "queue_generation_tasks":
        try:
            project = args.get("project") or ""
            ents = list(args.get("entities") or [])
            if ents == ["all_locations"] or args.get("all_locations"):
                ents = pipeline_queue.locations(project)
            if not ents:
                return {"error": "entities is empty"}, [], None
            base = args.get("prompt") or "Concept art of {entity}"
            result = pipeline_queue.queue_many(
                project, ents,
                lambda n: base.replace("{entity}", n) if "{entity}" in base
                else f"{base} ({n})",
                negative=args.get("negative") or "",
                task_name=args.get("task_name") or "chat_generation",
                task_type=args.get("task_type") or "Art",
                bot_login=args.get("bot"),
                start=bool(args.get("start", True)))
            return result, [], None
        except Exception as exc:
            return {"error": str(exc)}, [], None
    if name == "export_obsidian_map":
        try:
            return obsidian_export.export_project(
                args.get("project") or "test_proekt_vfx",
                args.get("section") or "TEST_PROJ",
                args.get("vault")), [], None
        except Exception as exc:
            return {"error": str(exc)}, [], None
    if name == "create_tasks_for_all_shots":
        try:
            return shot_script.create_tasks_for_entities(
                args.get("project") or "", task_name=args.get("task_name") or "conform",
                task_type=args.get("task_type") or "Conform",
                bot_login=args.get("bot") or "a.smirnov", progress=progress), [], None
        except Exception as exc:
            return {"error": str(exc)}, [], None
    if name == "create_shots_from_script":
        try:
            return shot_script.create_structure(
                args.get("project") or "", args.get("script_path") or "",
                fps=int(args.get("fps") or 25),
                location_map=args.get("location_map") or {},
                link_locations=bool(args.get("link_locations", True)),
                create_tasks=bool(args.get("create_tasks", True)),
                task_name=args.get("task_name") or "conform",
                task_type=args.get("task_type") or "Conform",
                bot_login=args.get("bot") or "a.smirnov",
                progress=progress), [], None
        except Exception as exc:
            return {"error": str(exc)}, [], None
    if name == "start_passport_batch":
        try:
            running = passport_batch.is_running()
            if running:
                return {"already_running": True, **running}, [], None
            return passport_batch.start_via_pipeline(
                args.get("project") or "", list(args.get("locations") or ["all"]),
                int(args.get("views") or 9), args.get("bot") or "a.smirnov"), [], None
        except Exception as exc:
            return {"error": str(exc)}, [], None
    if name == "publish_pdf_as_version":
        args, err = _publish_guard(session_id or "default", args)
        if err:
            return err, [], None
        try:
            return doc_flow.publish_pdf_as_version(
                args.get("project") or "", args.get("entity") or args.get("shot") or "",
                args.get("pdf_path") or "",
                task_name=args.get("task_name"), task_type=args.get("task_type"),
                bot_login=args.get("bot"), comment=args.get("comment") or "",
                progress=progress), [], None
        except Exception as exc:
            return {"error": str(exc)}, [], None
    if name == "publish_text_document":
        args, err = _publish_guard(session_id or "default", args)
        if err:
            return err, [], None
        try:
            result = doc_flow.publish_text_document(
                args.get("project") or "",
                args.get("entity") or args.get("shot") or "",
                text=args.get("text"),
                pdf_path=args.get("pdf_path"),
                title=args.get("title"),
                task_name=args.get("task_name"),
                task_type=args.get("task_type"),
                bot_login=args.get("bot"),
                comment=args.get("comment") or "",
                attach_files=args.get("attach_files") or [],
                page_size=args.get("page_size") or "A4",
                font_size=float(args.get("font_size") or 10.5),
                progress=progress)
            return result, [], None
        except Exception as exc:
            return {"error": str(exc)}, [], None
    if name == "check_shot_script":
        try:
            return script_tools.validate_shot_script(args.get("text") or ""), [], None
        except Exception as exc:
            return {"error": str(exc)}, [], None
    if name == "publish_shot_script":
        try:
            text = args.get("text") or ""
            checks = script_tools.validate_shot_script(text)
            if not checks.get("ok"):
                return {"error": "shot_script_invalid", **checks}, [], None
            args, err = _publish_guard(session_id or "default", args)
            if err:
                return err, [], None
            result = doc_flow.publish_text_document(
                args.get("project") or "",
                args.get("entity") or args.get("shot") or "",
                text=text,
                title=args.get("title") or "Сценарий по шотам",
                task_name=args.get("task_name") or "pilot_shot_script",
                task_type=args.get("task_type") or "Script",
                bot_login=args.get("bot"),
                comment=args.get("comment") or "",
                attach_files=args.get("attach_files") or [],
                progress=progress)
            return {**result, "script_check": checks}, [], None
        except Exception as exc:
            return {"error": str(exc)}, [], None
    if name == "synx_attach_files":
        try:
            from synx import SynxClient
            cfg = load_config()
            client = SynxClient(cfg["synx"]["base_url"], cfg["synx"]["login"],
                                cfg["synx"]["password"])
            target = str(args.get("target") or "task").lower()
            tid = str(args.get("target_id") or "")
            files = [str(f) for f in (args.get("files") or []) if Path(str(f)).is_file()]
            if not files:
                return {"error": "files is empty or not found"}, [], None
            body = args.get("body") or "Файлы: " + ", ".join(Path(f).name for f in files)
            if target == "task":
                pub = client.create_publication(tid, files, comment=body,
                                               note="Synx AI Robot attachment")
                return {"ok": True, "target": "task", "publication": pub,
                        "publication_code": pub.get("publication_code"),
                        "files": pub.get("files")}, [], None
            image_exts = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".avif"}
            if not all(Path(f).suffix.lower() in image_exts for f in files):
                return {"error": "synx_comments_images_only",
                        "message": ("synx принимает в комментарии только картинки. "
                                    "Текстовые файлы прикрепляй публикацией к задаче: "
                                    "target=task, target_id=<task_id>.")}, [], None
            fn = {"version": client.version_comment,
                  "entity": client.entity_comment}.get(target)
            if not fn:
                return {"error": "target must be task|version|entity"}, [], None
            return {"ok": True, "target": target, "target_id": tid,
                    "comment": fn(tid, body, files=files)}, [], None
        except Exception as exc:
            return {"error": str(exc)}, [], None
    if name == "publish_images_as_version":
        args, err = _publish_guard(session_id or "default", args)
        if err:
            return err, [], None
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
    if name == "web_search":
        try:
            server = _ddg_server()
            q = args.get("query") or ""
            d = mcp_runtime.call(server, "search",
                                 {"query": q, "max_results": int(args.get("max_results") or 8)},
                                 timeout=60)
            text = _mcp_text(d)
            urls = re.findall(r"URL:\s*(https?://\S+)", text)
            fetched = None
            if args.get("fetch_top") and urls:
                max_len = int(args.get("max_length") or 8000)
                f = mcp_runtime.call(server, "fetch_content",
                                     {"url": urls[0], "max_length": max_len,
                                      "parse_mode": "markdown"}, timeout=90)
                fetched = {"url": urls[0], "content": _mcp_text(f)[:max_len]}
            return {"query": q, "results": text[:14000], "sources": urls[:10],
                    "fetched": fetched}, [], None
        except Exception as exc:
            return {"error": str(exc)}, [], None
    if name == "web_fetch":
        try:
            server = _ddg_server()
            max_len = int(args.get("max_length") or 8000)
            d = mcp_runtime.call(server, "fetch_content",
                                 {"url": args.get("url") or "", "max_length": max_len,
                                  "parse_mode": args.get("parse_mode") or "markdown"},
                                 timeout=90)
            return {"url": args.get("url"), "content": _mcp_text(d)[:max_len]}, [], None
        except Exception as exc:
            return {"error": str(exc)}, [], None
    if name == "mcp_list_tools":
        try:
            server = args.get("server")
            if server:
                d = mcp_runtime.list_tools(server)
                return {"server": d.get("server"), "count": d.get("count"),
                        "tools": [{"name": t.get("name"),
                                   "description": (t.get("description") or "")[:220]}
                                  for t in d.get("tools") or []],
                        "hint": "Use mcp_call with tool=describe_tool for the full JSON schema."}, [], None
            servers = [s for s in mcp_manager.load_servers() if s.get("enabled")]
            out = []
            for srv in servers:
                try:
                    d = mcp_runtime.list_tools(srv.get("name"))
                    out.append({"server": srv.get("name"), "count": d.get("count"),
                                "tools": [t.get("name") for t in d.get("tools") or []]})
                except Exception as exc:
                    out.append({"server": srv.get("name"), "error": str(exc)})
            return {"servers": out}, [], None
        except Exception as exc:
            return {"error": str(exc)}, [], None
    if name == "mcp_call":
        try:
            result = mcp_runtime.call(
                args.get("server"), args.get("tool") or "", args.get("arguments") or {},
                timeout=float(args.get("timeout_sec") or 600))
            return result, [], None
        except Exception as exc:
            return {"error": str(exc)}, [], None
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


def chat_export_markdown(session_id: str = "default") -> dict[str, Any]:
    session = db.one("SELECT * FROM chat_sessions WHERE id=?", (session_id,)) or {}
    rows = db.list_chat_messages(session_id, limit=100000)
    title = session.get("title") or "Чат"
    lines = [f"# Чат: {title}", "", f"- Сессия: `{session_id}`",
             f"- Экспорт: {datetime.now().isoformat(timespec='seconds')}", ""]
    ctx = db.get_chat_context(session_id)
    if ctx:
        lines += ["## Импортированный контекст", "", ctx, ""]
    for m in rows:
        role = "Пользователь" if m.get("role") == "user" else "Ассистент"
        lines += [f"## {role}", ""]
        content = str(m.get("content") or "")
        if content:
            lines += [content, ""]
        imgs = (m.get("images") if m.get("role") == "user" else m.get("generated")) or []
        for im in imgs:
            name = im.get("name") or "image"
            url = im.get("url") or ""
            path = im.get("path") or ""
            if url:
                lines.append(f"![{name}]({url})")
            if path:
                lines.append(f"Файл: `{path}`")
        if m.get("playlist_text"):
            lines += ["Плейлист:", "```text", str(m.get("playlist_text")), "```", ""]
        stages = [e.get("text") for e in (m.get("progress") or []) if e.get("type") == "stage"]
        if stages:
            lines += ["<details><summary>Этапы</summary>", ""]
            lines += [f"- {x}" for x in stages]
            lines += ["", "</details>", ""]
        usage = m.get("usage") or {}
        if usage.get("input") or usage.get("output"):
            lines += [f"Токены: {usage.get('input', 0)} / {usage.get('output', 0)}", ""]
    text = "\n".join(lines).strip() + "\n"
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", title).strip("-")[:40] or "chat"
    return {"markdown": text, "filename": f"{safe}-{date.today().isoformat()}.md",
            "session_id": session_id, "messages": len(rows)}


def chat_turn(session_id: str, text: str, images: list[dict[str, Any]] | None = None,
              progress=None, job: dict[str, Any] | None = None) -> dict[str, Any]:
    images = images or []
    stages: list[dict[str, Any]] = []

    def emit(evt: dict[str, Any]):
        _check_cancel(job or {})
        evt = dict(evt)
        evt["seq"] = len(stages) + 1
        stages.append(evt)
        if progress:
            progress(evt)

    emit({"type": "stage", "text": "Анализирую запрос..."})
    db.add_chat_message(session_id, "user", text, images=images)
    _update_focus(session_id, text or "")
    history = _history_messages(session_id)
    cfg = load_config()
    ai = AIClient(cfg)
    usage = {"input": 0, "output": 0}
    generated: list[dict[str, Any]] = []
    playlist_text: str | None = None
    content = ""
    tool_used = False
    ai_error = False

    # "Покажи картинку / исходник" must actually attach an image, not describe it.
    show = _show_request(text or "")
    if show:
        content = ""
        try:
            if show == "source":
                focus = db.get_chat_focus(session_id) or {}
                shot = str(focus.get("shot") or "")
                if not shot:
                    raise ValueError("не знаю, какой исходник показать: назови шот и версию")
                emit({"type": "stage", "text": "Беру исходник версии в synx..."})
                ref = synx_tools.prepare_reference(
                    str(focus.get("project") or ""), shot, focus.get("version"), "middle")
                info = ref.get("version") or {}
                IMAGES.mkdir(parents=True, exist_ok=True)
                name = f"source_{shot}_{info.get('version') or 'v'}.png"
                dst = IMAGES / name
                shutil.copy2(ref["path"], dst)
                generated = [{"name": name, "path": str(dst),
                              "url": f"/api/chat/file/images/{name}"}]
                content = (f"Вот исходник **{info.get('project') or focus.get('project')}/"
                           f"{info.get('entity') or shot} {info.get('version') or ''}** "
                           f"({ref.get('width')}×{ref.get('height')}).")
                if re.search(r"(позы|поза|не похоже|скелет)", (text or "").lower()):
                    content += ("\n\nОбычный restyle в FLUX не сохраняет позы — для этого нужен "
                                "pose-воркфлоу: OpenPose/ControlNet по скелету + генерация по нему.")
            else:
                imgs = publish_flow.last_generated_images(session_id, limit_messages=10)
                if not imgs:
                    content = "В этом чате пока нет сгенерированных картинок."
                else:
                    generated = imgs
                    content = "Вот последняя сгенерированная картинка."
        except Exception as exc:
            content = f"Не смог показать изображение: {exc}"
        emit({"type": "stage", "text": "Готово"})
        db.add_chat_message(session_id, "assistant", content, generated=generated,
                            usage=usage, playlist_text=None, progress=stages)
        return {"messages": db.list_chat_messages(session_id), "usage": usage,
                "generated": generated, "playlist_text": None, "stages": stages}

    # Create asset/shot requests ("создай ассет main_script типа Script...").
    ent = _entity_create_request(text or "")
    if ent:
        try:
            emit({"type": "stage", "text": "Создаю сущность в synx..."})
            result = synx_tools.create_entity(
                ent["project"], ent["name"], ent["entity_kind"],
                asset_type=ent.get("asset_type"), info=ent.get("info"))
            entity = result.get("entity") or {}
            kind_label = "Ассет" if entity.get("entity_kind") == "asset" else "Шот"
            if result.get("exists"):
                content = (f"{kind_label} **{entity.get('name')}** уже существует в проекте "
                           f"**{ent['project']}**.\n\n- Тип: `{entity.get('asset_type') or ent.get('asset_type') or '—'}`\n"
                           f"- Описание: {entity.get('info') or ent.get('info') or '—'}\n"
                           f"- Ссылка: {result.get('url')}")
            else:
                content = (f"Готово. {kind_label} **{entity.get('name')}** создан в проекте "
                           f"**{ent['project']}**.\n\n- Тип: `{entity.get('asset_type') or ent.get('asset_type') or '—'}`\n"
                           f"- Описание: {entity.get('info') or ent.get('info') or '—'}\n"
                           f"- ID: `{entity.get('id')}`\n"
                           f"- Ссылка: {result.get('url')}")
        except Exception as exc:
            content = f"Не удалось создать сущность: {exc}"
        emit({"type": "stage", "text": "Готово"})
        db.add_chat_message(session_id, "assistant", content, generated=[],
                            usage=usage, playlist_text=None, progress=stages)
        return {"messages": db.list_chat_messages(session_id), "usage": usage,
                "generated": [], "playlist_text": None, "stages": stages}

    # Shot structure from a shot-by-shot script: deterministic, no tasks/versions.
    shot_req = _shot_structure_request(text or "", session_id)
    if shot_req:
        content = ""
        try:
            if not shot_req.get("script_path"):
                raise ValueError("не нашёл файл сценария с шотами")
            emit({"type": "stage", "text": "Создаю шоты по сценарию..."})
            result = shot_script.create_structure(
                shot_req["project"], shot_req["script_path"],
                location_map={}, link_locations=True, progress=emit)
            frames = result.get("frames_per_sequence") or {}
            lines = [f"Готово: **{result.get('sequences')}** секвенций, "
                     f"**{result.get('shots')}** шотов.",
                     f"Создано: {result.get('created')}, обновлено: {result.get('updated')}.",
                     "", "Кадры по секвенциям:"]
            for code in sorted(frames):
                lines.append(f"- {code}: {frames[code]} кадров")
            lines += ["", f"Всего: **{result.get('total_frames')}** кадров.",
                      f"Задач Conform создано: {result.get('tasks_created', 0)}, "
                      f"назначено ботов: {result.get('tasks_assigned', 0)}.",
                      "У шотов display_name = имя; связаны локация + участники сцены; "
                      "версии не создавались."]
            if result.get("errors"):
                lines += ["", "Ошибки: " + "; ".join(
                    f"{e.get('shot')}: {e.get('error')}" for e in result["errors"][:5])]
            content = "\n".join(lines)
        except Exception as exc:
            content = f"Не удалось создать структуру шотов: {exc}"
        emit({"type": "stage", "text": "Готово"})
        db.add_chat_message(session_id, "assistant", content, generated=[],
                            usage=usage, playlist_text=None, progress=stages)
        return {"messages": db.list_chat_messages(session_id), "usage": usage,
                "generated": [], "playlist_text": None, "stages": stages}

    # Bulk tasks for every shot ("создай Conform на все шоты").
    tasks_req = _all_shot_tasks_request(text or "", session_id)
    if tasks_req:
        content = ""
        try:
            emit({"type": "stage", "text": "Создаю задачи на все шоты..."})
            result = shot_script.create_tasks_for_entities(
                tasks_req["project"], task_name=tasks_req["task_name"],
                task_type=tasks_req["task_type"], bot_login=tasks_req["bot"],
                progress=emit)
            lines = [f"Готово: шотов **{result.get('shots')}**, "
                     f"создано задач **{result.get('created')}**, "
                     f"назначено ботов **{result.get('assigned')}**."]
            if result.get("errors"):
                lines.append("Ошибки: " + "; ".join(
                    f"{e.get('shot')}: {e.get('error')}" for e in result["errors"][:8]))
            content = "\n".join(lines)
        except Exception as exc:
            content = f"Не удалось создать задачи: {exc}"
        emit({"type": "stage", "text": "Готово"})
        db.add_chat_message(session_id, "assistant", content, generated=[],
                            usage=usage, playlist_text=None, progress=stages)
        return {"messages": db.list_chat_messages(session_id), "usage": usage,
                "generated": [], "playlist_text": None, "stages": stages}

    # Location passport batch: 9 views + 1 preview page = 10-page A3 PDF per
    # location, published as a new version. Runs in the background.
    passport_req = _passport_batch_request(text or "", session_id)
    if passport_req:
        try:
            running = passport_batch.is_running()
            if running:
                content = (f"Батч паспортов уже идёт (PID `{running.get('pid')}`), "
                           f"статус: `{running.get('status_file')}`. Дождусь завершения "
                           "и не запускаю второй.")
            else:
                emit({"type": "stage", "text": "Ставлю задачи паспортов в конвейер..."})
                status = passport_batch.start_via_pipeline(
                    passport_req["project"], passport_req["locations"],
                    passport_req["views"], passport_req["bot"])
                content = (
                    f"Запущено через конвейер: {status.get('queued')} задач "
                    f"`location_passport` (по {passport_req['views']} видов + превью, "
                    f"10 страниц A3). Публикация версиями Concept/location_develop "
                    f"от {passport_req['bot']}.\n\n"
                    f"- Проект: **{passport_req['project']}**\n"
                    f"- Задачи: `{status.get('task_ids')}`\n"
                    f"- Очередь: {status.get('pipeline', {}).get('queue')}\n\n"
                    "Прогресс виден в разделе «Задачи» и в статусе конвейера.")
        except Exception as exc:
            content = f"Не удалось запустить батч паспортов: {exc}"
        emit({"type": "stage", "text": "Готово"})
        db.add_chat_message(session_id, "assistant", content, generated=[],
                            usage=usage, playlist_text=None, progress=stages)
        return {"messages": db.list_chat_messages(session_id), "usage": usage,
                "generated": [], "playlist_text": None, "stages": stages}

    # PDF layout remix: "based on v013, make vN with black pages, 300 dpi,
    # 20% smaller text, image centered". This must run before the generic
    # PDF-publish path, otherwise the robot falls back to a white A4 PDF.
    remix = _pdf_remix_request(text or "")
    if remix:
        try:
            focus = db.get_chat_focus(session_id) or {}
            task = _remix_task(str(focus.get("shot") or "") or None)
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
                text_layout=remix.get("text_layout") or "stack",
                background=remix["background"],
                base_version=remix.get("base_version"),
                publish=True, progress=emit)
            corrected = (f" Базовая версия: {remix.get('base_version')}." if remix.get("base_version") else "")
            content = (
                f"Готово. Собрал PDF: {result.get('pages')} страниц, "
                f"{result.get('page_size')}, {result.get('dpi')} dpi.{corrected}\n\n"
                f"- Фон: {result.get('background')}; текст: {int(round(float(result.get('text_scale') or 1) * 100))}% от базы; "
                f"layout: {result.get('text_layout')}; кадр: {result.get('image_valign')} по вертикали.\n"
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

    # Internet research is a standard skill: explicit requests ("поищи в
    # интернете") and technical questions about ComfyUI/LoRA are answered from
    # live search results plus the most relevant pages, not from model memory.
    if _web_request(text or ""):
        content = ""
        try:
            emit({"type": "stage", "text": "Ищу в интернете..."})
            research = _web_research(text or "", ai, emit, usage)
            if not research.get("sources"):
                content = ("Поиск не дал результатов. Проверь формулировку или сеть, "
                           "затем попробуй снова.")
            elif ai.ready:
                emit({"type": "stage", "text": "Формулирую ответ по источникам..."})
                material = research.get("context") or ""
                system = (
                    "Ты — технический ассистент по ComfyUI, LoRA, нодам и воркфлоу. "
                    "Отвечай по-русски, кратко и по шагам. Используй ТОЛЬКО факты из "
                    "предоставленных результатов поиска и страниц; если факта там нет — "
                    "честно скажи, чего не хватает. Не выдумывай названия нод и параметры. "
                    "В конце добавь раздел «Источники» со ссылками из найденных URL. "
                    "Не пиши, что не умеешь искать в интернете.")
                prompt = (f"Запрос пользователя:\n{text}\n\n"
                          f"Найденные материалы:\n{material}")
                answer, u = ai.chat([{"role": "system", "content": system},
                                     {"role": "user", "content": prompt}])
                usage["input"] += int(u.get("input") or 0)
                usage["output"] += int(u.get("output") or 0)
                content = (answer or "").strip()
                if not content:
                    content = "Нашёл материалы, но не смог сформулировать ответ."
                if not re.search(r"https?://", content):
                    content += ("\n\nИсточники:\n" + "\n".join(
                        f"- {u}" for u in research.get("sources", [])[:8]))
            else:
                content = ("Нашёл в интернете:\n" + "\n".join(
                    f"- {u}" for u in research.get("sources", [])[:8]))
        except Exception as exc:
            content = f"Не удалось выполнить поиск в интернете: {exc}"
        emit({"type": "stage", "text": "Готово"})
        db.add_chat_message(session_id, "assistant", content, generated=[],
                            usage=usage, playlist_text=None, progress=stages)
        return {"messages": db.list_chat_messages(session_id), "usage": usage,
                "generated": [], "playlist_text": None, "stages": stages}

    # "Take version X as the basis and build a workflow" must resolve that
    # exact version and must never publish a PDF for some older shot.
    wf_req = _workflow_ref_request(text or "", session_id)
    if wf_req:
        content = ""
        try:
            emit({"type": "stage", "text": "Ищу версию и беру рендер за основу..."})
            ref = synx_tools.prepare_reference(
                wf_req["project"], wf_req["shot"], wf_req.get("version"), "middle")
            info = ref.get("version") or {}
            comfy = _comfy_alive()
            if not comfy and comfy_server.status().get("auto_start"):
                emit({"type": "stage", "text": "ComfyUI не запущен — запускаю сервер..."})
                started = comfy_server.ensure(progress=emit)
                comfy = bool(started.get("running"))
            lines = [
                f"Взял за основу **{info.get('project') or wf_req['project']}/"
                f"{info.get('shot') or wf_req['shot']} "
                f"{info.get('version') or wf_req.get('version') or ''}**.",
                "",
                f"- Референс-кадр: `{ref.get('path')}` "
                f"({ref.get('width')}×{ref.get('height')})",
                "- Версия и файл проверены в synx, ничего не публиковал.",
            ]
            if comfy:
                lines += ["", "ComfyUI доступен. В текущих профилях нет OpenPose/ControlNet — "
                          "могу собрать pose-воркфлоу через MCP (`create_workflow`) и добавить "
                          "ControlNet-ноды, но это отдельная сборка. Подтверди — и я начну."]
            else:
                st = comfy_server.status()
                lines += ["", f"**ComfyUI не запущен** (`{st.get('url')}`), автозапуск не удался.",
                          f"- Установлен: `{st.get('install_root')}`",
                          f"- Лог: `{st.get('log')}`",
                          "Проверь лог или укажи `comfy.root` в настройках, затем повтори запрос — "
                          "возьму этот же референс."]
            content = "\n".join(lines)
        except Exception as exc:
            content = (f"Не смог подготовить референс по версии "
                       f"{wf_req.get('shot')} {wf_req.get('version') or ''}: {exc}")
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

    seen_calls: dict[str, int] = {}
    try:
        max_steps = int((cfg.get("ai") or {}).get("max_tool_steps") or 100)
        for _step in range(max_steps):
            emit({"type": "stage", "text": "Думаю..."})
            if _step:
                _check_cancel(job or {})
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
                seen_calls[signature] = seen_calls.get(signature, 0) + 1
                repeats = seen_calls[signature]
                if repeats > 1:
                    result = {"error": "duplicate_tool_call",
                              "message": "Этот вызов уже выполнен. Используй предыдущий результат "
                                         "и не повторяй его; переходи к следующему шагу."}
                else:
                    try:
                        emit({"type": "stage", "text": f"Инструмент: {name}"})
                        result, gen, pl = _execute_tool(name, args, images, progress=emit,
                                                        session_id=session_id)
                        generated.extend(gen)
                        if pl:
                            playlist_text = pl
                    except ChatCancelled:
                        raise
                    except Exception as exc:
                        result = {"error": str(exc)}
                history.append({"role": "tool", "tool_call_id": call.get("id") or name,
                                "content": json.dumps(result, ensure_ascii=False)[:12000]})
        if not content:
            # The tool loop is over: answer from what was already collected
            # instead of dropping the request with a loop-limit message.
            try:
                emit({"type": "stage", "text": "Формулирую ответ..."})
                history.append({"role": "system", "content": (
                    "Текущий шаг завершён. Сформулируй ответ пользователю по уже "
                    "полученным результатам. Не говори, что инструменты отключены: "
                    "они доступны. Если работа не закончена, просто перечисли, что "
                    "осталось сделать, и продолжи в следующем сообщении.")})
                msg, u = ai.chat_message(history)
                usage["input"] += u.get("input", 0)
                usage["output"] += u.get("output", 0)
                content = (msg.get("content") or "").strip()
            except Exception:
                pass
        if not content:
            content = ("Не смог закончить за отведённое число шагов. "
                       "Уточни запрос или попроси конкретное действие.")
    except ChatCancelled as exc:
        content = str(exc)
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


class ChatCancelled(RuntimeError):
    """Raised inside a chat job when the user presses «Стоп»."""


def _check_cancel(job: dict[str, Any]) -> None:
    if job.get("cancel"):
        raise ChatCancelled("Прервано пользователем")


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
            result = chat_turn(session_id, text, images, progress=progress, job=job)
            with _JOBS_LOCK:
                job["result"] = result
                job["status"] = "done"
        except ChatCancelled as exc:
            with _JOBS_LOCK:
                job["status"] = "cancelled"
                job["error"] = str(exc)
                job["events"].append({"type": "stage", "text": "Прервано пользователем",
                                      "seq": len(job["events"]) + 1})
        except Exception as exc:
            with _JOBS_LOCK:
                job["error"] = str(exc)
                job["status"] = "error"

    threading.Thread(target=run, daemon=True, name="chat-job").start()
    return job_id


def stop_chat_job(job_id: str) -> dict[str, Any] | None:
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if not job:
            return None
        if job["status"] == "running":
            job["cancel"] = True
            job["events"].append({"type": "stage", "text": "Прерываю...",
                                  "seq": len(job["events"]) + 1})
        return {"id": job["id"], "status": job["status"], "cancel": bool(job.get("cancel"))}


def get_chat_job(job_id: str) -> dict[str, Any] | None:
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if not job:
            return None
        return {"id": job["id"], "status": job["status"], "events": list(job["events"]),
                "error": job["error"],
                "result": job["result"]}
