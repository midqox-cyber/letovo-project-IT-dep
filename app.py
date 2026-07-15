# -*- coding: utf-8 -*-
"""
GreenNet Crisis — серверная часть (Flask + SQLite).

Copyright © 2026 Астанин Никита Артёмович (TokKraftcorp). Все права защищены.
Проприетарная лицензия — см. файл LICENSE.

Единая платформа управления ликвидацией экологической катастрофы для программы
«Летово Игра». В отличие от браузерного прототипа, здесь:
  * настоящий вход по логину и паролю (пароли хранятся в виде хэшей);
  * права проверяются на СЕРВЕРЕ — их нельзя обойти через консоль браузера;
  * общая база данных для всех участников;
  * админ-панель: создание пользователей, выдача ролей, удаление.

Запуск:
    pip install flask
    python app.py
Затем открыть http://localhost:5000
"""

import os
import re
import html
import time
import json
import csv
import gzip
import base64
import sqlite3
import secrets
import threading
import urllib.request
import urllib.error
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from functools import wraps
from html.parser import HTMLParser

# Московское время (UTC+3, без перехода на летнее) — чтобы часы были верны
# независимо от часового пояса сервера/контейнера.
MSK = timezone(timedelta(hours=3))

from flask import (
    Flask, g, request, session, jsonify, send_from_directory, send_file, abort
)
from werkzeug.security import generate_password_hash, check_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# Каталог для данных можно переопределить через GREENNET_DATA (для Docker-тома / хостинга).
DATA_DIR = os.environ.get("GREENNET_DATA", BASE_DIR)
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "greennet.db")
SECRET_PATH = os.path.join(DATA_DIR, "secret_key.txt")
ADMIN_PW_PATH = os.path.join(DATA_DIR, "admin_password.txt")
DEMO_PW_PATH = os.path.join(DATA_DIR, "demo_password.txt")
USERS_CSV_PATH = os.path.join(DATA_DIR, "users.csv")
NIMPH_BASE_URL = (os.environ.get("GREENNET_NIMPH_URL") or
                  "http://nimph.fial-corporation.ru").strip().rstrip("/")

# Демо-режим: показной сценарий «Зелёный прилив» + игровые аккаунты + панель на входе.
# По умолчанию ВЫКЛЮЧЕН (продакшн): создаётся только админ-аккаунт, всё остальное пусто.
# Включить: переменная окружения GREENNET_DEMO=1
DEMO_MODE = os.environ.get("GREENNET_DEMO", "").lower() in ("1", "true", "yes")
ADMIN_USER = ((os.environ.get("GREENNET_ADMIN_USER") or "admin").strip().lower()) or "admin"
# Самостоятельная регистрация участников. По умолчанию ВКЛ. Отключить: GREENNET_REGISTRATION=0
ALLOW_REGISTRATION = os.environ.get("GREENNET_REGISTRATION", "1").lower() not in ("0", "false", "no")
# Доверять заголовку X-Forwarded-For (включать ТОЛЬКО за доверенным прокси, напр. nginx)
_TRUST_PROXY = os.environ.get("GREENNET_TRUST_PROXY", "").lower() in ("1", "true", "yes")

# --- Слой интеграции с порталом «Летово Игра» --------------------------------
# local  — только свой вход (по умолчанию, автономный режим).
# portal — вход через портал (Bearer/JWT портала) + подтягивание ролей/департаментов.
#          Свой вход остаётся как fallback. Портал дёргаем ТОЛЬКО server-to-server
#          (у портала нет CORS). Токен валидируем через GET {PORTAL}/auth/amiauthed,
#          профиль — GET /user/full/:username, роли — GET /user/roles/:username.
AUTH_MODE = ((os.environ.get("GREENNET_AUTH_MODE") or "local").strip().lower()) or "local"  # local | portal
PORTAL_BASE_URL = (os.environ.get("GREENNET_PORTAL_URL") or "").strip().rstrip("/")
PORTAL_API_KEY = os.environ.get("GREENNET_PORTAL_KEY") or ""
PORTAL_ENABLED = AUTH_MODE == "portal" and bool(PORTAL_BASE_URL)

# Соответствие ролей портала → наши. Настраивается JSON-строкой в GREENNET_PORTAL_ROLE_MAP.
# Ключи — как называет роли портал (в нижнем регистре); значения — наши id ролей.
_DEFAULT_ROLE_MAP = {
    "admin": "admin", "администратор": "admin",
    "hq": "hq_head", "штаб": "hq_head", "headquarters": "hq_head",
    "head": "dept_head", "глава": "dept_head", "lead": "dept_head", "руководитель": "dept_head",
    "senior": "senior", "старший": "senior",
    "specialist": "specialist", "специалист": "specialist", "member": "specialist",
    "observer": "observer", "волонтёр": "observer", "волонтер": "observer", "наблюдатель": "observer",
}
# Соответствие department-id портала (число, как строка) → наш департамент.
_DEFAULT_DEPT_MAP = {
    "1": "Департамент управления", "2": "Департамент общественных связей",
    "3": "Инженерный департамент", "4": "Департамент Икс", "5": "Научный департамент",
    "6": "Департамент IT", "7": "Департамент дизайна", "8": "Проект 11",
}

def _load_map(env_name, default):
    raw = os.environ.get(env_name)
    if raw:
        try:
            m = json.loads(raw)
            if isinstance(m, dict):
                return {str(k).lower(): str(v) for k, v in m.items()}
        except Exception:
            pass
    return dict(default)

PORTAL_ROLE_MAP = _load_map("GREENNET_PORTAL_ROLE_MAP", _DEFAULT_ROLE_MAP)
PORTAL_DEPT_MAP = _load_map("GREENNET_PORTAL_DEPT_MAP", _DEFAULT_DEPT_MAP)

app = Flask(__name__, static_folder="static", static_url_path="/static")

# --- Постоянный секретный ключ (чтобы сессии не сбрасывались при перезапуске) ---
def _load_secret():
    if os.path.exists(SECRET_PATH):
        with open(SECRET_PATH, "r", encoding="utf-8") as f:
            return f.read().strip()
    key = secrets.token_hex(32)
    with open(SECRET_PATH, "w", encoding="utf-8") as f:
        f.write(key)
    return key

app.config.update(
    SECRET_KEY=_load_secret(),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    # При публикации по HTTPS выставьте GREENNET_HTTPS=1 — кука станет Secure.
    SESSION_COOKIE_SECURE=os.environ.get("GREENNET_HTTPS", "").lower() in ("1", "true", "yes"),
    JSON_AS_ASCII=False,
    MAX_CONTENT_LENGTH=64 * 1024,   # запросы больше 64 КБ отклоняются (анти-флуд)
)


@app.errorhandler(OverflowError)
def _handle_overflow(e):
    # Огромный числовой id в URL (>= 2**63) не влезает в 64-битный INTEGER SQLite —
    # отдаём чистый 400 вместо необработанного 500.
    return jsonify(error="Некорректный идентификатор"), 400

# ---------------------------------------------------------------------------
# Роли, права, справочники  (ЕДИНЫЙ источник правды для сервера)
# ---------------------------------------------------------------------------
ROLES = {
    "admin":      {"label": "Администратор", "short": "АД", "rank": 6},
    "hq_head":    {"label": "Руководитель штаба", "short": "РШ", "rank": 5},
    "dept_head":  {"label": "Руководитель департамента", "short": "РД", "rank": 4},
    "senior":     {"label": "Старший специалист", "short": "СС", "rank": 3},
    "specialist": {"label": "Специалист", "short": "СП", "rank": 2},
    "observer":   {"label": "Волонтёр / наблюдатель", "short": "ВН", "rank": 1},
}
ROLE_ORDER = ["admin", "hq_head", "dept_head", "senior", "specialist", "observer"]

# Права (capabilities) на каждую роль. Проверяются на сервере при каждом действии.
#   post:        all | basic | none        — какие типы сообщений можно отправлять
#   verify:      True/False                — подтверждать/отклонять достоверность данных
#   task_create: all | propose | none      — создавать задачи
#   task_close:  all | own | none          — закрывать задачи (own = только своего департамента)
#   decide:      True/False                — принимать решения штаба
#   manage:      True/False                — управлять пользователями (админ-панель)
#   chat:        True/False                — писать в чаты департаментов (только главы/штаб)
#   finance:     True/False                — переводить игровую валюту «энеоины» (штаб/админ)
CAPS = {
    "admin":      {"post": "all",   "verify": True,  "task_create": "all",     "task_close": "all",  "decide": True,  "manage": True,  "chat": True,  "finance": True},
    "hq_head":    {"post": "all",   "verify": True,  "task_create": "all",     "task_close": "all",  "decide": True,  "manage": False, "chat": True,  "finance": True},
    "dept_head":  {"post": "all",   "verify": True,  "task_create": "all",     "task_close": "own",  "decide": False, "manage": False, "chat": True,  "finance": False},
    "senior":     {"post": "all",   "verify": True,  "task_create": "propose", "task_close": "own",  "decide": False, "manage": False, "chat": False, "finance": False},
    "specialist": {"post": "all",   "verify": False, "task_create": "propose", "task_close": "none", "decide": False, "manage": False, "chat": False, "finance": False},
    "observer":   {"post": "basic", "verify": False, "task_create": "none",    "task_close": "none", "decide": False, "manage": False, "chat": False, "finance": False},
}

MSG_TYPES = {
    "focus":    {"label": "Новый очаг заражения",     "basic": True},
    "urgent":   {"label": "Срочная проблема",         "basic": False},
    "help":     {"label": "Требуется помощь",         "basic": True},
    "research": {"label": "Результаты исследования",  "basic": False},
    "request":  {"label": "Запрос департаменту",      "basic": False},
}
# Департаменты «Летово Игра» (полные названия). Каждый участник приписан к департаменту.
DEPTS = [
    "Департамент управления",
    "Департамент общественных связей",
    "Инженерный департамент",
    "Департамент Икс",
    "Научный департамент",
    "Департамент IT",
    "Департамент дизайна",
    "Проект 11",
]
DEPT_INFO = {
    "Департамент управления": "Менеджмент, маркетинг, финансы",
    "Департамент общественных связей": "PR, медиа, пресса и коммуникации",
    "Инженерный департамент": "Моделирование и проектирование устройств",
    "Департамент Икс": "Секретные материалы, расследования, deep-research",
    "Научный департамент": "Исследования, разработки, конференции",
    "Департамент IT": "Программирование, нейросети, кибербезопасность",
    "Департамент дизайна": "Продукты с безупречной эргономикой",
    "Проект 11": "Общеобразовательный департамент для младших игроков",
}
# Соответствие старых демо-департаментов новым (для показного сценария)
_DEMO_DEPT_MAP = {
    "Штаб": "Департамент управления",
    "Научный": "Научный департамент",
    "Инженерный": "Инженерный департамент",
    "Медицинский": "Департамент Икс",
    "Логистика": "Проект 11",
    "IT": "Департамент IT",
}

# Типы пунктов расписания и уровни новостей (справочники для интерфейса)
SCHED_KINDS = {
    "briefing": "Брифинг",
    "input":    "Вводная",
    "deadline": "Дедлайн",
    "shift":    "Смена",
    "event":    "Событие",
}
NEWS_LEVELS = {
    "info":     "Инфо",
    "warning":  "Важно",
    "critical": "Срочно",
}

# ---------------------------------------------------------------------------
# База данных
# ---------------------------------------------------------------------------
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
        # WAL: читатели не блокируют друг друга — важно при ~200 одновременных игроках
        g.db.execute("PRAGMA journal_mode = WAL")
        g.db.execute("PRAGMA busy_timeout = 5000")
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT UNIQUE NOT NULL,
    name          TEXT NOT NULL,
    first_name    TEXT NOT NULL DEFAULT '',
    last_name     TEXT NOT NULL DEFAULT '',
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL,
    dept          TEXT NOT NULL,
    active        INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL,
    terms_at      TEXT
);
CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    type        TEXT NOT NULL,
    author_id   INTEGER,
    author_name TEXT NOT NULL,
    dept        TEXT NOT NULL,
    text        TEXT NOT NULL,
    verify      TEXT NOT NULL DEFAULT 'unverified',
    verified_by TEXT,
    coords_x    REAL,
    coords_y    REAL,
    sev         TEXT,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT NOT NULL,
    descr       TEXT,
    dept        TEXT NOT NULL,
    assignee    TEXT NOT NULL,
    due         TEXT,
    status      TEXT NOT NULL DEFAULT 'new',
    created_by  TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS knowledge (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    type        TEXT NOT NULL,
    title       TEXT NOT NULL,
    where_txt   TEXT,
    who         TEXT,
    body        TEXT,
    verify      TEXT NOT NULL DEFAULT 'unverified',
    src_msg     INTEGER,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS map_objects (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    kind    TEXT NOT NULL,
    x       REAL NOT NULL,
    y       REAL NOT NULL,
    label   TEXT NOT NULL,
    sev     TEXT,
    src_msg INTEGER
);
CREATE TABLE IF NOT EXISTS logs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    actor      TEXT NOT NULL,
    action     TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS news (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    title      TEXT NOT NULL,
    body       TEXT,
    level      TEXT NOT NULL DEFAULT 'info',
    author     TEXT,
    pinned     INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS schedule (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    at_time    TEXT NOT NULL,               -- 'ЧЧ:ММ'
    title      TEXT NOT NULL,
    dept       TEXT,
    kind       TEXT NOT NULL DEFAULT 'event',
    note       TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS channel_messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    dept        TEXT NOT NULL,
    author_id   INTEGER,
    author_name TEXT NOT NULL,
    author_role TEXT,
    text        TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chat_groups (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    description TEXT,
    created_by  INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chat_group_members (
    group_id  INTEGER NOT NULL REFERENCES chat_groups(id) ON DELETE CASCADE,
    user_id   INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    joined_at TEXT NOT NULL,
    PRIMARY KEY (group_id, user_id)
);
CREATE TABLE IF NOT EXISTS chat_group_messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id    INTEGER NOT NULL REFERENCES chat_groups(id) ON DELETE CASCADE,
    author_id   INTEGER REFERENCES users(id) ON DELETE SET NULL,
    author_name TEXT NOT NULL,
    text        TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chat_group_members_user ON chat_group_members(user_id, group_id);
CREATE INDEX IF NOT EXISTS idx_chat_group_messages_group ON chat_group_messages(group_id, id);
CREATE TABLE IF NOT EXISTS sensor_snapshots (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    mission_time  TEXT NOT NULL,
    collected_at  TEXT NOT NULL,
    station_count INTEGER NOT NULL,
    payload_gzip  BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sensor_snapshots_time ON sensor_snapshots(id DESC);
CREATE TABLE IF NOT EXISTS accounts (
    dept    TEXT PRIMARY KEY,
    balance INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS transactions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    from_dept  TEXT,
    to_dept    TEXT,
    amount     INTEGER NOT NULL,
    note       TEXT,
    actor      TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def now_iso():
    return datetime.now(MSK).isoformat(timespec="seconds")


def _split_person_name(name):
    """Разложить старое единое поле имени на имя и фамилию без потери исходного текста."""
    parts = str(name or "").strip().split(maxsplit=1)
    return (parts[0] if parts else "", parts[1] if len(parts) > 1 else "")


def _person_from_data(data):
    """Принять новые first_name/last_name и сохранить совместимость со старым полем name."""
    first_name = str(data.get("first_name") or "").strip()
    last_name = str(data.get("last_name") or "").strip()
    if not first_name and not last_name and data.get("name"):
        first_name, last_name = _split_person_name(data.get("name"))
    return first_name, last_name, " ".join(x for x in (first_name, last_name) if x)


_CSV_LOCK = threading.Lock()


def sync_users_csv(db):
    """Обновить CSV-датасет профилей. Пароли и их хэши намеренно не экспортируются."""
    rows = db.execute(
        "SELECT id,username,first_name,last_name,name,role,dept,active,created_at,terms_at "
        "FROM users ORDER BY id"
    ).fetchall()
    fields = ("id", "username", "first_name", "last_name", "display_name", "role",
              "dept", "active", "created_at", "terms_at")
    tmp_path = USERS_CSV_PATH + ".tmp"
    with _CSV_LOCK:
        try:
            with open(tmp_path, "w", encoding="utf-8-sig", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=fields)
                writer.writeheader()
                for row in rows:
                    writer.writerow({
                        "id": row["id"], "username": row["username"],
                        "first_name": row["first_name"] or "", "last_name": row["last_name"] or "",
                        "display_name": row["name"], "role": row["role"], "dept": row["dept"],
                        "active": 1 if row["active"] else 0, "created_at": row["created_at"],
                        "terms_at": row["terms_at"] or "",
                    })
            os.replace(tmp_path, USERS_CSV_PATH)
            return True
        except OSError as exc:
            # На Windows файл может быть временно заблокирован открытым Excel. Это не должно
            # отменять уже успешную регистрацию: CSV обновится при следующем изменении/скачивании.
            print(f"Предупреждение: не удалось обновить users.csv: {exc}")
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except OSError:
                pass
            return False


def _admin_password():
    """Пароль админа: из GREENNET_ADMIN_PASSWORD, иначе из файла, иначе генерируем надёжный.
    Возвращает (пароль, был_ли_сгенерирован_сейчас)."""
    env = os.environ.get("GREENNET_ADMIN_PASSWORD")
    if env:
        return env, False
    if os.path.exists(ADMIN_PW_PATH):
        with open(ADMIN_PW_PATH, "r", encoding="utf-8") as f:
            saved = f.read().strip()
        if saved:
            return saved, False
    pw = secrets.token_urlsafe(9)   # ~12 символов, криптостойкий
    with open(ADMIN_PW_PATH, "w", encoding="utf-8") as f:
        f.write(pw)
    return pw, True


def _demo_password():
    """Пароль демонстрационных профилей: только из окружения или локального data-файла."""
    env = os.environ.get("GREENNET_DEMO_PASSWORD")
    if env:
        return env, False
    if os.path.exists(DEMO_PW_PATH):
        with open(DEMO_PW_PATH, "r", encoding="utf-8") as f:
            saved = f.read().strip()
        if saved:
            return saved, False
    pw = secrets.token_urlsafe(9)
    with open(DEMO_PW_PATH, "w", encoding="utf-8") as f:
        f.write(pw)
    return pw, True


def _seed_settings(db):
    settings = {
        "incident_name": "Инцидент «Зелёный прилив»",
        "incident_place": "Река Летовка · сектор 4",
        "safe_zone": "16,74,150,74",  # x%,y%,w,h
    }
    for k, v in settings.items():
        db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (k, v))
    # Стартовые счета департаментов в игровой валюте «энеоин» (казна у управления)
    for d in DEPTS:
        bal = 5000 if d == "Департамент управления" else 1000
        db.execute("INSERT OR IGNORE INTO accounts(dept,balance) VALUES(?,?)", (d, bal))


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    # мягкая миграция для баз, созданных до появления колонки terms_at
    try:
        db.execute("ALTER TABLE users ADD COLUMN terms_at TEXT")
    except sqlite3.OperationalError:
        pass
    for column in ("first_name", "last_name"):
        try:
            db.execute(f"ALTER TABLE users ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
        except sqlite3.OperationalError:
            pass
    # Перенос данных из старого поля name в раздельные имя/фамилию.
    for row in db.execute("SELECT id,name,first_name,last_name FROM users").fetchall():
        if not row["first_name"] and not row["last_name"]:
            first_name, last_name = _split_person_name(row["name"])
            db.execute("UPDATE users SET first_name=?,last_name=? WHERE id=?",
                       (first_name, last_name, row["id"]))
    _commit(db)
    # Заполняем сценарий только если база пустая
    have_users = db.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
    if not have_users:
        seed(db)
    sync_users_csv(db)
    db.close()


def seed(db):
    """Начальный сценарий инцидента «Зелёный прилив» + демо-аккаунты."""
    def t(h, m):
        return f"2026-07-11T{h:02d}:{m:02d}:00"

    # --- Админ-аккаунт (создаётся всегда) ---
    admin_pw, generated = _admin_password()
    db.execute(
        "INSERT INTO users(username,name,first_name,last_name,password_hash,role,dept,active,created_at) "
        "VALUES(?,?,?,?,?,?,?,1,?)",
        (ADMIN_USER, "Администратор", "Администратор", "", generate_password_hash(admin_pw),
         "admin", "Департамент управления", now_iso()))
    if generated:
        print("\n" + "=" * 54)
        print("  СОЗДАН АДМИН-АККАУНТ (сгенерирован надёжный пароль)")
        print(f"    логин:  {ADMIN_USER}")
        print(f"    пароль: {admin_pw}")
        print(f"    (также сохранён в {ADMIN_PW_PATH})")
        print("  Смените его в админ-панели после первого входа.")
        print("=" * 54 + "\n")

    if not DEMO_MODE:
        # Продакшн: сцену наполняет сам админ. Только настройки — и выходим.
        _seed_settings(db)
        _commit(db)
        return

    # --- Демо-режим: игровые аккаунты с локальным/настраиваемым паролем ---
    demo_pw, demo_generated = _demo_password()
    if demo_generated:
        print("\n" + "=" * 54)
        print("  СОЗДАН ПАРОЛЬ ДЕМО-ПРОФИЛЕЙ")
        print(f"    пароль: {demo_pw}")
        print(f"    (также сохранён в {DEMO_PW_PATH})")
        print("  Для фиксированного значения задайте GREENNET_DEMO_PASSWORD.")
        print("=" * 54 + "\n")
    users = [
        ("kovalev",  "Д. Ковалёв",      "hq_head",    "Штаб"),
        ("sokolova", "М. Соколова",     "dept_head",  "Научный"),
        ("petrov",   "И. Петров",       "senior",     "Научный"),
        ("volkov",   "Е. Волков",       "specialist", "Инженерный"),
        ("smirnova", "Л. Смирнова",     "specialist", "Медицинский"),
        ("litvin",   "К. Литвин",       "specialist", "IT"),
        ("volonter", "Т. Наблюдатель",  "observer",   "Логистика"),
    ]
    for username, name, role, dept in users:
        first_name, last_name = _split_person_name(name)
        db.execute(
            "INSERT INTO users(username,name,first_name,last_name,password_hash,role,dept,active,created_at) "
            "VALUES(?,?,?,?,?,?,?,1,?)",
            (username, name, first_name, last_name, generate_password_hash(demo_pw), role, dept, now_iso()),
        )

    # --- Сообщения ленты ---
    msgs = [
        ("focus", "М. Соколова", "Научный",
         "Обнаружен новый очаг цветения токсичного грибка на участке №17, правый берег реки Летовки. "
         "Вода светится зелёным, характерный запах.", "verified", "Д. Ковалёв", 31, 44, "high", t(9, 42)),
        ("research", "И. Петров", "Научный",
         "Экспресс-анализ проб с участка №17: концентрация биотоксина превышает норму в 6 раз. "
         "Возбудитель — ранее не описанный штамм.", "verified", "М. Соколова", None, None, None, t(10, 15)),
        ("urgent", "Л. Смирнова", "Медицинский",
         "Двое волонтёров с раздражением кожи после контакта с водой. Срочно нужны средства защиты в секторе 4.",
         "unverified", None, None, None, None, t(10, 38)),
        ("help", "Т. Наблюдатель", "Логистика",
         "Требуется 20 комплектов защитных костюмов в сектор 4 до 16:00.",
         "unverified", None, None, None, None, t(10, 52)),
        ("request", "Е. Волков", "Инженерный",
         "Запрос в IT: нужны точные координаты границ очага для запуска дрона-разведчика над участком №17.",
         "verified", "Д. Ковалёв", None, None, None, t(11, 9)),
        ("focus", "И. Петров", "Научный",
         "Второй очаг замечен на участке №19, примерно в 400 м ниже по течению. Пятно меньше, но растёт.",
         "unverified", None, 52, 57, "medium", t(11, 34)),
        ("research", "К. Литвин", "IT",
         "Модель течения реки: при текущей скорости кромка загрязнения достигнет городского водозабора "
         "примерно через 9 часов.", "verified", "Д. Ковалёв", None, None, None, t(11, 58)),
    ]
    for m in msgs:
        db.execute(
            "INSERT INTO messages(type,author_name,dept,text,verify,verified_by,coords_x,coords_y,sev,created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)", m)

    # --- Задачи ---
    tasks = [
        ("Исследовать границы очага на участке №17", "Взять пробы по периметру, определить скорость распространения.",
         "Научный", "И. Петров", t(18, 0), "prog", "М. Соколова", t(9, 50)),
        ("Доставить 20 защитных костюмов в сектор 4", "По запросу медицинского департамента, срок критичный.",
         "Логистика", "Логистика", t(16, 0), "new", "Д. Ковалёв", t(10, 55)),
        ("Запустить дрон над участками №17–19", "Аэросъёмка очагов, передача координат в базу знаний.",
         "Инженерный", "Е. Волков", t(15, 30), "prog", "Д. Ковалёв", t(11, 12)),
        ("Подготовить план изоляции водозабора", "На основе модели течения от IT. Решение принимает штаб.",
         "Штаб", "Д. Ковалёв", t(20, 0), "new", "Д. Ковалёв", t(12, 2)),
        ("Развернуть пункт дезинфекции у моста", "Первичная обработка людей, выходящих из сектора 4.",
         "Медицинский", "Л. Смирнова", t(13, 0), "done", "М. Соколова", t(9, 30)),
    ]
    for tk in tasks:
        db.execute(
            "INSERT INTO tasks(title,descr,dept,assignee,due,status,created_by,created_at)"
            " VALUES(?,?,?,?,?,?,?,?)", tk)

    # --- База знаний ---
    kb = [
        ("focus", "Очаг №17 — цветение токсичного грибка", "Участок №17, правый берег",
         "М. Соколова (Научный)", "Первичный очаг. Вода светится зелёным, резкий запах. Концентрация токсина ×6 от нормы.",
         "verified", 1, t(9, 42)),
        ("research", "Идентификация возбудителя", "Лаборатория, сектор 4",
         "И. Петров (Научный)", "Ранее не описанный штамм грибка. Биотоксин поражает кожу при контакте. Требуется СИЗ.",
         "verified", 2, t(10, 15)),
        ("research", "Модель распространения по реке", "Река Летовка, сектор 4 → город",
         "К. Литвин (IT)", "Прогноз: кромка загрязнения достигнет водозабора через ~9 ч. Ключевая точка защиты — водозабор.",
         "verified", 7, t(11, 58)),
    ]
    for k in kb:
        db.execute(
            "INSERT INTO knowledge(type,title,where_txt,who,body,verify,src_msg,created_at)"
            " VALUES(?,?,?,?,?,?,?,?)", k)

    # --- Карта ---
    objs = [
        ("focus", 31, 44, "Очаг №17", "high"),
        ("focus", 52, 57, "Очаг №19", "medium"),
        ("lab", 20, 24, "Лаборатория", None),
        ("drone", 41, 38, "Дрон D-1", None),
        ("drone", 60, 50, "Дрон D-2", None),
        ("team", 37, 52, "Группа Альфа", None),
        ("intake", 83, 74, "Водозабор", None),
    ]
    for o in objs:
        db.execute("INSERT INTO map_objects(kind,x,y,label,sev) VALUES(?,?,?,?,?)", o)

    # --- Журнал ---
    logs = [
        ("М. Соколова", "сообщила о <b>новом очаге</b> на участке №17", t(9, 42)),
        ("Д. Ковалёв", "<b>подтвердил</b> координаты очага №17", t(9, 55)),
        ("И. Петров", "загрузил <b>результаты анализа</b> проб", t(10, 15)),
        ("Д. Ковалёв", "поставил задачу <b>«Запустить дрон над №17–19»</b>", t(11, 12)),
        ("К. Литвин", "добавил <b>модель течения</b> в базу знаний", t(11, 58)),
    ]
    for l in logs:
        db.execute("INSERT INTO logs(actor,action,created_at) VALUES(?,?,?)", l)

    # --- Перевод демо-департаментов в новую таксономию «Летово Игра» ---
    for old, new in _DEMO_DEPT_MAP.items():
        db.execute("UPDATE users SET dept=? WHERE dept=?", (new, old))
        db.execute("UPDATE messages SET dept=? WHERE dept=?", (new, old))
        db.execute("UPDATE tasks SET dept=? WHERE dept=?", (new, old))

    # --- Чаты департаментов (пишут только главы/штаб) ---
    chats = [
        ("Департамент управления", "Д. Ковалёв", "hq_head",
         "Главам департаментов: сводки каждый час. Приоритет — изоляция водозабора.", t(12, 10)),
        ("Научный департамент", "М. Соколова", "dept_head",
         "Свожу данные по очагу №17. Пробы по периметру — к 15:00.", t(12, 15)),
        ("Инженерный департамент", "Д. Ковалёв", "hq_head",
         "Нужен дрон на участок №19. Кто берёт?", t(12, 20)),
    ]
    for dept, name, role, text, ts in chats:
        db.execute(
            "INSERT INTO channel_messages(dept,author_name,author_role,text,created_at) VALUES(?,?,?,?,?)",
            (dept, name, role, text, ts))

    # --- Настройки сцены ---
    _seed_settings(db)

    _commit(db)


def h(x):
    """Экранирование пользовательских данных перед вставкой в HTML-строку журнала."""
    return html.escape(str(x if x is not None else ""))


def _body():
    """Тело запроса как словарь. Не-объектный JSON (массив/строка/число) → {},
    иначе data.get(...) упал бы с 500. RecursionError на глубоко вложенном JSON
    get_json(silent=True) не гасит — ловим сами."""
    try:
        d = request.get_json(silent=True)
    except Exception:
        return {}
    return d if isinstance(d, dict) else {}


# Версия данных: растёт при каждом изменении. Нужна для мгновенных 304-ответов
# в /api/state (сравнение числа вместо запросов к базе). Стартуем от времени,
# чтобы после перезапуска сервера версии не совпали со старыми у клиентов.
_DATA_VERSION = int(time.time())


def bump_version():
    global _DATA_VERSION
    _DATA_VERSION += 1


def add_log(db, actor, action):
    db.execute("INSERT INTO logs(actor,action,created_at) VALUES(?,?,?)", (actor, action, now_iso()))


def _commit(db):
    """Коммит + повышение версии данных ПОСЛЕ фиксации (иначе поллер мог бы закэшировать
    ответ под новой версией до того, как запись реально видна)."""
    db.commit()
    bump_version()


# ---------------------------------------------------------------------------
# Аутентификация и проверка прав
# ---------------------------------------------------------------------------
def current_user():
    uid = session.get("uid")
    if not uid:
        return None
    row = get_db().execute("SELECT * FROM users WHERE id=? AND active=1", (uid,)).fetchone()
    return row


def login_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if not current_user():
            return jsonify(error="Требуется вход"), 401
        return fn(*a, **kw)
    return wrapper


def caps_for(role):
    return CAPS.get(role, CAPS["observer"])


def require(cap_check):
    """Декоратор: проверить право текущего пользователя перед действием."""
    def deco(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            u = current_user()
            if not u:
                return jsonify(error="Требуется вход"), 401
            if not cap_check(caps_for(u["role"]), u):
                return jsonify(error="Недостаточно прав для этого действия"), 403
            return fn(*a, **kw)
        return wrapper
    return deco


# ---------------------------------------------------------------------------
# Сериализация
# ---------------------------------------------------------------------------
def upsert_external_user(db, ext_username, name, dept=None, role=None, email=None):
    """ЕДИНАЯ ТОЧКА ВХОДА через портал/SSO (слой интеграции).

    Вызывается обработчиком SSO-колбэка ПОСЛЕ проверки токена провайдера
    (см. INTEGRATION.md). Создаёт нового участника или обновляет существующего
    по логину. Локальный пароль внешним пользователям не нужен — ставится
    случайный. Новому участнику присваивается роль 'observer'; повысить может
    только администратор (роль существующего пользователя здесь не меняется).
    Возвращает строку пользователя (sqlite3.Row).
    """
    username = str(ext_username or "").strip().lower()
    if not username:
        raise ValueError("ext_username обязателен")
    name = (str(name or "").strip()) or username
    first_name, last_name = _split_person_name(name)
    dept = dept if dept in DEPTS else "Проект 11"
    role = role if role in ROLES else "observer"
    row = db.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    if row:
        # существующего не трогаем по роли — ролями управляет только админ
        db.execute("UPDATE users SET name=?,first_name=?,last_name=?,active=1 WHERE id=?",
                   (name, first_name, last_name, row["id"]))
        _commit(db)
        sync_users_csv(db)
        return db.execute("SELECT * FROM users WHERE id=?", (row["id"],)).fetchone()
    try:
        db.execute(
            "INSERT INTO users(username,name,first_name,last_name,password_hash,role,dept,active,created_at,terms_at)"
            " VALUES(?,?,?,?,?,?,?,1,?,?)",
            (username, name, first_name, last_name, generate_password_hash(secrets.token_urlsafe(16)),
             role, dept, now_iso(), now_iso()))
        _commit(db)
    except sqlite3.IntegrityError:
        db.rollback()   # кто-то создал этот логин параллельно — просто возвращаем его
    sync_users_csv(db)
    return db.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()


def user_public(row):
    return {
        "id": row["id"], "username": row["username"], "name": row["name"],
        "first_name": row["first_name"] or "", "last_name": row["last_name"] or "",
        "role": row["role"], "role_label": ROLES.get(row["role"], {}).get("label", row["role"]),
        "short": ROLES.get(row["role"], {}).get("short", "?"),
        "dept": row["dept"], "active": bool(row["active"]),
    }


def msg_public(row):
    d = {k: row[k] for k in ("id", "type", "author_name", "dept", "text", "verify", "verified_by", "sev")}
    d["ts"] = row["created_at"]
    if row["coords_x"] is not None:
        d["coords"] = {"x": row["coords_x"], "y": row["coords_y"]}
    return d


def task_public(row):
    d = {k: row[k] for k in ("id", "title", "descr", "dept", "assignee", "due", "status", "created_by")}
    d["ts"] = row["created_at"]
    return d


def kb_public(row):
    return {
        "id": row["id"], "type": row["type"], "title": row["title"], "where": row["where_txt"],
        "who": row["who"], "body": row["body"], "verify": row["verify"], "ts": row["created_at"],
    }


def obj_public(row):
    return {"id": row["id"], "kind": row["kind"], "x": row["x"], "y": row["y"], "label": row["label"], "sev": row["sev"]}


def log_public(row):
    return {"id": row["id"], "actor": row["actor"], "action": row["action"], "ts": row["created_at"]}


def news_public(row):
    return {"id": row["id"], "title": row["title"], "body": row["body"], "level": row["level"],
            "author": row["author"], "pinned": bool(row["pinned"]), "ts": row["created_at"]}


def sched_public(row):
    return {"id": row["id"], "at": row["at_time"], "title": row["title"],
            "dept": row["dept"], "kind": row["kind"], "note": row["note"]}


def chan_public(row):
    return {"id": row["id"], "dept": row["dept"], "author": row["author_name"],
            "role": row["author_role"], "text": row["text"], "ts": row["created_at"]}


def group_message_public(row):
    return {"id": row["id"], "group_id": row["group_id"], "author_id": row["author_id"],
            "author": row["author_name"], "text": row["text"], "ts": row["created_at"]}


def account_public(row):
    return {"dept": row["dept"], "balance": row["balance"]}


def tx_public(row):
    return {"id": row["id"], "from": row["from_dept"], "to": row["to_dept"],
            "amount": row["amount"], "note": row["note"], "actor": row["actor"], "ts": row["created_at"]}


# ---------------------------------------------------------------------------
# Маршруты: страница
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


# ---------------------------------------------------------------------------
# Простая защита от перебора паролей (in-memory)
# ---------------------------------------------------------------------------
# Хранится в памяти процесса; для боевого запуска gunicorn -w 1 (см. Dockerfile)
# этого достаточно. Ключ входа — (логин + IP): так мы не блокируем всю школьную
# сеть (все за одним IP) из-за ошибок одного участника, но защищаем конкретный
# аккаунт от подбора пароля.
_LOGIN_FAILS = {}
_REG_HITS = {}
LOGIN_WINDOW, LOGIN_MAX = 300, 10    # ≤10 неудачных входов за 5 минут на (логин+IP)
REG_WINDOW, REG_MAX = 600, 30        # ≤30 регистраций за 10 минут с одного IP
_SWEEP_LOCK = threading.Lock()
_LAST_SWEEP = [time.monotonic()]     # список — чтобы менять из вложенной функции


def _sweep_stores():
    """Периодическая чистка всех in-memory хранилищ от протухших ключей.
    Ключ _LOGIN_FAILS = логин+IP (логин задаёт атакующий), поэтому без общей уборки
    он рос бы без предела при переборе несуществующих логинов."""
    now = time.monotonic()
    if now - _LAST_SWEEP[0] < 120:
        return
    if not _SWEEP_LOCK.acquire(blocking=False):
        return
    try:
        _LAST_SWEEP[0] = now
        for store, window in ((_LOGIN_FAILS, LOGIN_WINDOW), (_REG_HITS, REG_WINDOW),
                              (_MSG_HITS, 30), (_VIOLATIONS, VIOL_WINDOW)):
            for k in list(store.keys()):
                fresh = [t for t in store.get(k, ()) if now - t < window]
                if fresh:
                    store[k] = fresh
                else:
                    store.pop(k, None)
        for k in list(_MUTED.keys()):
            if _MUTED.get(k, 0) <= now:
                _MUTED.pop(k, None)
        for k in list(_LAST_TEXT.keys()):
            v = _LAST_TEXT.get(k)
            if v and now - v[1] > 120:
                _LAST_TEXT.pop(k, None)
    finally:
        _SWEEP_LOCK.release()


def _client_ip():
    # По умолчанию доверяем ТОЛЬКО реальному TCP-адресу (его нельзя подделать) —
    # иначе злоумышленник меняет X-Forwarded-For на каждый запрос и обходит лимит входа.
    # За доверенным обратным прокси (nginx, который сам выставляет XFF) включите
    # GREENNET_TRUST_PROXY=1 — тогда берём адрес из заголовка.
    if _TRUST_PROXY:
        fwd = request.headers.get("X-Forwarded-For", "")
        if fwd:
            return fwd.split(",")[0].strip()
    return request.remote_addr or "?"


def _rate_ok(store, key, window, maxn):
    _sweep_stores()
    now = time.monotonic()
    hits = [t for t in store.get(key, []) if now - t < window]
    if hits:
        store[key] = hits
    else:
        store.pop(key, None)   # не копим пустые ключи (иначе dict растёт без предела)
    return len(hits) < maxn


def _rate_hit(store, key):
    store.setdefault(key, []).append(time.monotonic())


# ---------------------------------------------------------------------------
# Анти-мат, анти-флуд и авто-мут (модерация контента на сервере)
# ---------------------------------------------------------------------------
MSG_MAX_LEN = 1000

# Латиница/цифры-двойники → кириллица (ловим «xyй», «пи3да» и т.п.).
# Направление выбрано по транслиту: p→п (pidor), c→с, y→у, x→х.
_LOOKALIKE = str.maketrans({
    "a": "а", "b": "б", "c": "с", "d": "д", "e": "е", "f": "ф", "g": "г",
    "h": "х", "i": "и", "j": "й", "k": "к", "l": "л", "m": "м", "n": "н",
    "o": "о", "p": "п", "r": "р", "s": "с", "t": "т", "u": "у", "x": "х",
    "y": "у", "z": "з", "@": "а", "$": "с", "0": "о", "3": "з", "4": "ч", "6": "б",
})

# Корни, безопасные для поиска «внутри» слова (низкий риск ложных срабатываний).
# ВАЖНО: «бляд/блят» сюда нельзя — ловят «оскорблять», «употреблять» (см. _RU_WORDSTART).
_RU_ROOTS = (
    "пизд", "пидор", "пидар", "педик", "гандон", "гондон",
    "мудак", "мудил", "залуп", "шлюх", "дроч", "долбоеб", "уебок", "уебан",
    # «хует/хуев/хуйн/хуяр» НЕ подстрокой: их ловит _RU_WORDSTART по началу слова,
    # а как подстрока «хует» давала ложное на «страхует/застрахует/подстрахует».
    "нахуй", "похуй", "нихуя",
    "заеб", "доеб", "наеб", "проеб", "разъеб", "въеб", "отъеб", "подъеб",
    "ъеб", "ьеб", "ебанут", "ебальник",
)
# Шаблоны с началом слова (для коротких корней, где иначе были бы ложные:
# «страхуй», «психуй», «корабля», «барсук» — НЕ должны блокироваться)
_RU_WORDSTART = re.compile(
    r"\b(?:[ао]?ху[еийя]|бля(?![а-яё])|бля[дт]|еб(?:[аеиоуы]|н)|сука|суки|сучар|чмо(?![кмн]))\w*",
    re.IGNORECASE)
# Английские/транслит корни — проверяются по латинским токенам (см. шаг 1).
# Набор курируемый (без ложных на ebola/hue/suki); транслит-мат — «догоняющая» защита.
_EN_ROOTS = ("fuck", "fck", "shit", "bitch", "cunt", "nigg", "asshole",
             "blyad", "blya", "pizd", "pidor", "pidar", "suka", "cyka",
             "mudak", "mudil", "gandon", "dolboeb", "zaebal", "xyi", "hui", "huy", "ebat",
             "shluh", "shlyu", "droch", "zalup", "ebanut", "padonok", "gondon", "pedik")


def _has_profanity(text):
    raw = str(text or "").lower().replace("ё", "е")
    # 1) английский/транслит — ПО ЛАТИНСКИМ ТОКЕНАМ (не склеиваем соседние слова,
    #    иначе «notably angry» дало бы «blya»). Повторы букв схлопываем.
    for tok in re.findall(r"[a-z]+", raw):
        c = re.sub(r"(.)\1+", r"\1", tok)
        # проверяем И сырой токен, И схлопнутый: удвоения важны для «nigg»/«asshole»,
        # а схлопывание ловит растянутые «fuuuck».
        if any(r in tok or r in c for r in _EN_ROOTS):
            return True
    # 1.5) латинская разрядка ПОДРЯД идущими одиночными буквами: «f u c k», «x y i».
    lrun = []
    def _bad_lrun(chars):
        if len(chars) < 3:
            return False
        j = re.sub(r"(.)\1+", r"\1", "".join(chars))
        return any(r in j for r in _EN_ROOTS)
    for tok in re.findall(r"[a-z]+", raw):
        if len(tok) == 1:
            lrun.append(tok)
        else:
            if _bad_lrun(lrun):
                return True
            lrun = []
    if _bad_lrun(lrun):
        return True
    # 2) перевод двойников (латиница/цифры) в кириллицу, разбивка на токены.
    #    В «русские» проходы пускаем ТОЛЬКО токены, где уже есть кириллица (реальная
    #    обфускация всегда оставляет хотя бы одну: «xyй», «6лядь», «n@xуй»). Чисто
    #    латинские слова (ebola, hue, suki) покрыты _EN_ROOTS выше и не должны ложно
    #    матчиться после перевода двойников. Пробелы сохраняем — не склеиваем слова.
    raw_ru = " ".join(tok if re.search(r"[а-я]", tok) else " " for tok in raw.split())
    t = raw_ru.translate(_LOOKALIKE)
    tokens = re.sub(r"[^а-я0-9]+", " ", t).strip()
    if _RU_WORDSTART.search(tokens) or any(r in tokens for r in _RU_ROOTS):
        return True
    # 2.5) разделители ВНУТРИ слова: «ху.й», «пи-зда», «су_ка», «х1у1й». Убираем все
    #      небуквенные знаки (в т.ч. цифры — двойники уже переведены в буквы на шаге 2),
    #      кроме пробелов, чтобы НЕ склеивать соседние слова.
    for tok in re.sub(r"[^а-я\s]+", "", t).split():
        if _RU_WORDSTART.search(tok) or any(r in tok for r in _RU_ROOTS):
            return True
    # 3) обфускация ВНУТРИ слова: повторы букв («хуууй»→«хуй») — по каждому токену
    #    отдельно, без склейки соседних слов.
    for tok in tokens.split():
        c = re.sub(r"(.)\1+", r"\1", tok)
        if c != tok and (_RU_WORDSTART.search(c) or any(r in c for r in _RU_ROOTS)):
            return True
    # 4) разрядка ПОДРЯД идущими одиночными буквами: «х у й», «п.и.з.д.а».
    #    Склеиваем только РЯД соседних одиночных букв, а не разбросанные по фразе
    #    (иначе предлоги «с … у … к … а» дали бы ложное «сука»).
    run = []
    def _bad_run(chars):
        if len(chars) < 3:
            return False
        j = re.sub(r"(.)\1+", r"\1", "".join(chars))
        return bool(_RU_WORDSTART.search(j) or any(r in j for r in _RU_ROOTS))
    words = re.findall(r"[а-я]+", t)
    for tok in words:
        if len(tok) == 1 and tok.isalpha():
            run.append(tok)
        else:
            if _bad_run(run):
                return True
            run = []
    if _bad_run(run):
        return True
    # 5) короткое слово, разбитое ОДНИМ пробелом на два коротких куска: «ху й»,
    #    «су ка», «пиз да». Склеиваем только СОСЕДНИЕ короткие (≤3 буквы) токены —
    #    длинные слова («хлеб ляжет», «цех уехал») не трогаем, поэтому без ложных.
    for i in range(len(words) - 1):
        a, b = words[i], words[i + 1]
        if len(a) <= 3 and len(b) <= 3:
            j = a + b
            if _RU_WORDSTART.search(j) or any(r in j for r in _RU_ROOTS):
                return True
    return False


# Нарушения и мут (в памяти процесса; при -w 1 этого достаточно)
_VIOLATIONS = {}       # uid -> [моменты нарушений]
_MUTED = {}            # uid -> monotonic-время окончания мута
_MSG_HITS = {}         # uid -> [моменты отправок]
_LAST_TEXT = {}        # uid -> (нормализованный текст, момент)
VIOL_WINDOW, VIOL_MAX, MUTE_SECONDS = 300, 3, 600


def _mute_left(uid):
    until = _MUTED.get(uid, 0)
    left = until - time.monotonic()
    if left <= 0:
        _MUTED.pop(uid, None)
        return 0
    return int(left)


def _register_violation(uid):
    now = time.monotonic()
    hits = [t for t in _VIOLATIONS.get(uid, []) if now - t < VIOL_WINDOW]
    hits.append(now)
    _VIOLATIONS[uid] = hits
    if len(hits) >= VIOL_MAX:
        _MUTED[uid] = now + MUTE_SECONDS
        _VIOLATIONS[uid] = []


def _content_check(u, text, rate=True):
    """Проверка текста перед публикацией. Возвращает (код, ошибка) или None."""
    uid = u["id"]
    left = _mute_left(uid)
    if left:
        return 429, f"Вы временно заблокированы за нарушения (ещё {left} сек)"
    if len(text) > MSG_MAX_LEN:
        return 400, f"Слишком длинное сообщение (максимум {MSG_MAX_LEN} символов)"
    if _has_profanity(text):
        _register_violation(uid)
        return 400, "Сообщение отклонено: недопустимая лексика"
    if rate:
        if not _rate_ok(_MSG_HITS, uid, 30, 8):
            _register_violation(uid)
            return 429, "Слишком часто. Подождите несколько секунд"
        norm = re.sub(r"\s+", " ", text.lower()).strip()
        prev = _LAST_TEXT.get(uid)
        if prev and prev[0] == norm and time.monotonic() - prev[1] < 60:
            _register_violation(uid)
            return 400, "Повтор сообщения — не отправлено"
        _rate_hit(_MSG_HITS, uid)
        _LAST_TEXT[uid] = (norm, time.monotonic())
    return None


# ---------------------------------------------------------------------------
# NIMPH: серверный сбор полного HTML-среза сети зондов.
# Официальный точечный API не отдаёт таблицу показаний, поэтому повторяем способ
# страницы /monitor: забираем HTML-карточки и читаем документированные data-*.
# ---------------------------------------------------------------------------
class _NimphCardsParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.sensors = []
        self.current = None

    @staticmethod
    def _number(value, integer=False):
        if value in (None, ""):
            return None
        try:
            return int(value) if integer else float(value)
        except (TypeError, ValueError, OverflowError):
            return None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = set((attrs.get("class") or "").split())
        if tag == "article" and "nimph-sensor" in classes:
            sensor_id = str(attrs.get("data-sensor-id") or "").strip().upper()
            sensor_type = str(attrs.get("data-sensor-type") or "").strip().lower()
            if not re.match(r"^[A-Z][A-Z0-9_-]{1,15}$", sensor_id):
                self.current = None
                return
            self.current = {
                "sensor_id": sensor_id,
                "sensor_type": sensor_type if sensor_type in ("water", "soil", "air") else "unknown",
                "latitude": self._number(attrs.get("data-latitude")),
                "longitude": self._number(attrs.get("data-longitude")),
                "last_timestamp": attrs.get("data-last-timestamp") or None,
                "packet_id": self._number(attrs.get("data-packet-id"), integer=True),
                "battery_percent": self._number(attrs.get("data-battery")),
                "parameters": {},
            }
        elif tag == "tr" and self.current is not None and "nimph-reading" in classes:
            parameter = str(attrs.get("data-parameter") or "").strip()
            if not parameter or len(parameter) > 40:
                return
            self.current["parameters"][parameter] = {
                "value": self._number(attrs.get("data-value")),
                "unit": str(attrs.get("data-unit") or "")[:20],
                "quality_flag": (str(attrs.get("data-quality") or "").upper() or None),
                "timestamp": str(attrs.get("data-timestamp") or "")[:32],
                "primary": str(attrs.get("data-primary") or "").lower() == "true",
            }

    def handle_endtag(self, tag):
        if tag == "article" and self.current is not None:
            if self.current["latitude"] is not None and self.current["longitude"] is not None:
                self.sensors.append(self.current)
            self.current = None


_SENSOR_CACHE = {"payload": None, "at": 0.0}
_SENSOR_CACHE_LOCK = threading.Lock()
_SENSOR_REFRESH_HITS = {}
SENSOR_CACHE_SECONDS = 55
SENSOR_SNAPSHOT_SECONDS = 300
SENSOR_SNAPSHOT_KEEP = 96


def _nimph_get(path, timeout=20, min_bytes=0):
    last_error = None
    for attempt in range(3):
        req = urllib.request.Request(NIMPH_BASE_URL + path, method="GET", headers={
            "Accept": "application/json,text/html;q=0.9,*/*;q=0.5",
            "User-Agent": "GreenNet-Crisis/2026 sensor-map",
            "Connection": "close",
        })
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                raw = response.read(2 * 1024 * 1024 + 1)
                if len(raw) > 2 * 1024 * 1024:
                    raise ValueError("Ответ NIMPH слишком большой")
                if len(raw) < min_bytes:
                    raise ValueError(f"Неполный ответ NIMPH: {len(raw)} байт")
                return raw.decode("utf-8", "replace")
        except Exception as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(0.35 * (attempt + 1))
    raise last_error


def _fetch_nimph_snapshot():
    errors = []
    try:
        meta = json.loads(_nimph_get("/api/v1/meta", timeout=10))
    except Exception as exc:
        meta = {}
        errors.append("meta: " + str(exc)[:100])
    types = ("water", "soil", "air")
    fragments = {}
    # Типы независимы: один зависший HTML-ответ не должен скрывать остальные станции.
    with ThreadPoolExecutor(max_workers=3, thread_name_prefix="nimph") as pool:
        jobs = {pool.submit(_nimph_get, f"/api/v1/monitor/cards?type={kind}", 15, 100_000): kind for kind in types}
        for job in as_completed(jobs):
            kind = jobs[job]
            try:
                fragments[kind] = job.result()
            except Exception as exc:
                errors.append(kind + ": " + str(exc)[:100])
    by_id = {}
    for kind in types:
        if kind not in fragments:
            continue
        parser = _NimphCardsParser()
        parser.feed(fragments[kind])
        if len(parser.sensors) < 40:
            errors.append(f"{kind}: HTML содержит только {len(parser.sensors)} полных карточек")
        for sensor in parser.sensors:
            by_id[sensor["sensor_id"]] = sensor

    # Реестр маленький и обычно отвечает даже тогда, когда большой HTML-фрагмент
    # одного типа завис. Он гарантирует, что карта всё равно покажет все координаты.
    try:
        registry = json.loads(_nimph_get("/api/v1/stations", timeout=10))
        for station in registry.get("data", []):
            sensor_id = str(station.get("sensor_id") or "").strip().upper()
            if not re.match(r"^[A-Z][A-Z0-9_-]{1,15}$", sensor_id) or sensor_id in by_id:
                continue
            by_id[sensor_id] = {
                "sensor_id": sensor_id,
                "sensor_type": str(station.get("sensor_type") or "unknown").lower(),
                "latitude": _NimphCardsParser._number(station.get("latitude")),
                "longitude": _NimphCardsParser._number(station.get("longitude")),
                "last_timestamp": None,
                "packet_id": None,
                "battery_percent": None,
                "parameters": {},
            }
    except Exception as exc:
        errors.append("stations: " + str(exc)[:100])

    sensors = sorted(by_id.values(), key=lambda item: item["sensor_id"])
    if not sensors:
        raise ValueError("NIMPH вернул пустой список зондов")
    return {
        "source": NIMPH_BASE_URL,
        "mission_now": str(meta.get("mission_now") or ""),
        "deployment_start": str(meta.get("deployment_start") or ""),
        "collected_at": now_iso(),
        "count": len(sensors),
        "parameters": meta.get("parameters") if isinstance(meta.get("parameters"), list) else [],
        "primary_by_type": meta.get("primary_by_type") if isinstance(meta.get("primary_by_type"), dict) else {},
        "sensors": sensors,
        "stale": False,
        "partial": bool(errors),
        "source_error": "; ".join(errors)[:300] if errors else None,
    }


def _persist_sensor_snapshot(db, payload):
    latest = db.execute("SELECT collected_at FROM sensor_snapshots ORDER BY id DESC LIMIT 1").fetchone()
    if latest:
        try:
            age = datetime.now(MSK) - datetime.fromisoformat(latest["collected_at"])
            if age.total_seconds() < SENSOR_SNAPSHOT_SECONDS:
                return
        except (TypeError, ValueError):
            pass
    packed = gzip.compress(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), 6)
    db.execute(
        "INSERT INTO sensor_snapshots(mission_time,collected_at,station_count,payload_gzip) VALUES(?,?,?,?)",
        (payload.get("mission_now") or "", payload["collected_at"], payload["count"], sqlite3.Binary(packed)))
    db.execute(
        "DELETE FROM sensor_snapshots WHERE id NOT IN "
        "(SELECT id FROM sensor_snapshots ORDER BY id DESC LIMIT ?)", (SENSOR_SNAPSHOT_KEEP,))
    db.commit()  # отдельный набор данных: не заставляем основной /api/state перерисовываться


def _load_sensor_snapshot(db):
    row = db.execute("SELECT payload_gzip FROM sensor_snapshots ORDER BY id DESC LIMIT 1").fetchone()
    if not row:
        return None
    try:
        return json.loads(gzip.decompress(bytes(row["payload_gzip"])).decode("utf-8"))
    except Exception:
        return None


def _get_sensor_snapshot(force=False):
    now_mono = time.monotonic()
    cached = _SENSOR_CACHE.get("payload")
    if cached is not None and not force and now_mono - _SENSOR_CACHE["at"] < SENSOR_CACHE_SECONDS:
        return cached
    with _SENSOR_CACHE_LOCK:
        now_mono = time.monotonic()
        cached = _SENSOR_CACHE.get("payload")
        if cached is not None and not force and now_mono - _SENSOR_CACHE["at"] < SENSOR_CACHE_SECONDS:
            return cached
        try:
            payload = _fetch_nimph_snapshot()
            db = get_db()
            if not payload.get("partial") or _load_sensor_snapshot(db) is None:
                _persist_sensor_snapshot(db, payload)
        except Exception as exc:
            payload = _load_sensor_snapshot(get_db())
            if payload is None:
                raise RuntimeError("Не удалось получить данные NIMPH") from exc
            payload = dict(payload)
            payload["stale"] = True
            payload["source_error"] = str(exc)[:180]
        _SENSOR_CACHE["payload"] = payload
        _SENSOR_CACHE["at"] = time.monotonic()
        return payload


# ---------------------------------------------------------------------------
# Портал-адаптер «Летово Игра» (только при AUTH_MODE=portal). Всё server-to-server.
# ---------------------------------------------------------------------------
def _portal_get(path, token=None, timeout=6):
    """GET к API портала. Возвращает (status, json|None); (0, None) — портал недоступен."""
    if not PORTAL_BASE_URL:
        return 0, None
    req = urllib.request.Request(PORTAL_BASE_URL + path, method="GET")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    if PORTAL_API_KEY:
        req.add_header("X-Api-Key", PORTAL_API_KEY)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace")
            try:
                return r.status, (json.loads(body) if body else {})
            except Exception:
                return r.status, {"_raw": body}
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception:
        return 0, None   # сеть/таймаут → откат на локальный вход


def _jwt_username(token):
    """Логин из payload JWT БЕЗ проверки подписи (подпись проверяет портал через
    /auth/amiauthed). Пробуем частые поля."""
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return None
        pad = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(pad).decode("utf-8", "replace"))
        for k in ("username", "login", "sub", "user", "name", "preferred_username"):
            if payload.get(k):
                return str(payload[k])
    except Exception:
        pass
    return None


def _map_portal_role(roles):
    """Роли портала (список/строка/словарь) → наш id роли (макс. по рангу; иначе observer)."""
    if isinstance(roles, str):
        roles = [roles]
    elif isinstance(roles, dict):
        roles = list(roles.values())
    best, best_rank = "observer", -1
    for r in (roles or []):
        key = str(r).strip().lower()
        our = PORTAL_ROLE_MAP.get(key) or next((v for k, v in PORTAL_ROLE_MAP.items() if k in key), None)
        if our and ROLES.get(our, {}).get("rank", 0) > best_rank:
            best, best_rank = our, ROLES[our]["rank"]
    return best


def _map_portal_dept(dept):
    if dept is None:
        return "Проект 11"
    key = str(dept).strip().lower()
    if key in PORTAL_DEPT_MAP:
        return PORTAL_DEPT_MAP[key]
    return next((d for d in DEPTS if d.lower() == key), "Проект 11")


@app.post("/api/auth/portal")
def api_auth_portal():
    """Вход через портал: валидируем Bearer-токен на портале, тянем профиль/роли,
    создаём/обновляем пользователя у нас и открываем сессию. Свой вход — как fallback."""
    if not PORTAL_ENABLED:
        return jsonify(error="Вход через портал не настроен"), 400
    data = _body()
    token = str(data.get("token") or "").strip()
    if not token:
        auth_h = request.headers.get("Authorization", "")
        if auth_h.lower().startswith("bearer "):
            token = auth_h[7:].strip()
    if not token:
        return jsonify(error="Нет токена портала"), 400
    st, _ = _portal_get("/auth/amiauthed", token=token)
    if st == 0:
        return jsonify(error="Портал недоступен — войдите обычным способом"), 503
    if st != 200:
        return jsonify(error="Токен портала недействителен"), 401
    username = _jwt_username(token)
    if not username:
        return jsonify(error="Не удалось определить пользователя из токена портала"), 400
    username = username.strip().lower()
    q = urllib.parse.quote(username)
    _, full = _portal_get("/user/full/" + q, token=token)
    _, rolesr = _portal_get("/user/roles/" + q, token=token)
    full = full if isinstance(full, dict) else {}
    name = str(full.get("name") or full.get("fullname") or full.get("display_name") or username)[:60]
    dept_raw = full.get("department", full.get("dept"))
    if isinstance(rolesr, dict):
        roles = rolesr.get("roles") or rolesr.get("data") or list(rolesr.values())
    elif isinstance(rolesr, list):
        roles = rolesr
    else:
        roles = full.get("roles") or full.get("role")
    our_role = _map_portal_role(roles)
    our_dept = _map_portal_dept(dept_raw)
    db = get_db()
    # роль портала применяется при ПЕРВОМ входе; далее роль ведёт админ (защита от
    # ошибок маппинга). upsert не понижает/не меняет роль существующего.
    row = upsert_external_user(db, username, name, dept=our_dept, role=our_role)
    add_log(db, name, f"<b>вошёл(ла) через портал</b> · {h(our_dept)}")
    _commit(db)
    session.clear()
    session["uid"] = row["id"]
    session.permanent = True
    return jsonify(user=user_public(row), caps=caps_for(row["role"]), demo=DEMO_MODE, portal=True)


# ---------------------------------------------------------------------------
# API: аутентификация
# ---------------------------------------------------------------------------
@app.post("/api/login")
def api_login():
    data = _body()
    # приводим к строке — иначе нестроковый JSON (username: 123) уронил бы сервер
    username = str(data.get("username") or "").strip().lower()
    password = str(data.get("password") or "")
    rl_key = f"{username}|{_client_ip()}"
    if not _rate_ok(_LOGIN_FAILS, rl_key, LOGIN_WINDOW, LOGIN_MAX):
        return jsonify(error="Слишком много попыток входа. Подождите несколько минут."), 429
    row = get_db().execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    if not row or not check_password_hash(row["password_hash"], password):
        _rate_hit(_LOGIN_FAILS, rl_key)
        return jsonify(error="Неверный логин или пароль"), 401
    if not row["active"]:
        return jsonify(error="Аккаунт отключён администратором"), 403
    _LOGIN_FAILS.pop(rl_key, None)   # успешный вход — сбрасываем счётчик неудач
    session.clear()
    session["uid"] = row["id"]
    session.permanent = True
    return jsonify(user=user_public(row), caps=caps_for(row["role"]), demo=DEMO_MODE)


@app.post("/api/register")
def api_register():
    """Самостоятельная регистрация. Роль всегда 'observer' — повысить может только админ."""
    if not ALLOW_REGISTRATION:
        return jsonify(error="Регистрация отключена администратором"), 403
    ip = _client_ip()
    if not _rate_ok(_REG_HITS, ip, REG_WINDOW, REG_MAX):
        return jsonify(error="Слишком много регистраций. Подождите немного."), 429
    _rate_hit(_REG_HITS, ip)
    db = get_db()
    data = _body()
    username = str(data.get("username") or "").strip().lower()
    first_name, last_name, name = _person_from_data(data)
    password = str(data.get("password") or "")
    dept = data.get("dept") if data.get("dept") in DEPTS else "Проект 11"
    if not data.get("accept_terms"):
        return jsonify(error="Нужно принять пользовательское соглашение"), 400
    if not re.match(r"^[a-z0-9_.\-]{3,20}$", username):
        return jsonify(error="Логин: 3–20 символов, латиница, цифры, . _ -"), 400
    if not first_name or not last_name:
        return jsonify(error="Введите имя и фамилию"), 400
    if len(first_name) > 40 or len(last_name) > 60 or len(name) > 100:
        return jsonify(error="Слишком длинное имя или фамилия"), 400
    if _has_profanity(name) or _has_profanity(username):
        return jsonify(error="Имя или логин содержит недопустимую лексику"), 400
    if len(password) < 6:
        return jsonify(error="Пароль минимум 6 символов"), 400
    if db.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
        return jsonify(error="Такой логин уже занят"), 409
    role = "observer"   # минимальная роль; изменить может только администратор
    try:
        cur = db.execute(
            "INSERT INTO users(username,name,first_name,last_name,password_hash,role,dept,active,created_at,terms_at)"
            " VALUES(?,?,?,?,?,?,?,1,?,?)",
            (username, name, first_name, last_name, generate_password_hash(password),
             role, dept, now_iso(), now_iso()))
    except sqlite3.IntegrityError:
        db.rollback()
        return jsonify(error="Такой логин уже занят"), 409
    add_log(db, name, f"<b>зарегистрировался(ась)</b> как {h(dept)} · роль назначит штаб")
    _commit(db)
    sync_users_csv(db)
    # авто-вход сразу после регистрации
    session.clear()
    session["uid"] = cur.lastrowid
    session.permanent = True
    row = db.execute("SELECT * FROM users WHERE id=?", (cur.lastrowid,)).fetchone()
    return jsonify(user=user_public(row), caps=caps_for(role), demo=DEMO_MODE, registered=True)


@app.post("/api/logout")
def api_logout():
    session.clear()
    return jsonify(ok=True)


@app.get("/api/me")
def api_me():
    u = current_user()
    if not u:
        return jsonify(user=None, demo=DEMO_MODE, registration=ALLOW_REGISTRATION,
                       auth_mode=AUTH_MODE, portal=PORTAL_ENABLED), 200
    return jsonify(user=user_public(u), caps=caps_for(u["role"]), demo=DEMO_MODE,
                   registration=ALLOW_REGISTRATION, auth_mode=AUTH_MODE, portal=PORTAL_ENABLED)


# ---------------------------------------------------------------------------
# API: единое состояние
# ---------------------------------------------------------------------------
@app.get("/api/state")
@login_required
def api_state():
    u = current_user()
    # Быстрый путь: версия данных не изменилась → мгновенный 304 БЕЗ запросов к базе
    # и без сериализации. При 300 игроках с поллингом раз в 5с это ~99% всех ответов.
    etag_val = f"v{_DATA_VERSION}-{u['id']}-{u['role']}"
    if etag_val in (request.headers.get("If-None-Match") or ""):
        resp = app.response_class(status=304)
        resp.headers["ETag"] = f'"{etag_val}"'
        resp.headers["Cache-Control"] = "private, no-cache"
        return resp
    db = get_db()
    st = {r["key"]: r["value"] for r in db.execute("SELECT * FROM settings").fetchall()}
    roster = [user_public(r) for r in db.execute(
        "SELECT * FROM users ORDER BY (role='admin') DESC").fetchall()]
    group_rows = db.execute(
        "SELECT g.*,u.name creator_name FROM chat_groups g "
        "JOIN chat_group_members mine ON mine.group_id=g.id AND mine.user_id=? "
        "LEFT JOIN users u ON u.id=g.created_by ORDER BY g.id DESC", (u["id"],)).fetchall()
    chat_groups = []
    for group in group_rows:
        members = db.execute(
            "SELECT u.id,u.name,u.role FROM users u JOIN chat_group_members m ON m.user_id=u.id "
            "WHERE m.group_id=? AND u.active=1 ORDER BY u.name", (group["id"],)).fetchall()
        chat_groups.append({
            "id": group["id"], "name": group["name"], "description": group["description"] or "",
            "created_by": group["created_by"], "creator": group["creator_name"] or "Удалённый пользователь",
            "ts": group["created_at"],
            "members": [{"id": m["id"], "name": m["name"], "role": m["role"],
                         "short": ROLES.get(m["role"], {}).get("short", "?")} for m in members],
        })
    group_messages = [group_message_public(r) for r in db.execute(
        "SELECT * FROM (SELECT msg.* FROM chat_group_messages msg "
        "JOIN chat_group_members mine ON mine.group_id=msg.group_id "
        "WHERE mine.user_id=? ORDER BY msg.id DESC LIMIT 1000) ORDER BY id", (u["id"],)).fetchall()]
    payload = {
        "me": user_public(u),
        "version": _DATA_VERSION,
        "caps": caps_for(u["role"]),
        "roles": ROLES, "role_order": ROLE_ORDER,
        "msg_types": MSG_TYPES, "depts": DEPTS, "dept_info": DEPT_INFO,
        "sched_kinds": SCHED_KINDS, "news_levels": NEWS_LEVELS,
        "channels": [chan_public(r) for r in db.execute(
            "SELECT * FROM (SELECT * FROM channel_messages ORDER BY id DESC LIMIT 500) ORDER BY id").fetchall()],
        "chat_groups": chat_groups,
        "group_messages": group_messages,
        "currency": {"name": "энеоин", "short": "ЭН"},
        "accounts": [account_public(r) for r in db.execute(
            "SELECT * FROM accounts ORDER BY balance DESC").fetchall()],
        "transactions": [tx_public(r) for r in db.execute(
            "SELECT * FROM transactions ORDER BY id DESC LIMIT 50").fetchall()],
        "settings": st,
        "news": [news_public(r) for r in db.execute(
            "SELECT * FROM news ORDER BY pinned DESC, id DESC").fetchall()],
        "schedule": [sched_public(r) for r in db.execute(
            "SELECT * FROM schedule ORDER BY at_time, id").fetchall()],
        "roster": roster if caps_for(u["role"])["manage"] else [
            {k: r[k] for k in ("id", "name", "role", "role_label", "short", "dept")}
            for r in roster if r["active"]
        ],
        "messages": [msg_public(r) for r in db.execute(
            "SELECT * FROM (SELECT * FROM messages ORDER BY id DESC LIMIT 500) ORDER BY id").fetchall()],
        "tasks": [task_public(r) for r in db.execute(
            "SELECT * FROM (SELECT * FROM tasks ORDER BY id DESC LIMIT 500) ORDER BY id").fetchall()],
        "knowledge": [kb_public(r) for r in db.execute(
            "SELECT * FROM (SELECT * FROM knowledge ORDER BY id DESC LIMIT 500) ORDER BY id").fetchall()],
        "map_objects": [obj_public(r) for r in db.execute(
            "SELECT * FROM (SELECT * FROM map_objects ORDER BY id DESC LIMIT 400) ORDER BY id").fetchall()],
        "log": [log_public(r) for r in db.execute(
            "SELECT * FROM (SELECT * FROM logs ORDER BY id DESC LIMIT 300) ORDER BY id").fetchall()],
    }
    # Полный ответ с версионным ETag (быстрый 304-путь — в начале функции).
    # server_time в тело не кладём (см. /api/time), иначе ETag менялся бы каждую секунду.
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    resp = app.response_class(body, mimetype="application/json")
    resp.headers["ETag"] = f'"{etag_val}"'
    resp.headers["Cache-Control"] = "private, no-cache"   # хранить, но перепроверять каждый раз
    return resp


# ---------------------------------------------------------------------------
# API: сообщения
# ---------------------------------------------------------------------------
@app.post("/api/messages")
@login_required
def api_post_message():
    u = current_user()
    db = get_db()
    data = _body()
    mtype = data.get("type")
    text = str(data.get("text") or "").strip()
    if not isinstance(mtype, str) or mtype not in MSG_TYPES:
        return jsonify(error="Неизвестный тип сообщения"), 400
    if not text:
        return jsonify(error="Пустое сообщение"), 400
    bad = _content_check(u, text)
    if bad:
        return jsonify(error=bad[1]), bad[0]
    post_cap = caps_for(u["role"])["post"]
    if post_cap == "none":
        return jsonify(error="Ваша роль не может отправлять сообщения"), 403
    if post_cap == "basic" and not MSG_TYPES[mtype]["basic"]:
        return jsonify(error="Ваша роль может отправлять только «Новый очаг» и «Требуется помощь»"), 403

    coords_x = coords_y = sev = None
    if mtype == "focus":
        # авто-размещение нового очага на карте
        cnt = db.execute("SELECT COUNT(*) c FROM map_objects").fetchone()["c"]
        coords_x = 25 + (cnt * 13) % 55
        coords_y = 35 + (cnt * 11) % 45
        sev = "medium"

    cur = db.execute(
        "INSERT INTO messages(type,author_id,author_name,dept,text,verify,coords_x,coords_y,sev,created_at)"
        " VALUES(?,?,?,?,?, 'unverified', ?,?,?,?)",
        (mtype, u["id"], u["name"], u["dept"], text, coords_x, coords_y, sev, now_iso()))
    mid = cur.lastrowid

    if mtype == "focus":
        db.execute("INSERT INTO map_objects(kind,x,y,label,sev,src_msg) VALUES('focus',?,?,?,?,?)",
                   (coords_x, coords_y, "Очаг: " + text[:16].strip(), sev, mid))
        db.execute(
            "INSERT INTO knowledge(type,title,where_txt,who,body,verify,src_msg,created_at)"
            " VALUES('focus',?,?,?,?, 'unverified', ?,?)",
            ("Очаг: " + text[:40], u["dept"] + " · координаты уточняются",
             f"{u['name']} ({u['dept']})", text, mid, now_iso()))

    add_log(db, u["name"], f"сообщил(а): <b>{MSG_TYPES[mtype]['label']}</b>")
    _commit(db)
    return jsonify(id=mid, ok=True)


@app.post("/api/messages/<int:mid>/verify")
@require(lambda c, u: c["verify"])
def api_verify_message(mid):
    u = current_user()
    db = get_db()
    data = _body()
    ok = bool(data.get("ok"))
    row = db.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone()
    if not row:
        return jsonify(error="Сообщение не найдено"), 404
    new_state = "verified" if ok else "rejected"
    # UPDATE — авторитетная точка: он держит write-lock, поэтому параллельное удаление
    # сообщения сериализуется. Если сообщение уже удалили (rowcount 0) — выходим ДО
    # вставки в базу знаний, иначе осталась бы «осиротевшая» подтверждённая запись.
    cur = db.execute("UPDATE messages SET verify=?, verified_by=? WHERE id=?", (new_state, u["name"], mid))
    if cur.rowcount == 0:
        return jsonify(error="Сообщение не найдено"), 404
    if ok:
        # подтверждение → отражаем в базе знаний
        kb = db.execute("SELECT * FROM knowledge WHERE src_msg=?", (mid,)).fetchone()
        if kb:
            db.execute("UPDATE knowledge SET verify='verified' WHERE id=?", (kb["id"],))
        elif row["type"] == "research":
            db.execute(
                "INSERT INTO knowledge(type,title,where_txt,who,body,verify,src_msg,created_at)"
                " VALUES('research',?,?,?,?, 'verified', ?,?)",
                (row["text"][:42], row["dept"], f"{row['author_name']} ({row['dept']})",
                 row["text"], mid, now_iso()))
    else:
        # отклонение → убираем ложные данные из базы знаний и с карты (иначе очаг «висит»)
        db.execute("DELETE FROM knowledge WHERE src_msg=?", (mid,))
        db.execute("DELETE FROM map_objects WHERE src_msg=?", (mid,))
    verb = "подтвердил(а)" if ok else "отклонил(а)"
    add_log(db, u["name"], f"<b>{verb}</b> сообщение от {h(row['author_name'])}")
    _commit(db)
    return jsonify(ok=True, verify=new_state)


@app.delete("/api/messages/<int:mid>")
@require(lambda c, u: c["decide"])
def api_delete_message(mid):
    """Модерация ленты: удалить сообщение (штаб/админ). Каскадом чистим базу знаний и карту."""
    u = current_user()
    db = get_db()
    row = db.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone()
    if not row:
        return jsonify(error="Сообщение не найдено"), 404
    db.execute("DELETE FROM messages WHERE id=?", (mid,))
    db.execute("DELETE FROM knowledge WHERE src_msg=?", (mid,))
    db.execute("DELETE FROM map_objects WHERE src_msg=?", (mid,))
    add_log(db, u["name"], f"<b>удалил(а)</b> сообщение от {h(row['author_name'])} (модерация)")
    _commit(db)
    return jsonify(ok=True)


# ---------------------------------------------------------------------------
# API: чаты департаментов (писать могут только главы департаментов и штаб)
# ---------------------------------------------------------------------------
@app.post("/api/channels")
@require(lambda c, u: c["chat"])
def api_post_channel():
    u = current_user()
    db = get_db()
    data = _body()
    dept = data.get("dept")
    text = str(data.get("text") or "").strip()
    if dept not in DEPTS:
        return jsonify(error="Неизвестный департамент"), 400
    if not text:
        return jsonify(error="Пустое сообщение"), 400
    bad = _content_check(u, text)
    if bad:
        return jsonify(error=bad[1]), bad[0]
    cur = db.execute(
        "INSERT INTO channel_messages(dept,author_id,author_name,author_role,text,created_at)"
        " VALUES(?,?,?,?,?,?)",
        (dept, u["id"], u["name"], u["role"], text, now_iso()))
    add_log(db, u["name"], f"написал(а) в чат <b>{h(dept)}</b>")
    _commit(db)
    return jsonify(id=cur.lastrowid, ok=True)


@app.delete("/api/channels/<int:cid>")
@login_required
def api_delete_channel(cid):
    """Модерация чата департамента: штаб/админ — любой чат, глава — только свой департамент."""
    u = current_user()
    db = get_db()
    row = db.execute("SELECT * FROM channel_messages WHERE id=?", (cid,)).fetchone()
    if not row:
        return jsonify(error="Сообщение не найдено"), 404
    caps = caps_for(u["role"])
    allowed = caps["decide"] or (u["role"] == "dept_head" and row["dept"] == u["dept"])
    if not allowed:
        return jsonify(error="Модерировать этот чат могут штаб или глава департамента"), 403
    db.execute("DELETE FROM channel_messages WHERE id=?", (cid,))
    add_log(db, u["name"], f"<b>удалил(а)</b> сообщение в чате <b>{h(row['dept'])}</b> (модерация)")
    _commit(db)
    return jsonify(ok=True)


# ---------------------------------------------------------------------------
# API: создаваемые групповые чаты. Личных диалогов намеренно нет.
# В каждой комнате не менее трёх участников, включая создателя.
# ---------------------------------------------------------------------------
def _chat_group_for_member(db, gid, uid):
    return db.execute(
        "SELECT g.* FROM chat_groups g JOIN chat_group_members m ON m.group_id=g.id "
        "WHERE g.id=? AND m.user_id=?", (gid, uid)).fetchone()


@app.post("/api/chat/groups")
@login_required
def api_create_chat_group():
    u = current_user()
    db = get_db()
    data = _body()
    name = str(data.get("name") or "").strip()
    description = str(data.get("description") or "").strip()
    raw_ids = data.get("member_ids")
    if len(name) < 3 or len(name) > 50:
        return jsonify(error="Название группы: от 3 до 50 символов"), 400
    if len(description) > 180:
        return jsonify(error="Описание слишком длинное (максимум 180 символов)"), 400
    if _has_profanity(name) or _has_profanity(description):
        return jsonify(error="Название или описание содержит недопустимую лексику"), 400
    if not isinstance(raw_ids, list) or len(raw_ids) > 200:
        return jsonify(error="Выберите участников группы"), 400
    try:
        member_ids = {int(x) for x in raw_ids}
    except (TypeError, ValueError, OverflowError):
        return jsonify(error="Некорректный список участников"), 400
    member_ids.add(u["id"])
    if len(member_ids) < 3:
        return jsonify(error="В групповой комнате должно быть минимум 3 участника"), 400
    marks = ",".join("?" for _ in member_ids)
    active_ids = {r["id"] for r in db.execute(
        f"SELECT id FROM users WHERE active=1 AND id IN ({marks})", tuple(member_ids)).fetchall()}
    if active_ids != member_ids:
        return jsonify(error="Один из выбранных участников недоступен"), 400
    cur = db.execute(
        "INSERT INTO chat_groups(name,description,created_by,created_at) VALUES(?,?,?,?)",
        (name, description, u["id"], now_iso()))
    gid = cur.lastrowid
    joined_at = now_iso()
    db.executemany(
        "INSERT INTO chat_group_members(group_id,user_id,joined_at) VALUES(?,?,?)",
        [(gid, uid, joined_at) for uid in sorted(member_ids)])
    add_log(db, u["name"], f"создал(а) групповой чат <b>{h(name)}</b> · {len(member_ids)} участника(ов)")
    _commit(db)
    return jsonify(id=gid, ok=True)


@app.post("/api/chat/groups/<int:gid>/messages")
@login_required
def api_post_group_message(gid):
    u = current_user()
    db = get_db()
    group = _chat_group_for_member(db, gid, u["id"])
    if not group:
        return jsonify(error="Группа не найдена или вы не являетесь участником"), 404
    text = str(_body().get("text") or "").strip()
    if not text:
        return jsonify(error="Пустое сообщение"), 400
    bad = _content_check(u, text)
    if bad:
        return jsonify(error=bad[1]), bad[0]
    cur = db.execute(
        "INSERT INTO chat_group_messages(group_id,author_id,author_name,text,created_at) VALUES(?,?,?,?,?)",
        (gid, u["id"], u["name"], text, now_iso()))
    _commit(db)
    return jsonify(id=cur.lastrowid, ok=True)


@app.delete("/api/chat/groups/<int:gid>/messages/<int:mid>")
@login_required
def api_delete_group_message(gid, mid):
    u = current_user()
    db = get_db()
    group = _chat_group_for_member(db, gid, u["id"])
    if not group:
        return jsonify(error="Группа не найдена"), 404
    msg = db.execute("SELECT * FROM chat_group_messages WHERE id=? AND group_id=?", (mid, gid)).fetchone()
    if not msg:
        return jsonify(error="Сообщение не найдено"), 404
    if msg["author_id"] != u["id"] and group["created_by"] != u["id"] and u["role"] != "admin":
        return jsonify(error="Удалить сообщение может автор, создатель группы или администратор"), 403
    db.execute("DELETE FROM chat_group_messages WHERE id=?", (mid,))
    _commit(db)
    return jsonify(ok=True)


@app.delete("/api/chat/groups/<int:gid>")
@login_required
def api_delete_chat_group(gid):
    u = current_user()
    db = get_db()
    group = _chat_group_for_member(db, gid, u["id"])
    if not group:
        return jsonify(error="Группа не найдена"), 404
    if group["created_by"] != u["id"] and u["role"] != "admin":
        return jsonify(error="Удалить группу может только её создатель или администратор"), 403
    db.execute("DELETE FROM chat_groups WHERE id=?", (gid,))
    add_log(db, u["name"], f"удалил(а) групповой чат <b>{h(group['name'])}</b>")
    _commit(db)
    return jsonify(ok=True)


# ---------------------------------------------------------------------------
# API: игровая валюта «энеоины» — счета департаментов (штаб/админ управляют)
# ---------------------------------------------------------------------------
@app.post("/api/finance/tx")
@require(lambda c, u: c["finance"])
def api_finance_tx():
    """Перевод между счетами департаментов или эмиссия (выпуск) новых энеоинов.
    Эмиссия (без отправителя) — только администратор. Это игровая валюта."""
    u = current_user()
    db = get_db()
    data = _body()
    to_d = data.get("to")
    from_d = data.get("from") or None
    note = str(data.get("note") or "").strip()[:120]
    amt_raw = data.get("amount")
    if isinstance(amt_raw, bool) or not isinstance(amt_raw, (int, str)):
        return jsonify(error="Сумма должна быть целым числом"), 400
    try:
        amount = int(amt_raw)
    except (TypeError, ValueError):
        return jsonify(error="Сумма должна быть целым числом"), 400
    if amount <= 0:
        return jsonify(error="Сумма должна быть больше нуля"), 400
    if amount > 10_000_000:
        return jsonify(error="Слишком большая сумма"), 400
    if to_d not in DEPTS:
        return jsonify(error="Неизвестный департамент-получатель"), 400
    if from_d is not None and from_d not in DEPTS:
        return jsonify(error="Неизвестный департамент-отправитель"), 400
    if from_d == to_d:
        return jsonify(error="Отправитель и получатель совпадают"), 400

    if from_d is None:
        # эмиссия новых энеоинов — только администратор
        if not caps_for(u["role"])["manage"]:
            return jsonify(error="Выпускать новые энеоины может только администратор"), 403
    else:
        # Атомарное условное списание ОДНИМ оператором: если баланса не хватило,
        # обновится 0 строк. Это закрывает гонку (double-spend) при параллельных
        # переводах — проверка и списание неразделимы, а запись в SQLite сериализуется
        # (busy_timeout ждёт освобождения блокировки). Отдельного SELECT нет намеренно.
        cur = db.execute(
            "UPDATE accounts SET balance = balance - ? WHERE dept = ? AND balance >= ?",
            (amount, from_d, amount))
        if cur.rowcount != 1:
            return jsonify(error="Недостаточно энеоинов на счёте отправителя"), 400

    db.execute("INSERT OR IGNORE INTO accounts(dept,balance) VALUES(?,0)", (to_d,))
    db.execute("UPDATE accounts SET balance=balance+? WHERE dept=?", (amount, to_d))
    db.execute(
        "INSERT INTO transactions(from_dept,to_dept,amount,note,actor,created_at) VALUES(?,?,?,?,?,?)",
        (from_d, to_d, amount, note, u["name"], now_iso()))
    if from_d is None:
        add_log(db, u["name"], f"выпустил(а) <b>{amount} ЭН</b> → {h(to_d)}")
    else:
        add_log(db, u["name"], f"перевёл(а) <b>{amount} ЭН</b>: {h(from_d)} → {h(to_d)}")
    _commit(db)
    return jsonify(ok=True)


# ---------------------------------------------------------------------------
# API: задачи
# ---------------------------------------------------------------------------
@app.post("/api/tasks")
@login_required
def api_create_task():
    u = current_user()
    if caps_for(u["role"])["task_create"] == "none":
        return jsonify(error="Ваша роль не может ставить задачи"), 403
    db = get_db()
    data = _body()
    title = str(data.get("title") or "").strip()
    if not title:
        return jsonify(error="Введите название задачи"), 400
    bad = _content_check(u, title + " " + str(data.get("descr") or ""), rate=False)
    if bad:
        return jsonify(error=bad[1]), bad[0]
    dept = data.get("dept") if data.get("dept") in DEPTS else u["dept"]
    assignee = str(data.get("assignee") or "").strip() or dept
    due = str(data.get("due") or "").strip() or None
    descr = str(data.get("descr") or "").strip()
    cur = db.execute(
        "INSERT INTO tasks(title,descr,dept,assignee,due,status,created_by,created_at)"
        " VALUES(?,?,?,?,?, 'new', ?,?)",
        (title, descr, dept, assignee, due, u["name"], now_iso()))
    add_log(db, u["name"], f"поставил(а) задачу <b>«{h(title[:40])}»</b> → {h(dept)}")
    _commit(db)
    return jsonify(id=cur.lastrowid, ok=True)


@app.patch("/api/tasks/<int:tid>")
@login_required
def api_update_task(tid):
    u = current_user()
    db = get_db()
    data = _body()
    status = data.get("status")
    if status not in ("new", "prog", "done"):
        return jsonify(error="Неверный статус"), 400
    row = db.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
    if not row:
        return jsonify(error="Задача не найдена"), 404
    caps = caps_for(u["role"])
    close = caps["task_close"]
    can_close = close == "all" or (close == "own" and row["dept"] == u["dept"])
    if status == "done" or row["status"] == "done":
        # закрытие ИЛИ переоткрытие закрытой задачи — только у кого есть право закрывать её департамент
        if not can_close:
            return jsonify(error="Нет прав закрывать или переоткрывать эту задачу"), 403
    else:  # перевод new <-> prog по незакрытой задаче
        if caps["task_create"] == "none":
            return jsonify(error="Нет прав менять задачи"), 403
        # ниже штаба/админа — только задачи своего департамента (как при закрытии)
        if close != "all" and row["dept"] != u["dept"]:
            return jsonify(error="Можно менять только задачи своего департамента"), 403
    db.execute("UPDATE tasks SET status=? WHERE id=?", (status, tid))
    verb = {"done": "закрыл(а)", "prog": "взял(а) в работу", "new": "вернул(а) в очередь"}[status]
    add_log(db, u["name"], f"<b>{verb}</b> задачу <b>«{h(row['title'][:40])}»</b>")
    _commit(db)
    return jsonify(ok=True)


# ---------------------------------------------------------------------------
# API: управление пользователями (только админ)
# ---------------------------------------------------------------------------
@app.get("/api/users")
@require(lambda c, u: c["manage"])
def api_users():
    rows = get_db().execute("SELECT * FROM users ORDER BY id").fetchall()
    return jsonify(users=[user_public(r) for r in rows])


@app.get("/api/users.csv")
@require(lambda c, u: c["manage"])
def api_users_csv():
    """Скачать актуальный CSV-датасет профилей без паролей и password_hash."""
    updated = sync_users_csv(get_db())
    if not updated and not os.path.exists(USERS_CSV_PATH):
        return jsonify(error="CSV временно недоступен. Закройте файл в Excel и повторите."), 503
    return send_file(USERS_CSV_PATH, mimetype="text/csv", as_attachment=True,
                     download_name="greennet-users.csv", max_age=0)


@app.post("/api/users")
@require(lambda c, u: c["manage"])
def api_create_user():
    db = get_db()
    u = current_user()
    data = _body()
    username = str(data.get("username") or "").strip().lower()
    first_name, last_name, name = _person_from_data(data)
    password = str(data.get("password") or "")
    role = data.get("role")
    dept = data.get("dept")
    if not username or not first_name or not last_name or not password:
        return jsonify(error="Заполните логин, имя, фамилию и пароль"), 400
    if not re.match(r"^[a-z0-9_.\-]{3,20}$", username):
        return jsonify(error="Логин: 3–20 символов, латиница, цифры, . _ -"), 400
    if len(first_name) > 40 or len(last_name) > 60 or _has_profanity(name):
        return jsonify(error="Проверьте имя и фамилию"), 400
    if not isinstance(role, str) or role not in ROLES:
        return jsonify(error="Неизвестная роль"), 400
    if dept not in DEPTS:
        dept = "Департамент управления"
    if len(password) < 6:
        return jsonify(error="Пароль слишком короткий (мин. 6 символов)"), 400
    exists = db.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone()
    if exists:
        return jsonify(error="Логин уже занят"), 409
    try:
        cur = db.execute(
            "INSERT INTO users(username,name,first_name,last_name,password_hash,role,dept,active,created_at) "
            "VALUES(?,?,?,?,?,?,?,1,?)",
            (username, name, first_name, last_name, generate_password_hash(password), role, dept, now_iso()))
    except sqlite3.IntegrityError:
        db.rollback()
        return jsonify(error="Логин уже занят"), 409
    add_log(db, u["name"], f"создал(а) пользователя <b>{h(name)}</b> ({ROLES[role]['label']})")
    _commit(db)
    sync_users_csv(db)
    return jsonify(id=cur.lastrowid, ok=True)


@app.patch("/api/users/<int:uid>")
@require(lambda c, u: c["manage"])
def api_update_user(uid):
    db = get_db()
    actor = current_user()
    data = _body()
    row = db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    if not row:
        return jsonify(error="Пользователь не найден"), 404
    fields, params = [], []
    if "role" in data:
        if not isinstance(data["role"], str) or data["role"] not in ROLES:
            return jsonify(error="Неизвестная роль"), 400
        # нельзя понизить самого себя и лишить систему единственного админа
        if uid == actor["id"] and data["role"] != "admin":
            return jsonify(error="Нельзя снять роль администратора с самого себя"), 400
        fields.append("role=?"); params.append(data["role"])
    if "dept" in data and data["dept"] in DEPTS:
        fields.append("dept=?"); params.append(data["dept"])
    if any(k in data for k in ("name", "first_name", "last_name")):
        first_name, last_name, name = _person_from_data(data)
        if not first_name or not last_name:
            return jsonify(error="Имя и фамилия обязательны"), 400
        fields.extend(("first_name=?", "last_name=?", "name=?"))
        params.extend((first_name, last_name, name))
    if "active" in data:
        if uid == actor["id"] and not data["active"]:
            return jsonify(error="Нельзя отключить самого себя"), 400
        fields.append("active=?"); params.append(1 if data["active"] else 0)
    new_pw = str(data.get("password") or "")
    if new_pw:
        if len(new_pw) < 6:
            return jsonify(error="Пароль слишком короткий (мин. 6 символов)"), 400
        fields.append("password_hash=?"); params.append(generate_password_hash(new_pw))
    if not fields:
        return jsonify(error="Нечего обновлять"), 400
    # Разжалование/отключение админа — атомарно с проверкой «останется ли хоть один
    # активный админ», чтобы два параллельных запроса не обнулили админов (как в DELETE).
    demoting_admin = row["role"] == "admin" and (
        ("role=?" in fields and data.get("role") != "admin") or
        ("active=?" in fields and not data.get("active")))
    params.append(uid)
    sql = f"UPDATE users SET {','.join(fields)} WHERE id=?"
    if demoting_admin:
        sql += " AND (SELECT COUNT(*) FROM users WHERE role='admin' AND active=1 AND id<>?) > 0"
        params.append(uid)
    cur = db.execute(sql, params)
    if demoting_admin and cur.rowcount != 1:
        return jsonify(error="Нельзя снять роль последнего администратора"), 400
    add_log(db, actor["name"], f"изменил(а) профиль пользователя <b>{h(row['name'])}</b>")
    _commit(db)
    sync_users_csv(db)
    return jsonify(ok=True)


@app.delete("/api/users/<int:uid>")
@require(lambda c, u: c["manage"])
def api_delete_user(uid):
    db = get_db()
    actor = current_user()
    if uid == actor["id"]:
        return jsonify(error="Нельзя удалить самого себя"), 400
    row = db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    if not row:
        return jsonify(error="Пользователь не найден"), 404
    # Инвариант «не удалить последнего админа» вшит в сам DELETE, чтобы два
    # параллельных удаления не обнулили админов (запись в SQLite сериализуется).
    cur = db.execute(
        "DELETE FROM users WHERE id=? AND "
        "(role<>'admin' OR (SELECT COUNT(*) FROM users WHERE role='admin' AND active=1) > 1)",
        (uid,))
    if cur.rowcount != 1:
        return jsonify(error="Нельзя удалить последнего администратора"), 400
    add_log(db, actor["name"], f"удалил(а) пользователя <b>{h(row['name'])}</b>")
    _commit(db)
    sync_users_csv(db)
    return jsonify(ok=True)


@app.post("/api/reset")
@require(lambda c, u: c["manage"])
def api_reset():
    """Сброс оперативных данных и сценария (аккаунты и текущий вход сохраняются может быть — но проще пересоздать всё)."""
    db = get_db()
    for tbl in ("chat_group_messages", "chat_group_members", "chat_groups", "messages", "tasks",
                "knowledge", "map_objects", "logs", "news", "schedule", "channel_messages",
                "accounts", "transactions", "settings", "users"):
        db.execute(f"DELETE FROM {tbl}")
    # сбрасываем счётчики AUTOINCREMENT, чтобы сценарий восстановился идентично исходному
    db.execute("DELETE FROM sqlite_sequence")
    _commit(db)
    seed(db)
    sync_users_csv(db)
    bump_version()
    session.clear()
    return jsonify(ok=True)


# ---------------------------------------------------------------------------
# API: серверное время (для синхронизации часов у всех участников)
# ---------------------------------------------------------------------------
@app.get("/api/time")
def api_time():
    return jsonify(server_time=now_iso())


@app.get("/api/sensors/snapshot")
@login_required
def api_sensor_snapshot():
    try:
        response = jsonify(_get_sensor_snapshot(force=False))
        response.headers["Cache-Control"] = "private, no-cache"
        return response
    except RuntimeError:
        return jsonify(error="Сеть зондов временно недоступна, сохранённого снимка пока нет"), 502


@app.post("/api/sensors/refresh")
@login_required
def api_refresh_sensors():
    u = current_user()
    if not _rate_ok(_SENSOR_REFRESH_HITS, u["id"], 30, 2):
        return jsonify(error="Обновление уже выполнялось. Подождите несколько секунд."), 429
    _rate_hit(_SENSOR_REFRESH_HITS, u["id"])
    try:
        response = jsonify(_get_sensor_snapshot(force=True))
        response.headers["Cache-Control"] = "private, no-cache"
        return response
    except RuntimeError:
        return jsonify(error="Не удалось обновить карту зондов"), 502


# ---------------------------------------------------------------------------
# API: новости (создаёт только администратор — «вводные от ведущих»)
# ---------------------------------------------------------------------------
def _clean_news(item):
    title = str(item.get("title") or "").strip()
    if not title:
        return None
    lvl = item.get("level")
    level = lvl if isinstance(lvl, str) and lvl in NEWS_LEVELS else "info"
    body = str(item.get("body") or "").strip()
    pinned = 1 if item.get("pinned") else 0
    return (title, body, level, pinned)


@app.post("/api/news")
@require(lambda c, u: c["manage"])
def api_create_news():
    db = get_db()
    u = current_user()
    cleaned = _clean_news(_body())
    if not cleaned:
        return jsonify(error="Введите заголовок новости"), 400
    title, body, level, pinned = cleaned
    cur = db.execute(
        "INSERT INTO news(title,body,level,author,pinned,created_at) VALUES(?,?,?,?,?,?)",
        (title, body, level, u["name"], pinned, now_iso()))
    add_log(db, u["name"], f"опубликовал(а) новость <b>«{h(title[:44])}»</b>")
    _commit(db)
    return jsonify(id=cur.lastrowid, ok=True)


@app.post("/api/news/import")
@require(lambda c, u: c["manage"])
def api_import_news():
    db = get_db()
    u = current_user()
    items = (_body()).get("items") or []
    if not isinstance(items, list):
        return jsonify(error="Ожидается список новостей"), 400
    n = 0
    for it in items:
        cleaned = _clean_news(it if isinstance(it, dict) else {})
        if not cleaned:
            continue
        title, body, level, pinned = cleaned
        db.execute("INSERT INTO news(title,body,level,author,pinned,created_at) VALUES(?,?,?,?,?,?)",
                   (title, body, level, u["name"], pinned, now_iso()))
        n += 1
    if n:
        add_log(db, u["name"], f"импортировал(а) новостей: <b>{n}</b>")
    _commit(db)
    return jsonify(imported=n, ok=True)


@app.delete("/api/news/<int:nid>")
@require(lambda c, u: c["manage"])
def api_delete_news(nid):
    db = get_db()
    row = db.execute("SELECT * FROM news WHERE id=?", (nid,)).fetchone()
    if not row:
        return jsonify(error="Новость не найдена"), 404
    db.execute("DELETE FROM news WHERE id=?", (nid,))
    add_log(db, current_user()["name"], f"удалил(а) новость <b>«{h(row['title'][:44])}»</b>")
    _commit(db)
    return jsonify(ok=True)


# ---------------------------------------------------------------------------
# API: расписание / события (ведёт штаб)
# ---------------------------------------------------------------------------
def _valid_time(s):
    m = re.match(r"^(\d{1,2}):(\d{2})$", str(s or "").strip())
    if not m:
        return None
    hh, mm = int(m.group(1)), int(m.group(2))
    if hh > 23 or mm > 59:
        return None
    return f"{hh:02d}:{mm:02d}"


def _clean_sched(item):
    at = _valid_time(item.get("at"))
    title = str(item.get("title") or "").strip()
    if not at or not title:
        return None
    dept = item.get("dept") if item.get("dept") in DEPTS else None
    k = item.get("kind")
    kind = k if isinstance(k, str) and k in SCHED_KINDS else "event"
    note = str(item.get("note") or "").strip()
    return (at, title, dept, kind, note)


@app.post("/api/schedule")
@require(lambda c, u: c["decide"])
def api_create_sched():
    db = get_db()
    u = current_user()
    cleaned = _clean_sched(_body())
    if not cleaned:
        return jsonify(error="Нужны время (ЧЧ:ММ) и название"), 400
    at, title, dept, kind, note = cleaned
    cur = db.execute(
        "INSERT INTO schedule(at_time,title,dept,kind,note,created_at) VALUES(?,?,?,?,?,?)",
        (at, title, dept, kind, note, now_iso()))
    add_log(db, u["name"], f"добавил(а) в расписание <b>{h(at)} — «{h(title[:40])}»</b>")
    _commit(db)
    return jsonify(id=cur.lastrowid, ok=True)


@app.post("/api/schedule/import")
@require(lambda c, u: c["decide"])
def api_import_sched():
    db = get_db()
    u = current_user()
    items = (_body()).get("items") or []
    if not isinstance(items, list):
        return jsonify(error="Ожидается список пунктов"), 400
    n = 0
    for it in items:
        cleaned = _clean_sched(it if isinstance(it, dict) else {})
        if not cleaned:
            continue
        at, title, dept, kind, note = cleaned
        db.execute("INSERT INTO schedule(at_time,title,dept,kind,note,created_at) VALUES(?,?,?,?,?,?)",
                   (at, title, dept, kind, note, now_iso()))
        n += 1
    if n:
        add_log(db, u["name"], f"импортировал(а) пунктов расписания: <b>{n}</b>")
    _commit(db)
    return jsonify(imported=n, ok=True)


@app.patch("/api/schedule/<int:sid>")
@require(lambda c, u: c["decide"])
def api_update_sched(sid):
    db = get_db()
    row = db.execute("SELECT * FROM schedule WHERE id=?", (sid,)).fetchone()
    if not row:
        return jsonify(error="Пункт не найден"), 404
    data = _body()
    fields, params = [], []
    if "at" in data:
        at = _valid_time(data["at"])
        if not at:
            return jsonify(error="Время в формате ЧЧ:ММ"), 400
        fields.append("at_time=?"); params.append(at)
    if "title" in data and str(data["title"] or "").strip():
        fields.append("title=?"); params.append(str(data["title"]).strip())
    if "dept" in data:
        fields.append("dept=?"); params.append(data["dept"] if data["dept"] in DEPTS else None)
    if "kind" in data and isinstance(data["kind"], str) and data["kind"] in SCHED_KINDS:
        fields.append("kind=?"); params.append(data["kind"])
    if "note" in data:
        fields.append("note=?"); params.append(str(data["note"] or "").strip())
    if not fields:
        return jsonify(error="Нечего обновлять"), 400
    params.append(sid)
    db.execute(f"UPDATE schedule SET {','.join(fields)} WHERE id=?", params)
    add_log(db, current_user()["name"], f"изменил(а) в расписании <b>«{h(row['title'][:40])}»</b>")
    _commit(db)
    return jsonify(ok=True)


@app.delete("/api/schedule/<int:sid>")
@require(lambda c, u: c["decide"])
def api_delete_sched(sid):
    db = get_db()
    row = db.execute("SELECT * FROM schedule WHERE id=?", (sid,)).fetchone()
    if not row:
        return jsonify(error="Пункт не найден"), 404
    db.execute("DELETE FROM schedule WHERE id=?", (sid,))
    add_log(db, current_user()["name"], f"удалил(а) из расписания <b>«{h(row['title'][:40])}»</b>")
    _commit(db)
    return jsonify(ok=True)


# Инициализация базы при импорте — чтобы работало и через gunicorn (gunicorn app:app),
# и через обычный запуск python app.py. Функция идемпотентна (сценарий заполняется
# только если база пустая).
# ВНИМАНИЕ: рассчитано на ОДИН воркер (Dockerfile: gunicorn -w 1). SQLite + сидирование
# при импорте не безопасны для нескольких процессов-воркеров. Для масштабирования на
# несколько воркеров нужен внешний БД-сервер (PostgreSQL) и разовая инициализация.
init_db()

# ---------------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"GreenNet Crisis запущен → http://localhost:{port}")
    print("Вход: admin (пароль сгенерирован выше при первом запуске и лежит в admin_password.txt)")
    app.run(host="0.0.0.0", port=port, debug=False)
