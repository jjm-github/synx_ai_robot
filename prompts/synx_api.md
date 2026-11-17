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

Daily PDF (базовый навык):

- первая страница — все кадры мелко (контактный лист);
- далее по одному кадру на страницу, фон чёрный, кадр по ширине листа,
  боковые края совпадают с границами страницы;
- под кадром серым текстом (белый ~50%): Prompt EN/RU, модели и LoRA,
  время генерации, PC/GPU/VRAM/CPU/RAM/SWAP, стоимость API, длина видео,
  разрешение;
- те же данные — в комментарий публикуемой версии;
- A4 landscape 842×595 pt; проверять число страниц и пустые страницы.

## ffmpeg

ffmpeg/ffprobe установлены и являются базовыми инструментами для видео. Для
нарезки, склейки, кадров, слайдшоу и проверки используй прямые вызовы ffmpeg.
В программе есть управление MCP-конфигами (поиск/установка/список), но
рантайм-вызов MCP-инструментов пока не реализован. Для PDF/ffmpeg это не
требуется — прямые вызовы проще и надёжнее.

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
