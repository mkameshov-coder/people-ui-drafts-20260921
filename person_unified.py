#!/usr/bin/env python3
"""Пилот «единое окно человека» для Базы диалогов.

Виртуальная лента: сообщения остаются в своих chat_id, человек видит их
одним хронологическим полотном. Физического слияния (chat_alias) здесь нет.

Отвязка удаляет строку person_chats и пишет исключение. Следующий прогон
identity.py --apply это исключение читает и не вставляет чат обратно.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

MSK = ZoneInfo("Europe/Moscow")
HERE = Path(__file__).resolve().parent
DEFAULT_ALLOWLIST = HERE / "person_pilot_allowlist.json"

EXCLUSION_DDL = """
CREATE TABLE IF NOT EXISTS person_chat_exclusions (
    chat_id INTEGER NOT NULL,
    person_name_key TEXT NOT NULL,
    person_id INTEGER,
    reason TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (chat_id, person_name_key)
);
CREATE TABLE IF NOT EXISTS person_chat_exclusion_peers (
    chat_id INTEGER NOT NULL,
    peer_chat_id INTEGER NOT NULL,
    PRIMARY KEY (chat_id, peer_chat_id)
);
"""

CHANNEL_LABELS = {
    "whatsapp": "WhatsApp",
    "max": "MAX",
    "maxgroup": "MAX",
    "telegram": "Telegram",
    "telegramgroup": "Telegram",
    "instagram": "Instagram",
    "avito": "Avito",
}

LINK_GLOSS = {
    "title": "по названию",
    "phone": "по телефону",
    "pilot": "вручную, пилот",
    "auto": "авто",
}


def norm_name(value) -> str:
    """Тот же ключ, что identity.norm_title: регистр, пробелы, ё.

    Держим копию здесь, чтобы отвязка и автосборка сравнивали имена одинаково,
    даже если identity.py в этот момент не импортируется.
    """
    import re
    text = re.sub(r"\s+", " ", str(value or "").strip()).lower()
    return text.replace("ё", "е")




# --- smart search (pilot list; later API q= can reuse) ---
_RU2LAT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}
# longest multi-letter latin digraphs first for reverse map
_LAT2RU_MULTI = [
    ("sch", "щ"), ("zh", "ж"), ("kh", "х"), ("ts", "ц"), ("ch", "ч"),
    ("sh", "ш"), ("yu", "ю"), ("ya", "я"), ("yo", "ё"),
]
_LAT2RU_ONE = {
    "a": "а", "b": "б", "v": "в", "g": "г", "d": "д", "e": "е",
    "z": "з", "i": "и", "y": "й", "k": "к", "l": "л", "m": "м",
    "n": "н", "o": "о", "p": "п", "r": "р", "s": "с", "t": "т",
    "u": "у", "f": "ф", "h": "х", "c": "к", "j": "й", "w": "в",
    "x": "кс", "q": "к",
}


def fold_yo(value: str) -> str:
    return (value or "").replace("ё", "е").replace("Ё", "Е")


def normalize_search(value) -> str:
    """Lowercase, fold ё, strip punctuation/spaces noise for substring match."""
    import re as _re
    text = fold_yo(str(value or "")).lower().strip()
    text = _re.sub(r"[\s_\-.,;:!?\"'`()\[\]{}/\\|+*=@#$%^&~]+", " ", text)
    return _re.sub(r"\s+", " ", text).strip()


def translit_ru_to_lat(value: str) -> str:
    out = []
    for ch in fold_yo(value or "").lower():
        out.append(_RU2LAT.get(ch, ch))
    return "".join(out)


def translit_lat_to_ru(value: str) -> str:
    s = fold_yo(value or "").lower()
    out = []
    i = 0
    while i < len(s):
        hit = None
        for lat, ru in _LAT2RU_MULTI:
            if s.startswith(lat, i):
                hit = ru
                i += len(lat)
                break
        if hit is not None:
            out.append(hit)
            continue
        ch = s[i]
        out.append(_LAT2RU_ONE.get(ch, ch))
        i += 1
    return "".join(out)


def search_variants(value: str) -> set:
    """Normalized forms of a string for ru↔lat matching."""
    base = normalize_search(value)
    if not base:
        return set()
    variants = {base, translit_ru_to_lat(base), translit_lat_to_ru(base)}
    # also translit of already-lat / already-ru
    variants.add(normalize_search(translit_ru_to_lat(base)))
    variants.add(normalize_search(translit_lat_to_ru(base)))
    return {v for v in variants if v}


def search_match(needle, *haystacks) -> bool:
    """True if needle matches any haystack (name/db_name/slug) with translit."""
    n = normalize_search(needle)
    if not n:
        return True
    n_vars = search_variants(n)
    for hay in haystacks:
        h_vars = search_variants(hay)
        for nv in n_vars:
            for hv in h_vars:
                if nv in hv or hv in nv:
                    return True
    return False

def to_msk(value) -> str:
    """Время сообщения в Europe/Moscow, без суффикса зоны, до секунды.

    В архиве date хранится как ISO с +00:00 (иногда с долями секунды).
    Наивная строка без зоны читается как UTC: так лежат старые импорты.
    """
    if not value:
        return ""
    raw = str(value).strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError:
        return ""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(MSK).strftime("%Y-%m-%dT%H:%M:%S")


def source_label(source, channel) -> str:
    source = source or ""
    channel = channel or ""
    if channel in CHANNEL_LABELS:
        return CHANNEL_LABELS[channel]
    if source == "max":
        return "MAX"
    if source in ("whatsapp", "whatsapp_export", "wa_cache"):
        return "WhatsApp"
    if source in ("personal_tg", "tg_manager"):
        return "Telegram"
    if source == "avito":
        return "Avito"
    if source in ("megafon1", "megafon2"):
        return "Звонок"
    if source == "wazzup":
        return "Wazzup"
    return source or channel or "канал"


def merge_sources_by_label(items: list) -> list:
    """Склеить куски с одной подписью (Telegram+Telegram → один Telegram).

    В архиве одна линия часто режется по разным source/channel
    (пустой channel → потом telegram; wa_cache → whatsapp). В витрине
    это выглядело как два Telegram / два WhatsApp. Суммируем по label.
    """
    parts_by_label = {}
    order = []
    for item in items or []:
        label = item.get("label") or "канал"
        if label not in parts_by_label:
            parts_by_label[label] = []
            order.append(label)
        parts_by_label[label].append(item)
    out = []
    for label in order:
        parts = parts_by_label[label]
        top = max(parts, key=lambda row: int(row.get("n") or 0))
        out.append({
            "source": top.get("source") or "",
            "channel": top.get("channel") or "",
            "label": label,
            "n": sum(int(row.get("n") or 0) for row in parts),
        })
    out.sort(key=lambda item: -int(item["n"]))
    return out


def ensure_schema(con: sqlite3.Connection) -> None:
    """Создать таблицы отвязки.

    По одной инструкции, без executescript: тот делает COMMIT до скрипта.
    Сам CREATE в SQLite всё равно фиксируется сразу, поэтому схему зовём
    только из записи (--apply и отвязка), не из сухого прогона.
    """
    for statement in EXCLUSION_DDL.split(";"):
        sql = statement.strip()
        if sql:
            con.execute(sql)


def load_allowlist(path=None) -> dict:
    file = Path(path) if path else DEFAULT_ALLOWLIST
    data = json.loads(file.read_text(encoding="utf-8"))
    if "people" not in data:
        raise ValueError("в списке пилота нет people")
    return data


def _one(con, sql, params=()):
    return con.execute(sql, params).fetchone()


def resolve_person_id(con: sqlite3.Connection, spec: dict):
    """person_id из списка, если запись жива. Иначе ищем по чатам и имени."""
    pid = spec.get("person_id")
    if pid is not None:
        row = _one(con, "SELECT id FROM persons WHERE id=?", (int(pid),))
        if row:
            return int(row[0])
        return None
    chats = [int(c) for c in spec.get("chats") or []]
    if chats:
        marks = ",".join("?" * len(chats))
        row = _one(
            con,
            f"""SELECT person_id FROM person_chats
                WHERE chat_id IN ({marks})
                GROUP BY person_id ORDER BY count(*) DESC LIMIT 1""",
            chats,
        )
        if row:
            return int(row[0])
    for name in (spec.get("db_name"), spec.get("display_name")):
        if not name:
            continue
        row = _one(con, "SELECT id FROM persons WHERE name=?", (name,))
        if row:
            return int(row[0])
    return None


def pilot_spec(con, person_id, allowlist) -> dict | None:
    for spec in allowlist.get("people") or []:
        resolved = resolve_person_id(con, spec)
        if resolved is not None and int(resolved) == int(person_id):
            return spec
    return None


def _message_stats(con, chat_ids):
    """Counts, per-chat source buckets, and last message ISO date per chat."""
    if not chat_ids:
        return {}, {}, {}
    marks = ",".join("?" * len(chat_ids))
    counts = {
        int(r[0]): int(r[1])
        for r in con.execute(
            f"SELECT chat_id, count(*) FROM messages WHERE chat_id IN ({marks}) GROUP BY chat_id",
            chat_ids,
        )
    }
    lasts = {
        int(r[0]): (r[1] or "")
        for r in con.execute(
            f"SELECT chat_id, max(date) FROM messages WHERE chat_id IN ({marks}) GROUP BY chat_id",
            chat_ids,
        )
    }
    sources = {}
    for row in con.execute(
        f"""SELECT chat_id, source, channel, count(*) n
            FROM messages WHERE chat_id IN ({marks})
            GROUP BY chat_id, source, channel""",
        chat_ids,
    ):
        sources.setdefault(int(row[0]), []).append({
            "source": row[1] or "",
            "channel": row[2] or "",
            "label": source_label(row[1], row[2]),
            "n": int(row[3]),
        })
    for cid, bucket in list(sources.items()):
        sources[cid] = merge_sources_by_label(bucket)
    return counts, sources, lasts


def list_pilot(con: sqlite3.Connection, allowlist: dict, q: str | None = None) -> dict:
    """Пилотный список людей. Сортировка: last_message_at desc (без даты — в конце).

    q — опциональный фильтр (display_name / db_name / slug, ru↔lat). Для пилота
    фильтр можно делать и на клиенте; параметр готов к будущему API ?q=.
    """
    people = []
    for spec in allowlist.get("people") or []:
        pid = resolve_person_id(con, spec)
        status = "ready"
        db_name = ""
        if spec.get("person_id") is not None and pid is None:
            status = "missing_person"
        elif pid is None and spec.get("create"):
            status = "pending_seed"
        elif pid is None:
            status = "pending_seed"
        if pid is not None:
            row = _one(con, "SELECT name FROM persons WHERE id=?", (pid,))
            db_name = row[0] if row else ""
        chats = [int(c) for c in spec.get("chats") or []]
        linked = []
        if pid is not None:
            linked = [
                int(r[0]) for r in con.execute(
                    "SELECT chat_id FROM person_chats WHERE person_id=?", (pid,)
                )
            ]
        counts, _sources, lasts = _message_stats(con, linked)
        last_message_at = max(lasts.values()) if lasts else None
        if last_message_at == "":
            last_message_at = None
        people.append({
            "slug": spec.get("slug"),
            "person_id": pid,
            "display_name": spec.get("display_name") or db_name,
            "db_name": db_name,
            "status": status,
            "channel_count": len(linked),
            "message_count": sum(counts.values()),
            "last_message_at": last_message_at,
            "missing_chats": [c for c in chats if c not in set(linked)],
        })
    # ISO-строки сравнимы лексикографически; пустые — в конец при reverse
    people.sort(key=lambda p: p.get("last_message_at") or "", reverse=True)
    if q:
        people = [
            p for p in people
            if search_match(q, p.get("display_name"), p.get("db_name"), p.get("slug"))
        ]
    return {"label": allowlist.get("label") or "Люди · пилот", "people": people}


def showcase(con: sqlite3.Connection, person_id: int, allowlist: dict) -> dict:
    spec = pilot_spec(con, person_id, allowlist)
    if spec is None:
        return {"ok": False, "error": "этот человек не в пилоте"}
    person = _one(con, "SELECT id, name, note FROM persons WHERE id=?", (int(person_id),))
    if person is None:
        return {"ok": False, "error": "человек не найден"}
    linked_rows = list(con.execute(
        """SELECT pc.chat_id, pc.link_source, pc.confirmed, ch.title, ch.type
           FROM person_chats pc
           LEFT JOIN chats ch ON ch.chat_id = pc.chat_id
           WHERE pc.person_id=?
           ORDER BY pc.chat_id""",
        (int(person_id),),
    ))
    by_chat = {int(r[0]): r for r in linked_rows}
    order = []
    for cid in spec.get("chats") or []:
        cid = int(cid)
        if cid not in order:
            order.append(cid)
    for cid in by_chat:
        if cid not in order:
            order.append(cid)
    counts, sources, _lasts = _message_stats(con, order)
    channels = []
    for cid in order:
        row = by_chat.get(cid)
        title = ""
        ctype = ""
        if row is not None:
            title = row[3] or ""
            ctype = row[4] or ""
        else:
            chat = _one(con, "SELECT title, type FROM chats WHERE chat_id=?", (cid,))
            if chat:
                title, ctype = chat[0] or "", chat[1] or ""
        link_source = row[1] if row is not None else ""
        channels.append({
            "chat_id": cid,
            "title": title,
            "type": ctype,
            "sources": sources.get(cid, []),
            "message_count": counts.get(cid, 0),
            "link_source": link_source,
            "link_gloss": LINK_GLOSS.get(link_source, link_source),
            "confirmed": int(row[2]) if row is not None and row[2] is not None else 0,
            "linked": row is not None,
        })
    channels.sort(key=lambda item: (-item["message_count"], item["chat_id"]))
    return {
        "ok": True,
        "person_id": int(person_id),
        "display_name": spec.get("display_name") or person[1],
        "db_name": person[1],
        "note": person[2] or "",
        "slug": spec.get("slug"),
        "channels": channels,
        "message_count": sum(item["message_count"] for item in channels if item["linked"]),
    }


def person_feed(con: sqlite3.Connection, person_id: int, allowlist: dict, *,
                 limit: int = 80, before_date=None, before_chat=None,
                 before_msg=None, me_id: int = 790590637) -> dict:
    """Хронологическая страница по всем чатам человека. Новые сообщения в конце."""
    spec = pilot_spec(con, person_id, allowlist)
    if spec is None:
        return {"ok": False, "error": "этот человек не в пилоте"}
    limit = max(1, min(int(limit or 80), 500))
    chat_rows = list(con.execute(
        "SELECT chat_id FROM person_chats WHERE person_id=?", (int(person_id),)
    ))
    chat_ids = [int(r[0]) for r in chat_rows]
    if not chat_ids:
        return {"ok": True, "person_id": int(person_id), "messages": [], "exhausted": True}
    marks = ",".join("?" * len(chat_ids))
    params = list(chat_ids)
    where = f"m.chat_id IN ({marks})"
    if before_date:
        if before_chat is None or before_msg is None:
            return {"ok": False, "error": "для курсора нужны before, before_chat и before_msg"}
        where += """ AND (
            m.date < ?
            OR (m.date = ? AND m.chat_id < ?)
            OR (m.date = ? AND m.chat_id = ? AND m.msg_id < ?)
        )"""
        params.extend([
            before_date, before_date, int(before_chat),
            before_date, int(before_chat), int(before_msg),
        ])
    sql = f"""
        SELECT m.chat_id, m.msg_id, m.date, m.sender_id, m.sender_name, m.kind,
               m.text, m.is_voice, m.voice_duration, m.ai_caption, m.media_name,
               m.call_type, m.source, m.channel, m.voice_file, m.media_file,
               ch.title AS chat_title
        FROM messages m
        LEFT JOIN chats ch ON ch.chat_id = m.chat_id
        WHERE {where}
        ORDER BY m.date DESC, m.chat_id DESC, m.msg_id DESC
        LIMIT ?
    """
    params.append(limit + 1)
    rows = list(con.execute(sql, params))
    exhausted = len(rows) <= limit
    rows = rows[:limit]
    rows.reverse()
    messages = []
    for row in rows:
        source = row[12] or ""
        channel = row[13] or ""
        messages.append({
            "chat_id": int(row[0]),
            "msg_id": int(row[1]),
            "date": row[2] or "",
            "date_msk": to_msk(row[2]),
            "sender": row[4] or "",
            "is_me": row[3] == me_id,
            "kind": row[5] or "",
            "text": row[6] or "",
            "is_voice": row[7] or 0,
            "dur": row[8],
            "cap": row[9] or "",
            "fname": row[10] or "",
            "ctype": row[11] or "",
            "src": source,
            "channel": channel,
            "chip": source_label(source, channel),
            "chat_title": row[16] or "",
            "has_media": 1 if (row[14] or row[15]) else 0,
        })
    return {
        "ok": True,
        "person_id": int(person_id),
        "messages": messages,
        "exhausted": exhausted,
    }


def _exclusion_keys(con, person_id, chat_id, spec) -> set:
    person = _one(con, "SELECT name FROM persons WHERE id=?", (int(person_id),))
    keys = set()
    if person and person[0]:
        keys.add(norm_name(person[0]))
    if spec and spec.get("display_name"):
        keys.add(norm_name(spec["display_name"]))
    own = _one(con, "SELECT title FROM chats WHERE chat_id=?", (int(chat_id),))
    if own and own[0]:
        keys.add(norm_name(own[0]))
    peers = con.execute(
        """SELECT ch.title FROM person_chats pc
           LEFT JOIN chats ch ON ch.chat_id = pc.chat_id
           WHERE pc.person_id=? AND pc.chat_id<>?""",
        (int(person_id), int(chat_id)),
    )
    for row in peers:
        if row[0]:
            keys.add(norm_name(row[0]))
    keys.discard("")
    return keys


def unlink_chat(con: sqlite3.Connection, person_id: int, chat_id: int, *,
                reason: str = "", allowlist: dict) -> dict:
    """Снять диалог с человека. Сообщения не трогаем."""
    ensure_schema(con)
    spec = pilot_spec(con, person_id, allowlist)
    if spec is None:
        return {"ok": False, "error": "этот человек не в пилоте"}
    link = _one(
        con,
        "SELECT link_source FROM person_chats WHERE person_id=? AND chat_id=?",
        (int(person_id), int(chat_id)),
    )
    if link is None:
        return {"ok": False, "error": "диалог не привязан к этому человеку"}
    before = _one(con, "SELECT count(*) FROM messages WHERE chat_id=?", (int(chat_id),))[0]
    keys = _exclusion_keys(con, person_id, chat_id, spec)
    peers = [
        int(r[0]) for r in con.execute(
            "SELECT chat_id FROM person_chats WHERE person_id=? AND chat_id<>?",
            (int(person_id), int(chat_id)),
        )
    ]
    now = datetime.now(timezone.utc).isoformat()
    why = (reason or "").strip() or "отвязано в пилоте"
    for key in keys:
        con.execute(
            """INSERT OR REPLACE INTO person_chat_exclusions
               (chat_id, person_name_key, person_id, reason, created_at)
               VALUES (?,?,?,?,?)""",
            (int(chat_id), key, int(person_id), why, now),
        )
    for peer in peers:
        con.execute(
            """INSERT OR REPLACE INTO person_chat_exclusion_peers
               (chat_id, peer_chat_id) VALUES (?,?)""",
            (int(chat_id), peer),
        )
    con.execute(
        "DELETE FROM person_chats WHERE person_id=? AND chat_id=?",
        (int(person_id), int(chat_id)),
    )
    con.commit()
    after = _one(con, "SELECT count(*) FROM messages WHERE chat_id=?", (int(chat_id),))[0]
    return {
        "ok": True,
        "person_id": int(person_id),
        "chat_id": int(chat_id),
        "messages_kept": int(after),
        "messages_before": int(before),
    }


def _table_exists(con, name) -> bool:
    row = _one(
        con,
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    )
    return row is not None


def is_blocked(con: sqlite3.Connection, chat_id: int, person_name: str,
               cluster_ids) -> bool:
    """True, если этот чат человек уже отвязал от такой личности."""
    if not _table_exists(con, "person_chat_exclusions"):
        return False
    key = norm_name(person_name)
    if key:
        hit = _one(
            con,
            """SELECT 1 FROM person_chat_exclusions
               WHERE chat_id=? AND person_name_key=?""",
            (int(chat_id), key),
        )
        if hit:
            return True
    ids = [int(c) for c in cluster_ids if int(c) != int(chat_id)]
    if not ids or not _table_exists(con, "person_chat_exclusion_peers"):
        return False
    marks = ",".join("?" * len(ids))
    hit = _one(
        con,
        f"""SELECT 1 FROM person_chat_exclusion_peers
            WHERE chat_id=? AND peer_chat_id IN ({marks}) LIMIT 1""",
        (int(chat_id), *ids),
    )
    return hit is not None


def without_excluded(con: sqlite3.Connection, person_name: str, chat_ids) -> list:
    """Список чатов кластера без тех, что отвязали вручную.

    identity.py зовёт это перед INSERT в person_chats. Таблицы ещё нет —
    возвращаем кластер как есть, автосборка не падает.
    """
    if not _table_exists(con, "person_chat_exclusions"):
        return list(chat_ids)
    return [cid for cid in chat_ids if not is_blocked(con, cid, person_name, chat_ids)]


def blocked_for_seed(con: sqlite3.Connection, chat_id: int, person_id: int,
                     person_name: str) -> bool:
    if not _table_exists(con, "person_chat_exclusions"):
        return False
    key = norm_name(person_name)
    hit = _one(
        con,
        """SELECT 1 FROM person_chat_exclusions
           WHERE chat_id=? AND (person_name_key=? OR person_id=?)""",
        (int(chat_id), key, int(person_id)),
    )
    return hit is not None


def register(app, auth, db, me_id: int = 790590637, allowlist_path=None):
    """Повесить страницу пилота и JSON на существующее Flask-приложение витрины.

    Классические /people и /api/people* — под HTTP Basic Auth.
    Зеркало для панели управления: /dash-people/<PEOPLE_DASH_TOKEN>/… без Basic Auth,
    доступ только по секрету в пути (как у objects-* / access-*).
    """
    import hmac
    import os

    from flask import Response, jsonify, request

    def allowlist():
        return load_allowlist(allowlist_path)

    def people_dash_token() -> str:
        return (os.environ.get("PEOPLE_DASH_TOKEN") or "").strip()

    def token_ok(token: str) -> bool:
        expected = people_dash_token()
        if not expected or not token:
            return False
        return hmac.compare_digest(str(token), expected)

    def page_html(base: str = "") -> str:
        html = PAGE
        # data-base на body: JS строит API/media относительно него.
        # base — наш токен-путь (без пользовательского HTML).
        marker = "<body>"
        if marker in html:
            safe = str(base).replace('"', "")
            html = html.replace(marker, f'<body data-base="{safe}">', 1)
        return html

    def _list_people():
        con = db()
        try:
            q = (request.args.get("q") or "").strip() or None
            return jsonify(list_pilot(con, allowlist(), q=q))
        finally:
            con.close()

    def _person(person_id):
        con = db()
        try:
            payload = showcase(con, person_id, allowlist())
        finally:
            con.close()
        code = 200 if payload.get("ok") else 404
        return jsonify(payload), code

    def _person_messages(person_id):
        con = db()
        try:
            payload = person_feed(
                con, person_id, allowlist(),
                limit=int(request.args.get("limit", 80)),
                before_date=request.args.get("before") or None,
                before_chat=request.args.get("before_chat") or None,
                before_msg=request.args.get("before_msg") or None,
                me_id=me_id,
            )
        finally:
            con.close()
        code = 200 if payload.get("ok") else 404
        return jsonify(payload), code

    def _person_unlink(person_id):
        body = request.get_json(silent=True) or {}
        try:
            chat_id = int(body.get("chat_id"))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "нужен chat_id"}), 400
        con = db()
        try:
            payload = unlink_chat(
                con, person_id, chat_id,
                reason=body.get("reason") or "",
                allowlist=allowlist(),
            )
        finally:
            con.close()
        if payload.get("ok"):
            return jsonify(payload)
        missing = payload.get("error") == "этот человек не в пилоте"
        absent = payload.get("error") == "диалог не привязан к этому человеку"
        code = 404 if missing or absent else 400
        return jsonify(payload), code

    def _forbidden():
        return jsonify({"ok": False, "error": "forbidden"}), 403

    def _dash_media():
        """Тот же /media, но без Basic Auth — только с верным path-токеном."""
        import mimetypes
        import os as _os

        from flask import redirect, send_file

        c = db()
        try:
            cid = int(request.args["c"])
            mid = int(request.args["m"])
            r = c.execute(
                "SELECT kind,voice_file,media_file,media_name FROM messages "
                "WHERE chat_id=? AND msg_id=?",
                (cid, mid),
            ).fetchone()
            if not r:
                return Response("not found", 404)
            # sqlite Row или tuple
            def cell(row, key, idx):
                try:
                    return row[key]
                except (KeyError, IndexError, TypeError):
                    return row[idx]

            path = cell(r, "voice_file", 1) or cell(r, "media_file", 2) or ""
            if not path:
                return Response("no file", 404)
            mt = mimetypes.guess_type(path)[0] or "application/octet-stream"
            dl = bool(request.args.get("dl"))
            name = cell(r, "media_name", 3) or _os.path.basename(path)
            if _os.path.exists(path):
                return send_file(
                    path, mimetype=mt, as_attachment=dl,
                    download_name=name, conditional=True,
                )
            import sys as _sys
            if "scripts" not in _sys.path:
                _sys.path.insert(0, "scripts")
            import media_store
            import s3util

            catrow = c.execute(
                "SELECT category FROM chats WHERE chat_id=?", (cid,)
            ).fetchone()
            cat = None
            if catrow is not None:
                try:
                    cat = catrow["category"]
                except (KeyError, IndexError, TypeError):
                    cat = catrow[0]
            key = media_store.key_of(cid, cat, path)
            if not s3util.exists(key):
                return Response("no file", 404)
            return redirect(
                s3util.presign(key, download_name=(name if dl else None)),
                code=302,
            )
        finally:
            c.close()

    @app.route("/people")
    @auth
    def people_page():
        return Response(page_html(""), mimetype="text/html; charset=utf-8")

    @app.route("/api/people")
    @auth
    def api_people():
        return _list_people()

    @app.route("/api/people/<int:person_id>")
    @auth
    def api_person(person_id):
        return _person(person_id)

    @app.route("/api/people/<int:person_id>/messages")
    @auth
    def api_person_messages(person_id):
        return _person_messages(person_id)

    @app.route("/api/people/<int:person_id>/unlink", methods=["POST"])
    @auth
    def api_person_unlink(person_id):
        return _person_unlink(person_id)

    @app.route("/dash-people/<token>/")
    @app.route("/dash-people/<token>")
    def dash_people_page(token):
        if not token_ok(token):
            return Response("forbidden", 403)
        base = f"/dash-people/{token}"
        return Response(page_html(base), mimetype="text/html; charset=utf-8")

    @app.route("/dash-people/<token>/api/people")
    def dash_api_people(token):
        if not token_ok(token):
            return _forbidden()
        return _list_people()

    @app.route("/dash-people/<token>/api/people/<int:person_id>")
    def dash_api_person(token, person_id):
        if not token_ok(token):
            return _forbidden()
        return _person(person_id)

    @app.route("/dash-people/<token>/api/people/<int:person_id>/messages")
    def dash_api_person_messages(token, person_id):
        if not token_ok(token):
            return _forbidden()
        return _person_messages(person_id)

    @app.route("/dash-people/<token>/api/people/<int:person_id>/unlink", methods=["POST"])
    def dash_api_person_unlink(token, person_id):
        if not token_ok(token):
            return _forbidden()
        return _person_unlink(person_id)

    @app.route("/dash-people/<token>/media")
    def dash_people_media(token):
        if not token_ok(token):
            return Response("forbidden", 403)
        return _dash_media()


PAGE = r'''<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Люди · пилот</title>
<style>
:root{
  --bg:#f7f6f4; --card:#fff; --ink:#1c1a19; --ink2:#57514e;
  --line:#e4dfdb; --accent:#902A35; --accent-soft:#f3e9ea;
  --muted:#efebe8; --tg:#229ED9; --wa:#25D366; --max:#6B4EFF;
  --safe-bottom:env(safe-area-inset-bottom,0px);
  --safe-top:env(safe-area-inset-top,0px);
}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html,body{margin:0;height:100%;background:var(--bg);color:var(--ink);
  font:15px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
button{font:inherit;cursor:pointer}
a{color:var(--accent);text-decoration:none}
#app{display:flex;height:100vh;height:100dvh}
#side{width:300px;flex-shrink:0;background:var(--card);border-right:1px solid var(--line);
  display:flex;flex-direction:column;min-height:0}
#side h1{font-size:17px;margin:0;padding:calc(12px + var(--safe-top)) 14px 4px;font-weight:700}
#side .sub{font-size:12px;color:var(--ink2);padding:0 14px 10px;border-bottom:1px solid var(--line)}
#side .sub a{color:var(--accent)}
.search-wrap{padding:10px 12px;border-bottom:1px solid var(--line);position:sticky;top:0;z-index:5;background:var(--card)}
#search{width:100%;min-height:44px;border:1px solid var(--line);border-radius:12px;
  padding:10px 14px;font-size:16px;background:var(--bg);color:var(--ink);outline:none}
#search:focus{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft)}
#people{overflow:auto;flex:1;-webkit-overflow-scrolling:touch}
.person{display:flex;align-items:center;gap:12px;width:100%;text-align:left;border:0;
  border-bottom:1px solid var(--line);background:transparent;padding:12px 14px;min-height:64px;color:inherit}
.person:hover{background:var(--muted)}
.person.act{background:var(--accent-soft);border-left:3px solid var(--accent)}
.avatar{width:44px;height:44px;border-radius:50%;background:var(--accent-soft);color:var(--accent);
  display:flex;align-items:center;justify-content:center;font-weight:700;font-size:13px;flex-shrink:0}
.person .meta{flex:1;min-width:0}
.person .t{font-weight:650;font-size:16px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.person .s{font-size:12px;color:var(--ink2);margin-top:2px}
.person .wait{color:#a60}
.chev{color:#b0a9a4;font-size:20px;display:none}
#main{flex:1;display:flex;flex-direction:column;min-width:0;background:var(--bg)}
#head{padding:12px 14px;background:var(--card);border-bottom:1px solid var(--line)}
#head h2{margin:0;font-size:18px}
#head .meta{font-size:12px;color:var(--ink2);margin-top:2px}
#channels{display:flex;gap:8px;overflow:auto;padding:10px 14px;background:var(--card);border-bottom:1px solid var(--line)}
.chan{min-width:200px;max-width:280px;border:1px solid var(--line);border-radius:12px;padding:10px 12px;background:var(--bg);font-size:13px}
.chan .title{font-weight:600}
.chan .row{color:var(--ink2);font-size:12px;margin-top:2px}
.chan button,.unlink{margin-top:8px;min-height:44px;border:1px solid #e0c4c8;background:#fff;color:var(--accent);
  border-radius:10px;padding:8px 12px;cursor:pointer;font-size:14px;font-weight:600}
.chan.off{opacity:.55}
#view{flex:1;overflow:auto;padding:14px;-webkit-overflow-scrolling:touch}
.day{text-align:center;font-size:12px;color:#999;margin:14px 0 8px}
.msg{max-width:74%;margin:6px 0;padding:10px 12px;border-radius:12px;background:var(--card);border:1px solid var(--line);white-space:pre-wrap;word-wrap:break-word}
.msg.me{margin-left:auto;background:var(--accent-soft);border-color:#e0c4c8}
.msg .h{font-size:12px;color:var(--accent);font-weight:600}
.chip{font-size:10px;color:#3d4a5c;background:var(--muted);border-radius:4px;padding:1px 5px;margin-left:6px;font-weight:500}
.msg .tm{font-size:11px;color:#aaa;text-align:right;margin-top:3px}
.more{display:block;margin:8px auto;min-height:44px;padding:8px 16px;border:1px solid var(--line);border-radius:10px;background:var(--card);cursor:pointer}
.empty{color:var(--ink2);text-align:center;margin-top:40px}
.err{color:var(--accent);padding:8px 14px;font-size:13px}
.section-title{font-size:12px;color:var(--ink2);font-weight:700;margin:14px 0 8px;text-transform:uppercase;letter-spacing:.04em}
.channel{display:flex;align-items:center;gap:10px;background:var(--card);border:1px solid var(--line);
  border-radius:12px;padding:10px 12px;margin:0 0 8px;min-height:52px}
.ch-dot{width:10px;height:10px;border-radius:50%;flex-shrink:0;background:#bbb}
.ch-dot.Telegram{background:var(--tg)}.ch-dot.WhatsApp{background:var(--wa)}.ch-dot.MAX{background:var(--max)}
.ch-name{flex:1;font-weight:600;min-width:0}.ch-count{color:var(--ink2);font-size:14px;margin-right:6px}
.sheet-backdrop{display:none;position:fixed;inset:0;background:rgba(28,26,25,.35);z-index:40;opacity:0;pointer-events:none;transition:opacity .2s}
.sheet-backdrop.on{opacity:1;pointer-events:auto}
.sheet{display:none;position:fixed;left:0;right:0;bottom:0;z-index:50;
  background:var(--card);border-radius:18px 18px 0 0;max-height:85vh;
  transform:translateY(110%);transition:transform .25s ease;flex-direction:column;
  padding-bottom:var(--safe-bottom);box-shadow:0 -8px 30px rgba(0,0,0,.12)}
.sheet.on{transform:translateY(0)}
.handle{width:40px;height:5px;border-radius:3px;background:#d5cfc9;margin:10px auto 6px;flex-shrink:0}
.sheet-head{padding:4px 14px 10px;border-bottom:1px solid var(--line);display:flex;align-items:center;gap:10px;flex-shrink:0}
.sheet-body{padding:12px 14px;overflow:auto;-webkit-overflow-scrolling:touch;flex:1;min-height:0}
.iconbtn{min-width:44px;min-height:44px;border:0;background:transparent;color:var(--accent);
  display:inline-flex;align-items:center;justify-content:center;border-radius:10px;font-size:18px;font-weight:600}
.sheet .msg{max-width:100%}
@media(max-width:720px){
  #app{flex-direction:column}
  #side{width:100%;border-right:0;height:100%}
  #main{display:none}
  .chev{display:inline}
  .sheet-backdrop,.sheet{display:flex}
  .person.act{border-left:0;background:transparent}
}
@media(min-width:721px){
  .sheet-backdrop,.sheet{display:none!important}
}
</style></head><body>
<div id="app">
 <div id="side">
  <h1 id="label">Люди · пилот</h1>
  <div class="sub">Каналы и одна лента. <a href="/">К чатам</a></div>
  <div class="search-wrap"><input id="search" type="search" placeholder="Найти…" autocomplete="off" enterkeyhint="search"></div>
  <div id="people"></div>
 </div>
 <div id="main">
  <div id="head"><h2>Выберите человека</h2><div class="meta" id="meta"></div></div>
  <div id="channels"></div>
  <div id="err" class="err"></div>
  <div id="view"><div class="empty">Слева список пилота. Справа — каналы и общая лента.</div></div>
 </div>
</div>
<div class="sheet-backdrop" id="backdrop"></div>
<div class="sheet" id="sheet" role="dialog" aria-modal="true">
  <div class="handle" id="handle"></div>
  <div class="sheet-head">
    <div id="sheetAvatar"></div>
    <div style="flex:1;min-width:0">
      <div id="sheetName" style="font-weight:700;font-size:17px"></div>
      <div id="sheetSub" style="color:var(--ink2);font-size:13px"></div>
    </div>
    <button type="button" class="iconbtn" id="closeSheet" aria-label="Закрыть">✕</button>
  </div>
  <div class="sheet-body" id="sheetBody"></div>
</div>
<script>
const BASE=document.body.dataset.base||'';
const esc=s=>String(s||'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let PEOPLE=[], FILTERED=[], CUR=null, QUERY='';

const RU2LAT={а:'a',б:'b',в:'v',г:'g',д:'d',е:'e',ё:'e',ж:'zh',з:'z',и:'i',й:'y',к:'k',л:'l',м:'m',н:'n',о:'o',п:'p',р:'r',с:'s',т:'t',у:'u',ф:'f',х:'h',ц:'ts',ч:'ch',ш:'sh',щ:'sch',ъ:'',ы:'y',ь:'',э:'e',ю:'yu',я:'ya'};
const LAT2RU_MULTI=[['sch','щ'],['zh','ж'],['kh','х'],['ts','ц'],['ch','ч'],['sh','ш'],['yu','ю'],['ya','я'],['yo','ё']];
const LAT2RU_ONE={a:'а',b:'б',v:'в',g:'г',d:'д',e:'е',z:'з',i:'и',y:'й',k:'к',l:'л',m:'м',n:'н',o:'о',p:'п',r:'р',s:'с',t:'т',u:'у',f:'ф',h:'х',c:'к',j:'й',w:'в',x:'кс',q:'к'};
function foldYo(s){return String(s||'').replace(/ё/gi,m=>m==='Ё'?'Е':'е');}
function normalizeSearch(s){
  let t=foldYo(s).toLowerCase().trim();
  t=t.replace(/[\s_\-.,;:!?\"'`()\[\]{}\/\\\\|+*=@#$%^&~]+/g,' ');
  return t.replace(/\s+/g,' ').trim();
}
function translitRuToLat(s){return [...foldYo(s).toLowerCase()].map(ch=>RU2LAT[ch]??ch).join('');}
function translitLatToRu(s){
  s=foldYo(s).toLowerCase(); let out='', i=0;
  while(i<s.length){
    let hit=null;
    for(const [lat,ru] of LAT2RU_MULTI){ if(s.startsWith(lat,i)){ hit=ru; i+=lat.length; break; } }
    if(hit!==null){ out+=hit; continue; }
    out+=LAT2RU_ONE[s[i]]??s[i]; i++;
  }
  return out;
}
function searchVariants(value){
  const base=normalizeSearch(value);
  if(!base) return new Set();
  const v=new Set([base, translitRuToLat(base), translitLatToRu(base)]);
  v.add(normalizeSearch(translitRuToLat(base)));
  v.add(normalizeSearch(translitLatToRu(base)));
  return v;
}
function personMatches(p, q){
  const n=normalizeSearch(q);
  if(!n) return true;
  const nVars=[...searchVariants(n)];
  const hays=[p.display_name,p.db_name,p.slug].filter(Boolean);
  for(const hay of hays){
    const hVars=[...searchVariants(hay)];
    for(const nv of nVars) for(const hv of hVars) if(hv.includes(nv)) return true;
  }
  return false;
}
function isMobile(){ return window.matchMedia('(max-width:720px)').matches; }
function initials(name){
  const parts=String(name||'').trim().split(/\s+/).filter(Boolean);
  if(!parts.length) return '?';
  if(parts.length===1) return parts[0].slice(0,2).toUpperCase();
  return (parts[0][0]+parts[1][0]).toUpperCase();
}
function avatarHTML(name){ return `<div class="avatar">${esc(initials(name))}</div>`; }
function relativeHint(iso){
  if(!iso) return '';
  const raw=String(iso).trim();
  let d;
  try{
    let s=raw.endsWith('Z')?raw.slice(0,-1)+'+00:00':raw;
    d=new Date(s);
    if(isNaN(d)) return '';
  }catch(e){ return ''; }
  const fmt=new Intl.DateTimeFormat('en-CA',{timeZone:'Europe/Moscow',year:'numeric',month:'2-digit',day:'2-digit'});
  const todayStr=fmt.format(new Date());
  const msgStr=fmt.format(d);
  if(msgStr===todayStr) return 'сегодня';
  const yest=new Date(); yest.setDate(yest.getDate()-1);
  if(msgStr===fmt.format(yest)) return 'вчера';
  const parts=msgStr.split('-');
  if(parts.length===3) return parts[2]+'.'+parts[1];
  return msgStr;
}
async function api(url, opt){
  const r=await fetch(BASE+url, opt);
  const data=await r.json().catch(()=>({ok:false,error:'не разобрал ответ'}));
  if(!r.ok) throw new Error(data.error||('ошибка '+r.status));
  return data;
}
function statusText(p){
  if(p.status==='pending_seed') return 'ждёт посева';
  if(p.status==='missing_person') return 'нет записи person_id';
  const base=(p.channel_count||0)+' диал. · '+(p.message_count||0)+' сообщ.';
  const rel=relativeHint(p.last_message_at);
  return rel? base+' · '+rel : base;
}
function applyFilter(){
  FILTERED=!QUERY? PEOPLE.slice() : PEOPLE.filter(p=>personMatches(p, QUERY));
  renderPeople();
}
function renderPeople(){
  const list=FILTERED;
  document.getElementById('people').innerHTML=list.length? list.map(p=>`
  <button type="button" class="person${CUR===p.person_id?' act':''}" data-id="${p.person_id||''}"
    onclick="openPerson(${p.person_id===null?'null':p.person_id})">
   ${avatarHTML(p.display_name)}
   <div class="meta">
    <div class="t">${esc(p.display_name)}</div>
    <div class="s${p.status==='ready'?'':' wait'}">${esc(statusText(p))}</div>
   </div>
   <span class="chev">›</span>
  </button>`).join('') : '<div class="empty" style="margin:20px">Никого не нашли</div>';
}
async function loadPeople(){
  const data=await api('/api/people');
  PEOPLE=data.people||[];
  document.getElementById('label').textContent=data.label||'Люди · пилот';
  applyFilter();
}
function channelsHTML(channels){
  return (channels||[]).map(ch=>`
  <div class="channel${ch.linked?'':' off'}">
   <span class="ch-dot ${esc((ch.sources&&ch.sources[0]&&ch.sources[0].label)||'')}"></span>
   <div class="ch-name">${esc(ch.title||'без названия')}
    <div style="font-size:12px;color:var(--ink2);font-weight:400">${esc(ch.type||'')} · ${(ch.sources||[]).map(s=>esc(s.label)+' '+s.n).join(' · ')||'нет сообщений'}</div>
    <div style="font-size:12px;color:var(--ink2);font-weight:400">${esc(ch.link_gloss||ch.link_source||'не привязан')}</div>
   </div>
   <span class="ch-count">${ch.message_count||0}</span>
   ${ch.linked?`<button type="button" class="unlink" onclick="unlink(${ch.chat_id})">Отвязать</button>`:''}
  </div>`).join('') || '<div class="empty">Нет каналов</div>';
}
function paintDesktop(card, feed){
  document.getElementById('head').innerHTML=`<h2>${esc(card.display_name)}</h2>
    <div class="meta">person_id ${card.person_id}${card.db_name&&card.db_name!==card.display_name?' · в базе: '+esc(card.db_name):''} · ${card.message_count||0} сообщ.</div>`;
  document.getElementById('channels').innerHTML=(card.channels||[]).map(ch=>`
  <div class="chan${ch.linked?'':' off'}">
   <div class="title">${esc(ch.title||'без названия')}</div>
   <div class="row">${esc(ch.type||'')} · chat_id ${ch.chat_id}</div>
   <div class="row">${(ch.sources||[]).map(s=>esc(s.label)+' '+s.n).join(' · ')||'нет сообщений'}</div>
   <div class="row">${ch.message_count} сообщ. · ${esc(ch.link_gloss||ch.link_source||'не привязан')}</div>
   ${ch.linked?`<button onclick="unlink(${ch.chat_id})">Отвязать</button>`:'<div class="row">не в person_chats</div>'}
  </div>`).join('');
  paintFeed(feed, false, document.getElementById('view'));
}
function openSheet(card, feed){
  document.getElementById('sheetAvatar').innerHTML=avatarHTML(card.display_name);
  document.getElementById('sheetName').textContent=card.display_name||'';
  const linked=(card.channels||[]).filter(c=>c.linked).length;
  document.getElementById('sheetSub').textContent=linked+' диал. · '+(card.message_count||0)+' сообщ.';
  const body=document.getElementById('sheetBody');
  body.innerHTML=`<div class="section-title" style="margin-top:4px">Каналы</div>${channelsHTML(card.channels)}
    <div class="section-title">Лента</div><div id="sheetFeed"></div>`;
  paintFeed(feed, false, document.getElementById('sheetFeed'));
  document.getElementById('sheet').classList.add('on');
  document.getElementById('backdrop').classList.add('on');
}
function closeSheet(){
  document.getElementById('sheet').classList.remove('on');
  document.getElementById('backdrop').classList.remove('on');
}
async function openPerson(personId){
  document.getElementById('err').textContent='';
  if(!personId){
    if(isMobile()){
      document.getElementById('sheetAvatar').innerHTML=avatarHTML('?');
      document.getElementById('sheetName').textContent='Человек ещё не заведён';
      document.getElementById('sheetSub').textContent='Запустите person_pilot_seed.py --apply';
      document.getElementById('sheetBody').innerHTML='<div class="empty">В списке пилота есть слот, записи persons ещё нет.</div>';
      document.getElementById('sheet').classList.add('on');
      document.getElementById('backdrop').classList.add('on');
    } else {
      document.getElementById('head').innerHTML='<h2>Человек ещё не заведён</h2><div class="meta">Запустите person_pilot_seed.py --apply</div>';
      document.getElementById('channels').innerHTML='';
      document.getElementById('view').innerHTML='<div class="empty">В списке пилота есть слот, записи persons ещё нет.</div>';
    }
    CUR=null; renderPeople(); return;
  }
  CUR=personId; renderPeople();
  const card=await api('/api/people/'+personId);
  const feed=await api('/api/people/'+personId+'/messages?limit=80');
  if(isMobile()) openSheet(card, feed);
  else { closeSheet(); paintDesktop(card, feed); }
}
function paintFeed(feed, prepend, view){
  view=view||document.getElementById('view');
  const html=renderMsgs(feed.messages||[]);
  if(!prepend){
    const more=(feed.messages&&feed.messages.length&&!feed.exhausted)
      ?'<button class="more" id="earlier" onclick="earlier()">раньше</button>':'';
    view.innerHTML=more+(html||'<div class="empty">В связанных диалогах пусто</div>');
    view.scrollTop=view.scrollHeight;
  } else if(html){
    const first=view.querySelector('[data-d]');
    if(first) first.insertAdjacentHTML('beforebegin', html);
    else view.insertAdjacentHTML('afterbegin', html);
  }
}
function renderMsgs(list){
  const days=new Set(); let h='';
  for(const m of list){
    const day=(m.date_msk||m.date||'').slice(0,10);
    if(day && !days.has(day)){days.add(day);h+=`<div class="day">${day}</div>`;}
    const tm=(m.date_msk||'').slice(11,16)||'';
    const media=`${BASE}/media?c=${m.chat_id}&m=${m.msg_id}`;
    let body='';
    if(m.is_voice || m.kind==='voice'){
      body=esc(m.text||'(голосовое без расшифровки)');
      if(m.has_media) body+=`<br><audio controls preload="none" src="${media}" style="width:240px;height:32px;margin-top:4px"></audio>`;
    } else if(m.kind==='photo'){
      body='📷 '+esc(m.cap||m.text||'фото');
      if(m.text && m.cap) body+='<br>'+esc(m.text);
      if(m.has_media) body+=`<br><a href="${media}" target="_blank"><img src="${media}" style="max-width:240px;border-radius:8px;margin-top:5px"></a>`;
    } else if(m.kind==='video'){
      body='📹 '+esc(m.cap||m.text||'видео');
      if(m.has_media) body+=`<br><a href="${media}" target="_blank">открыть</a>`;
    } else if(m.kind==='document'){
      const fname=m.fname||'файл';
      body='📄 '+esc(fname);
      if(m.has_media) body+=` <a href="${media}&dl=1" style="font-size:11px">⬇ скачать</a>`;
      if(m.cap) body+=`<div style="margin-top:4px;font-size:12px;line-height:1.35;opacity:.92">${esc(m.cap)}</div>`;
      else if(m.text && m.text!==fname) body+=`<div style="margin-top:4px;font-size:12px;opacity:.7">${esc(m.text)}</div>`;
    } else if(m.kind==='call'){
      body='📞 '+esc(m.text||'звонок');
    } else {
      body=esc(m.text||m.cap||m.fname||'');
    }
    h+=`<div class="msg${m.is_me?' me':''}" data-d="${esc(m.date||'')}" data-c="${m.chat_id}" data-m="${m.msg_id}">
     <div class="h">${esc(m.sender||'—')}<span class="chip">${esc(m.chip||m.src||'')}${m.chat_title?' · '+esc(m.chat_title):''}</span></div>
     ${body}
     <div class="tm">${tm?tm+' МСК':''}</div>
    </div>`;
  }
  return h;
}
async function earlier(){
  const root=isMobile()?document.getElementById('sheetFeed'):document.getElementById('view');
  const first=root && root.querySelector('[data-d]');
  if(!first||!CUR) return;
  const u='/api/people/'+CUR+'/messages?limit=80&before='+encodeURIComponent(first.dataset.d)
   +'&before_chat='+first.dataset.c+'&before_msg='+first.dataset.m;
  const feed=await api(u);
  if(!(feed.messages||[]).length){ const b=document.getElementById('earlier'); if(b) b.remove(); return; }
  const old=root.scrollHeight;
  paintFeed(feed, true, root);
  root.scrollTop=root.scrollHeight-old;
  if(feed.exhausted){ const b=document.getElementById('earlier'); if(b) b.remove(); }
}
async function unlink(chatId){
  if(!CUR) return;
  if(!confirm('Отвязать этот диалог от человека? Сообщения останутся в архиве.')) return;
  document.getElementById('err').textContent='';
  try{
    await api('/api/people/'+CUR+'/unlink', {method:'POST', headers:{'Content-Type':'application/json'},
     body: JSON.stringify({chat_id: chatId})});
    await loadPeople();
    await openPerson(CUR);
  }catch(e){ document.getElementById('err').textContent=e.message||String(e); }
}
document.getElementById('search').addEventListener('input', e=>{
  QUERY=e.target.value||'';
  applyFilter();
});
document.getElementById('closeSheet').onclick=closeSheet;
document.getElementById('backdrop').onclick=closeSheet;
let startY=0, dragging=false;
const handle=document.getElementById('handle');
const sheetEl=document.getElementById('sheet');
function onStart(y){dragging=true;startY=y;}
function onMove(y){ if(!dragging) return; const dy=Math.max(0,y-startY); sheetEl.style.transform='translateY('+dy+'px)'; }
function onEnd(y){ if(!dragging) return; dragging=false; sheetEl.style.transform=''; if(y-startY>80) closeSheet(); }
handle.addEventListener('touchstart',e=>onStart(e.touches[0].clientY),{passive:true});
handle.addEventListener('touchmove',e=>onMove(e.touches[0].clientY),{passive:true});
handle.addEventListener('touchend',e=>onEnd(e.changedTouches[0].clientY));
handle.addEventListener('mousedown',e=>onStart(e.clientY));
window.addEventListener('mousemove',e=>{if(dragging) onMove(e.clientY);});
window.addEventListener('mouseup',e=>{if(dragging) onEnd(e.clientY);});
loadPeople().catch(e=>{ document.getElementById('err').textContent=e.message||String(e); });
</script></body></html>
'''
