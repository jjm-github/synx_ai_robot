# Synx API (production: https://synks.ru)

Авторизация: `POST /api/auth/login` JSON `{"short_name": "...", "password": "..."}`.
Сессия — cookie; клиент приложения хранит base_url, логин и пароль в настройках.
При 502/503/504 API временно недоступен — повтори запрос через 3–5 секунд.

## Чтение

- `GET /api/projects` — проекты: `id`, `alias`, `name`, `fps`, `output_format`.
- `GET /api/projects/{project_id}/entities` — сущности проекта (shot/asset и др.).
- `GET /api/projects/{project_id}/tasks` — все задачи проекта.
- `GET /api/projects/{project_id}/versions` — все версии проекта.
- `GET /api/projects/{project_id}/task_types` — допустимые типы задач проекта.
- `GET /api/entities/{entity_id}/tasks` — задачи сущности.
- `GET /api/entities/{entity_id}/versions` — версии сущности.
- `GET /api/entities/{entity_id}/comments` — комментарии сущности.
- `GET /api/tasks/{task_id}/comments` — комментарии задачи.
- `GET /api/versions/{version_id}/comments` — комментарии версии.
- `GET /api/tasks/{task_id}/assignments-form` — пользователи, доступные для назначения, с полем `skills`.
- `GET /api/tasks/{task_id}/assignments` — текущие назначения.
- `GET /files/{file_id}` — файл версии (видео/картинка/PDF).
- `GET /files/{file_id}/thumb` — превью файла.

Версия содержит: `id`, `entity_id`, `task_id`, `task_name`, `task_type`,
`version_number`, `version_code`, `file_id`, `file_url`, `pretty_url`,
`original_name`, `status`, `comment`, `created_at`, `media` (`kind`, `width`,
`height`, `fps`, `frames`, `duration_secs`, `codec`), `entity_name`,
`entity_kind`, `author`.

Типы `media.kind`: `video`, `image`, `pdf`, `null`. Для PDF `frames` — число
страниц, `duration_secs` — страницы × 2 секунды. Если `media.kind=null`, synx
не распознал файл как медиа и плеер может показать пустоту, даже если файл
валиден. После публикации PDF всегда проверяй `media.kind` в ответе/версии.

## События и сообщения

- `GET /api/events?limit=1000` — лента событий: `tag`, `comment`, `version`,
  `assignment`, `status`, `priority`, `creation`, `review`.
  - `tag` — упоминание пользователя, в т.ч. `@p.robot`; смотри `recipients`,
    `comment_body`, `comment_id`, `entity_id`, `task_id`, `version_id`.
- `POST /api/events/{event_id}/read` — отметить событие прочитанным.
- `POST /api/events/read-all` — отметить всё прочитанным.
- `GET /api/direct/unread` — непрочитанные личные сообщения.
- `GET/POST /api/direct/{user_id}/messages` — личные сообщения.
- `PATCH/DELETE /api/direct/messages/{message_id}` — правка/удаление.

Программа уже мониторит `/api/events` и складывает новые комментарии и
`@p.robot` в локальную таблицу. Для вопросов «что нового» используй таблицу,
а не повторный полный обход API.

## Создание сущностей и задач

- `GET /api/projects/{project_id}/asset_types` — допустимые типы ассетов
  (`code` + `label`; например `script` = «Script»).
- `POST /api/projects/{project_id}/entities` JSON `EntityCreate`:
  `entity_kind` (`asset`/`shot`), `name`, `display_name`, `info`,
  `cut_in`, `cut_out`, `asset_type` (code, не label), `linked_asset_ids`,
  `skill_codes`, `task_types`.
  Пример ассета: `{"entity_kind":"asset","name":"main_script",
  "asset_type":"script","info":"разработка главного сценария фильма"}`.
  Имя шота должно быть в формате `021a_0010` (sequence + 4 цифры); иначе 422
  `Shot name must look like 021a_0010`.
- `PATCH /api/entities/{entity_id}` — изменить сущность.
- `PATCH /api/entities/{entity_id}/status` — статус сущности.
- `PATCH /api/entities/{entity_id}/length` — длина.
- `PATCH /api/projects/{project_id}/entities/asset-type` — массовая смена типа.
- `POST /api/entities/{entity_id}/tasks` — создать задачу
  (`task_name`, `task_type`, `bid`, `status`, `comment`).
- `POST /api/tasks/{task_id}/assignments` — назначить исполнителей.
- `PATCH /api/tasks/{task_id}/priority` — приоритет; `/budget` — бюджет.

## Запись

- `POST /api/entities/{entity_id}/tasks` JSON:
  `{"task_name","task_type","bid","status","comment"}`.
  - `task_type` должен быть из `task_types` проекта, иначе 422
    `Invalid task_type for shot`.
  - при 409 с тем же именем переиспользуй существующую задачу.
- `POST /api/tasks/{task_id}/assignments` JSON `{"user_ids": [...]}`.
- `PATCH /api/tasks/{task_id}/status` JSON `{"status": "wip|done|..."}`.
- `POST /api/tasks/{task_id}/comments` form `body`.
- `POST /api/comments/{comment_id}` → `PATCH`/`DELETE` для правки/удаления.
- `POST /api/tasks/{task_id}/publications` multipart:
  `files[]`, `comment`, `minutes`, `hours`, `note`.
  Это файлы-публикации задачи (не версия): удобно для PDF, референсов и
  рабочих материалов, которые должны быть видны в задаче.
- `POST /api/entities/{entity_id}/versions` multipart:
  `file`, `task_id`, `comment`, `minutes`, `hours`, `note`, `comment_image`, `author`.
  Это создаёт новую версию (видео/картинка/PDF).

## Боты, скилы и назначение

Пароли ботов — в локальном `bots.json` (настройки программы). Скилы берутся из
synx: `GET /api/tasks/{task_id}/assignments-form` → `short_name`, `full_name`,
`skills` (например `Render, AI, Pipeline`). Для задачи выбирай включённого бота,
у которого есть пароль и который есть в assignments-form; назначай его
user_id. Публикуй версию от имени этого бота.

## Рабочие файлы задачи

Все рабочие файлы генерации (PNG/JPG, видео, workflow JSON, PDF, meta, README)
должны лежать под задачей, в папке назначенного бота:

```text
<projects_root>/<project>/shots/<sequence>/<shot>/<task_type_lower>/work/<bot>/<task_name>_vXXX/
```

Для asset-сущностей:

```text
<projects_root>/<project>/assets/<asset>/<task_type_lower>/work/<bot>/<task_name>_vXXX/
```

Финальные изображения/видео/PDF дополнительно копируются в
`.../<shot>/render/vXXX/`. Не складывай рабочие файлы в корень проекта,
`render/` без `vXXX`, `data/chat` или произвольные папки. Превью чата может
временно лежать в `data/chat`, но канонические файлы задачи — только в папке
задачи/бота.

## PDF

- Собирай многостраничный PDF: одна картинка на страницу, стандартная
  страница A4 landscape (842×595 pt), поля 8–10 мм, белый фон.
- Всегда проверяй PDF перед публикацией: число страниц = числу картинок,
  стандартный размер страницы, нет пустых страниц.
- После публикации проверяй `media.kind` версии. Если он `null`, synx не
  распознал PDF для этой сущности/типа задачи: опубликуй preview-версию
  (контактный лист/слайдшоу) или используй PDF-friendly задачу/asset, а PDF
  оставь файлом задачи/публикацией.
- Если PDF уже опубликован пустым/плохим — пересобери его из кадров задачи,
  проверь и опубликуй новую версию.
- Для просмотра PDF используй кэш страниц: `pdf_render_pages` (параллельный
  рендер в PNG), `POST /api/pdf/render` (`path`, `dpi`, `workers`),
  `GET /api/pdf/render/{job_id}`, `GET /api/pdf/page/{job_id}/{page}`. UI
  показывает готовые страницы, не дожидаясь полной загрузки PDF.

Daily PDF (базовый навык):

- первая страница — все кадры мелко (контактный лист);
- далее по одному кадру на страницу, фон чёрный, кадр по ширине листа,
  боковые края совпадают с границами страницы;
- под кадром серым текстом: Prompt EN/RU, модели и LoRA, время генерации,
  PC/GPU/VRAM/CPU/RAM/SWAP, стоимость API, длина видео, разрешение;
  белых букв нет: максимум 60% белого, основной текст 40%;
- те же данные — в комментарий публикуемой версии;
- A4 landscape 842×595 pt; проверять число страниц и пустые страницы;
  при 300 dpi использовать две колонки текста, чтобы он читался.
- Если пользователь ссылается на версию («версия 13»), это базовая версия:
  пересобирай layout из кадров задачи, не генерируй изображения заново.
  Параметры: `base_version`, `dpi=300` при «300 dpi», `text_scale=0.8` при
  «текст на 20% меньше», `image_valign=center` при «по центру/по вертикали»,
  `background=#000000` для чёрных страниц.

## ComfyUI MCP

Включён `comfyui-mcp` (artokun/comfyui-mcp, 41 инструмент):

- `create_workflow` — create/modify/validate workflow JSON по шаблонам
  (txt2img, img2img, upscale, inpaint, controlnet, ip_adapter, ltx_video);
- `download_model` — search HuggingFace/CivitAI, download_model/download_civitai,
  target_subfolder `checkpoints`, `loras`, `vae`, `diffusion_models`, ...;
- `list_local_models` — установленные модели и LoRA;
- `search_custom_nodes` / `install_custom_node`;
- `get_workflow` / `save_workflow` / `visualize_workflow`.

Вызов: `mcp_list_tools` (server=comfyui-mcp), затем `mcp_call` с tool и
arguments; для схемы — `mcp_call` tool=describe_tool. Это лучший вариант для
генерации воркфлоу и скачивания моделей/LoRA. Альтернативы: официальный
`Comfy-Org/comfy-mcp` (локальный контроль и download по URL, без CivitAI-поиска)
и `ConstantineB6/comfy-pilot` (редактирование live-графа, без загрузки моделей).

## Веб-поиск (DuckDuckGo MCP)

Включён `ddg-search` (nickclyde/duckduckgo-mcp-server). Инструменты:

- `web_search` — поиск DuckDuckGo, `max_results`, `fetch_top`;
- `web_fetch` — `fetch_content` страницы в text/markdown;
- напрямую через MCP: `search`, `fetch_content`, `expand_link`.

Используй для вопросов про ComfyUI/LoRA/ноды/ошибки/воркфлоу: сначала
`web_search`, затем `web_fetch` лучшего URL, затем ответ с конкретными шагами
и ссылками. Для LoRA: `Load LoRA` (model, clip, lora_name, strength_model,
strength_clip), цепочка нескольких Load LoRA, папка `ComfyUI/models/loras`,
совместимость LoRA с базовой моделью.

## ffmpeg

ffmpeg/ffprobe установлены. MCP-инструменты включённых серверов вызываются
через `mcp_list_tools` / `mcp_call` (stdio-рантайм реализован). `ffmpeg-skill`
даёт probe/cut/fit/caption/graphics/render; `comfyui-mcp` — workflow и модели.
Для PDF проще прямой PIL-пайплайн, для видео можно использовать и `ffmpeg`,
и `ffmpeg-skill`.

## Формат плейлиста Synx Player

```text
<start synx playlist:
{
  "synx_playlist": 1,
  "items": [
    {
      "version": "v003",
      "file": "c004_0020_prod_gen_v003.mp4",
      "sf": 1,
      "ef": 230,
      "duration": 230,
      "fps": 25,
      "url": "https://synks.ru/files/...",
      "checked": true,
      "original_name": "c004_0020_prod_gen_v003.mp4",
      "movie_url": "https://synks.ru/files/...",
      "movie_attachment": {"name": "c004_0020_prod_gen_v003.mp4"},
      "version_id": "....",
      "entity": "c004_0020",
      "project_id": "...."
    }
  ]
}
end synx playlist>
```

Правила: `items` — по одному на версию; `sf=1`; `ef=media.frames`;
`duration=media.frames`; `fps=media.fps` или `project.fps`; `url`/`movie_url` —
абсолютный `base_url + file_url`; `checked: true`.
