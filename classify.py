from __future__ import annotations

import json
import re
from typing import Any

ACTION_RE = re.compile(
    r"(надо|нужн|требует|исправ|передел|перерис|измен|поправ|замен|убрать|удали|"
    r"добав|сделай|сделать|переген|не так|не тот|не та|не те|плохо|ошиб|брак|"
    r"верни|усилить|ослабить|подправ|fix|change|redo|update|replace|remove|add|"
    r"wrong|bad|fix|make|adjust|improve|regenerate)",
    re.I,
)
POSITIVE_RE = re.compile(
    r"(хорошо|норм|нравится|класс|супер|отлично|реалистич|похоже|принято|"
    r"без замечаний|всё ок|все ок|ок\b|ok\b|nice|good|great|approved)",
    re.I,
)
PUBLICATION_RE = re.compile(
    r"(опубликован|публикац|превью|скриншот|storyboard:|^v\d+\b|"
    r"версия|version|h3|wan|flux|файл|кадр|рендер|render)",
    re.I,
)
NOTE_RE = re.compile(r"^(превью|v\d{1,4}\b|версия\s+v\d+|опубликован)", re.I)
TZ_RE = re.compile(r"^\s*Сцена\s+\d|Шот сценария|Блок\s+\d", re.I)
CLIENT_AUTHORS = {"client", "Review", "review-service", "client-review"}
BOT_AUTHORS = {"a.smirnov", "e.matveev", "j.klimova", "m.karpov", "s.smirnov"}
REPLY_RE = re.compile(
    r"^(понял|принял|выпускаю|выпустил|готово|сделал|обновил|учёл|учел|"
    r"беру в работу|взял в работу|версия\s+v\d+|v\d+\b|превью|опубликован)",
    re.I,
)


def _attachments(pub: dict[str, Any]) -> list[dict[str, Any]]:
    a = pub.get("attachments")
    if a is None:
        try:
            a = json.loads(pub.get("attachments_json") or "[]")
        except Exception:
            a = []
    return a or []


def classify_publication(pub: dict[str, Any]) -> dict[str, Any]:
    """Return {needs_action, publication_type, source, reason}."""
    body = (pub.get("body") or "").strip()
    low = body.lower()
    author = str(pub.get("author") or "")
    thread = str(pub.get("thread_kind") or "")
    atts = _attachments(pub)
    if pub.get("answered_by_version"):
        vnum = pub.get("answer_version_number")
        vcode = pub.get("answer_version_code") or (f"v{int(vnum):03d}" if vnum else "")
        return {"needs_action": 0, "publication_type": "version",
                "source": "reply",
                "reason": "по комментарию уже выпущена версия" + (f" {vcode}" if vcode else "")}

    if pub.get("is_mention"):
        return {"needs_action": 1, "publication_type": "continuation",
                "source": "mention",
                "reason": "приоритетное сообщение с упоминанием @p.robot"}

    if NOTE_RE.search(body) and not ACTION_RE.search(body):
        return {"needs_action": 0, "publication_type": "version",
                "source": "rules", "reason": "публикационное сообщение версии"}

    analysis = pub.get("analysis")
    if isinstance(analysis, str):
        try:
            analysis = json.loads(analysis)
        except Exception:
            analysis = None
    if isinstance(analysis, dict) and "needs_task" in analysis:
        needs = 1 if analysis.get("needs_task", True) else 0
        return {"needs_action": needs,
                "publication_type": "continuation" if needs else "version",
                "source": "ai", "reason": analysis.get("summary") or ""}

    if not body and not atts:
        return {"needs_action": None, "publication_type": "unknown",
                "source": "rules", "reason": "empty"}

    if TZ_RE.search(body):
        return {"needs_action": 0, "publication_type": "version",
                "source": "rules", "reason": "ТЗ / постановка"}

    if author in BOT_AUTHORS and REPLY_RE.search(body):
        return {"needs_action": 0, "publication_type": "version",
                "source": "rules", "reason": "ответ/публикация бота"}

    if author in BOT_AUTHORS and thread == "version" and not ACTION_RE.search(body):
        return {"needs_action": 0, "publication_type": "version",
                "source": "rules", "reason": "публикация версии ботом"}

    action = bool(ACTION_RE.search(body))
    positive = bool(POSITIVE_RE.search(body))
    publication = bool(PUBLICATION_RE.search(body)) or thread == "version"
    clientish = author in CLIENT_AUTHORS
    generic = bool(re.search(r"comment with images|комментарий с картин", low))

    if action and not (generic and not body):
        return {"needs_action": 1, "publication_type": "continuation",
                "source": "rules", "reason": "найдены слова-запросы на правку"}

    if positive:
        return {"needs_action": 0, "publication_type": "version",
                "source": "rules", "reason": "положительный отзыв"}

    if (publication or publication and not clientish) and not clientish:
        return {"needs_action": 0, "publication_type": "version",
                "source": "rules", "reason": "публикация версии"}

    if thread == "task" and not clientish:
        return {"needs_action": 0, "publication_type": "version",
                "source": "rules", "reason": "служебная задача/публикация"}

    # Client/review text without an explicit request, or a picture-only review.
    if clientish:
        return {"needs_action": None, "publication_type": "unknown",
                "source": "needs_ai", "reason": "client/review comment needs AI"}

    return {"needs_action": None, "publication_type": "unknown",
            "source": "rules", "reason": "ambiguous"}
