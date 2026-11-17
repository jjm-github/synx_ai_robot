# Synx API (production: https://synks.ru)

Авторизация: `POST /api/auth/login` JSON `{"short_name": "...", "password": "..."}`.
Сессия — cookie `synx_session`. В приложении используются настройки Synx:
логин/пароль/адрес.

## Чтение

- `GET /api/projects` — проекты: `id`, `alias`, `name`, `fps`, `output_format`.
- `GET /api/projects/{project_id}/entities` — сущности проекта.
- `GET /api/projects/{project_id}/tasks` — все задачи проекта.
- `GET /api/projects/{project_id}/versions` — все версии проекта.
- `GET /api/entities/{entity_id}/tasks` — задачи сущности.
- `GET /api/entities/{entity_id}/versions` — версии сущности.
- `GET /api/tasks/{task_id}/comments` — комментарии задачи.
- `GET /api/versions/{version_id}/comments` — комментарии версии.
- `GET /api/tasks/{task_id}/assignments-form` — список пользователей для назначения.
- `GET /files/{file_id}` — файл версии (видео/картинка/PDF).

Версия содержит: `id`, `entity_id`, `task_id`, `task_name`, `task_type`,
`version_number`, `version_code`, `file_id`, `file_url`, `pretty_url`,
`original_name`, `status`, `comment`, `created_at`, `media` (`kind`, `width`,
`height`, `fps`, `frames`, `duration_secs`, `codec`), `entity_name`,
`entity_kind`, `author`.

## Запись

- `POST /api/entities/{entity_id}/tasks` JSON:
  `{"task_name","task_type","bid","status","comment"}`.
- `POST /api/tasks/{task_id}/assignments` JSON `{"user_ids": [...]}`.
- `PATCH /api/tasks/{task_id}/status` JSON `{"status": "wip|done|..."}`.
- `POST /api/tasks/{task_id}/comments` form `body`.
- `POST /api/entities/{entity_id}/versions` multipart:
  `file`, `task_id`, `comment`, `minutes`, `note`.

## Формат плейлиста Synx Player

Скопированный в буфер плейлист — это текст:

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

Правила:

- `items` — по одному на версию;
- `sf` — первый кадр (обычно 1);
- `ef` — последний кадр, равен `media.frames`;
- `duration` — число кадров;
- `fps` — из `media.fps`, иначе `project.fps`;
- `url` и `movie_url` — абсолютный URL `base_url + file_url`;
- `version_id` — id версии;
- `entity` — имя шота/ассета;
- `project_id` — id проекта;
- `checked: true`.
