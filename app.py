import os
import sqlite3
from datetime import datetime

import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

TOKEN = os.environ.get("MAX_TOKEN", "")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
ADMIN_ID = os.environ.get("ADMIN_ID", "")

API = "https://platform-api2.max.ru"
DB = os.environ.get("DB_PATH", "/tmp/bot.db")

states = {}


# =========================
# БАЗА ДАННЫХ
# =========================

def db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row

    c.execute("""
        CREATE TABLE IF NOT EXISTS trainings(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT,
            time TEXT,
            capacity INTEGER,
            active INTEGER DEFAULT 1
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS registrations(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            training_id INTEGER,
            user_id TEXT,
            name TEXT,
            phone TEXT,
            category TEXT,
            created_at TEXT,
            UNIQUE(training_id, user_id)
        )
    """)

    c.commit()
    return c


# =========================
# MAX API
# =========================

def headers():
    return {
        "Authorization": TOKEN,
        "Content-Type": "application/json"
    }


def kb(rows):
    return [{
        "type": "inline_keyboard",
        "payload": {
            "buttons": rows
        }
    }]


def btn(text, payload):
    return {
        "type": "callback",
        "text": text,
        "payload": payload
    }


def send(uid, text, rows=None):
    body = {
        "text": text
    }

    if rows:
        body["attachments"] = kb(rows)

    try:
        r = requests.post(
            f"{API}/messages",
            params={"user_id": uid},
            headers=headers(),
            json=body,
            timeout=20,
            verify=False
        )

        print(
            "SEND:",
            r.status_code,
            r.text,
            flush=True
        )

        return r

    except Exception as e:
        print(
            "SEND ERROR:",
            repr(e),
            flush=True
        )
        return None


def answer(cb_id, text="Готово"):
    if not cb_id:
        return

    try:
        r = requests.post(
            f"{API}/answers",
            params={"callback_id": cb_id},
            headers=headers(),
            json={"notification": text},
            timeout=20,
            verify=False
        )

        print(
            "ANSWER:",
            r.status_code,
            r.text,
            flush=True
        )

    except Exception as e:
        print(
            "ANSWER ERROR:",
            repr(e),
            flush=True
        )


# =========================
# ГЛАВНОЕ МЕНЮ
# =========================

def main_menu(uid):
    send(
        uid,
        "🎯 Спортивное метание ножа | Самара\n\n"
        "Выберите действие:",
        [
            [btn("🎯 Записаться", "book")],
            [btn("📋 Мои записи", "mine")],
            [btn("❌ Отменить запись", "cancel")],
            [btn("ℹ️ Информация", "info")]
        ]
    )


# =========================
# ТРЕНИРОВКИ
# =========================

def available():
    c = db()

    rows = c.execute("""
        SELECT
            t.*,
            COUNT(r.id) AS cnt
        FROM trainings t
        LEFT JOIN registrations r
            ON r.training_id = t.id
        WHERE t.active = 1
        GROUP BY t.id
        ORDER BY t.date, t.time
    """).fetchall()

    c.close()

    return [
        r for r in rows
        if r["cnt"] < r["capacity"]
    ]


def show_trainings(uid):
    rows = available()

    if not rows:
        send(
            uid,
            "Сейчас нет открытых тренировок.\n\n"
            "Как только появится новая дата, "
            "она будет доступна здесь."
        )
        return

    buttons = []

    for r in rows:
        left = r["capacity"] - r["cnt"]

        buttons.append([
            btn(
                f"📅 {r['date']} • {r['time']} • мест {left}",
                f"choose:{r['id']}"
            )
        ])

    send(
        uid,
        "Выберите тренировку:",
        buttons
    )


# =========================
# ДАННЫЕ UPDATE
# =========================

def user_id(update):
    paths = [
        ("message", "sender", "user_id"),
        ("callback", "user", "user_id"),
        ("user", "user_id")
    ]

    for path in paths:
        x = update

        try:
            for p in path:
                x = x[p]

            if x is not None:
                return str(x)

        except Exception:
            pass

    return None


def text_of(update):
    try:
        return (
            update
            .get("message", {})
            .get("body", {})
            .get("text")
            or update
            .get("message", {})
            .get("text")
            or ""
        ).strip()

    except Exception:
        return ""


def callback_of(update):
    c = update.get("callback") or {}

    return (
        c.get("payload", ""),
        c.get("callback_id")
    )


# =========================
# МОИ ЗАПИСИ
# =========================

def mine(uid, cancel_mode=False):
    c = db()

    rows = c.execute("""
        SELECT
            r.id,
            t.date,
            t.time,
            r.name
        FROM registrations r
        JOIN trainings t
            ON t.id = r.training_id
        WHERE r.user_id = ?
        ORDER BY t.date, t.time
    """, (uid,)).fetchall()

    c.close()

    if not rows:
        send(
            uid,
            "У вас пока нет записей."
        )
        return

    if cancel_mode:
        buttons = [
            [
                btn(
                    f"❌ {r['date']} • {r['time']}",
                    f"del:{r['id']}"
                )
            ]
            for r in rows
        ]

        send(
            uid,
            "Какую запись отменить?",
            buttons
        )

        return

    text = "📋 Ваши записи:\n\n"

    text += "\n".join(
        f"• {r['date']} в {r['time']} — {r['name']}"
        for r in rows
    )

    send(uid, text)


# =========================
# CALLBACK КНОПКИ
# =========================

def handle_callback(update, uid):
    payload, cbid = callback_of(update)

    answer(cbid)

    if payload == "book":
        show_trainings(uid)
        return

    if payload == "mine":
        mine(uid)
        return

    if payload == "cancel":
        mine(uid, True)
        return

    if payload == "info":
        send(
            uid,
            "ℹ️ Спортивное метание ножа | Самара\n\n"
            "📍 Самара, ул. Пионерская, 108, "
            "тир «Аверс».\n\n"
            "Через этого бота можно записаться "
            "на доступные тренировки."
        )
        return

    if payload.startswith("choose:"):
        tid = int(
            payload.split(":")[1]
        )

        states[uid] = {
            "step": "name",
            "training_id": tid
        }

        send(
            uid,
            "Введите ФИО участника:"
        )

        return

    if payload.startswith("cat:"):
        s = states.get(uid)

        if not s:
            main_menu(uid)
            return

        s["category"] = payload.split(
            ":", 1
        )[1]

        c = db()

        try:
            tr = c.execute(
                "SELECT * FROM trainings WHERE id=?",
                (s["training_id"],)
            ).fetchone()

            cnt = c.execute(
                """
                SELECT COUNT(*) AS n
                FROM registrations
                WHERE training_id=?
                """,
                (s["training_id"],)
            ).fetchone()["n"]

            if (
                not tr
                or not tr["active"]
                or cnt >= tr["capacity"]
            ):
                c.close()

                send(
                    uid,
                    "К сожалению, места "
                    "на эту тренировку уже закончились."
                )

                states.pop(uid, None)
                return

            c.execute("""
                INSERT INTO registrations(
                    training_id,
                    user_id,
                    name,
                    phone,
                    category,
                    created_at
                )
                VALUES(?,?,?,?,?,?)
            """, (
                s["training_id"],
                uid,
                s["name"],
                s["phone"],
                s["category"],
                datetime.now().isoformat()
            ))

            c.commit()

        except sqlite3.IntegrityError:
            c.close()

            send(
                uid,
                "Вы уже записаны "
                "на эту тренировку."
            )

            states.pop(uid, None)
            return

        c.close()

        send(
            uid,
            f"✅ Вы записаны!\n\n"
            f"📅 {tr['date']}\n"
            f"⏰ {tr['time']}\n"
            f"👤 {s['name']}\n"
            f"📞 {s['phone']}\n"
            f"Категория: {s['category']}"
        )

        if ADMIN_ID:
            send(
                ADMIN_ID,
                f"🎯 Новая запись\n\n"
                f"👤 {s['name']}\n"
                f"📅 {tr['date']}\n"
                f"⏰ {tr['time']}\n"
                f"📞 {s['phone']}\n"
                f"Категория: {s['category']}\n"
                f"MAX ID: {uid}"
            )

        states.pop(uid, None)
        return

    if payload.startswith("del:"):
        rid = int(
            payload.split(":")[1]
        )

        c = db()

        row = c.execute(
            """
            SELECT *
            FROM registrations
            WHERE id=? AND user_id=?
            """,
            (rid, uid)
        ).fetchone()

        if row:
            c.execute(
                "DELETE FROM registrations WHERE id=?",
                (rid,)
            )

            c.commit()

            send(
                uid,
                "✅ Запись отменена."
            )

        c.close()
        return


# =========================
# ОБЫЧНЫЕ СООБЩЕНИЯ
# =========================

def handle_text(update, uid, text):
    if text.lower() in [
        "/start",
        "start",
        "старт",
        "меню"
    ]:
        states.pop(uid, None)
        main_menu(uid)
        return

    # Создание тренировки администратором
    if (
        ADMIN_ID
        and uid == ADMIN_ID
        and text.startswith("/slot ")
    ):
        try:
            _, date, time, cap = text.split()

            cap = int(cap)

            c = db()

            c.execute(
                """
                INSERT INTO trainings(
                    date,
                    time,
                    capacity
                )
                VALUES(?,?,?)
                """,
                (date, time, cap)
            )

            c.commit()
            c.close()

            send(
                uid,
                f"✅ Тренировка создана\n\n"
                f"📅 {date}\n"
                f"⏰ {time}\n"
                f"👥 Мест: {cap}"
            )

        except Exception:
            send(
                uid,
                "Формат команды:\n"
                "/slot 20.09.2026 18:00 12"
            )

        return

    if (
        ADMIN_ID
        and uid == ADMIN_ID
        and text == "/slots"
    ):
        c = db()

        rows = c.execute("""
            SELECT
                t.*,
                COUNT(r.id) AS cnt
            FROM trainings t
            LEFT JOIN registrations r
                ON r.training_id=t.id
            GROUP BY t.id
            ORDER BY t.id DESC
        """).fetchall()

        c.close()

        if not rows:
            send(
                uid,
                "Тренировок пока нет."
            )
            return

        text_out = "\n".join(
            f"#{r['id']} "
            f"{r['date']} "
            f"{r['time']} — "
            f"{r['cnt']}/{r['capacity']} "
            f"{'🟢' if r['active'] else '🔴'}"
            for r in rows
        )

        send(uid, text_out)
        return

    if (
        ADMIN_ID
        and uid == ADMIN_ID
        and text.startswith("/close ")
    ):
        try:
            tid = int(
                text.split()[1]
            )

            c = db()

            c.execute(
                """
                UPDATE trainings
                SET active=0
                WHERE id=?
                """,
                (tid,)
            )

            c.commit()
            c.close()

            send(
                uid,
                f"🔴 Запись на тренировку "
                f"#{tid} закрыта."
            )

        except Exception:
            send(
                uid,
                "Формат команды:\n"
                "/close 1"
            )

        return

    if (
        ADMIN_ID
        and uid == ADMIN_ID
        and text.startswith("/list ")
    ):
        try:
            tid = int(
                text.split()[1]
            )

            c = db()

            tr = c.execute(
                """
                SELECT *
                FROM trainings
                WHERE id=?
                """,
                (tid,)
            ).fetchone()

            rows = c.execute(
                """
                SELECT *
                FROM registrations
                WHERE training_id=?
                ORDER BY id
                """,
                (tid,)
            ).fetchall()

            c.close()

            if not tr:
                send(
                    uid,
                    "Тренировка не найдена."
                )
                return

            result = (
                f"📋 {tr['date']} "
                f"{tr['time']}\n\n"
            )

            if rows:
                result += "\n".join(
                    f"{i + 1}. "
                    f"{r['name']} — "
                    f"{r['phone']} — "
                    f"{r['category']}"
                    for i, r in enumerate(rows)
                )

            else:
                result += "Записей пока нет."

            send(uid, result)

        except Exception:
            send(
                uid,
                "Формат команды:\n"
                "/list 1"
            )

        return

    # Процесс записи пользователя
    s = states.get(uid)

    if not s:
        main_menu(uid)
        return

    if s["step"] == "name":
        s["name"] = text
        s["step"] = "phone"

        send(
            uid,
            "Введите номер телефона\n"
            "(например, +7 927 000-00-00):"
        )

        return

    if s["step"] == "phone":
        s["phone"] = text
        s["step"] = "category"

        send(
            uid,
            "Выберите категорию:",
            [
                [
                    btn(
                        "Общая",
                        "cat:Общая"
                    ),
                    btn(
                        "ПОДА",
                        "cat:ПОДА"
                    )
                ],
                [
                    btn(
                        "Другая",
                        "cat:Другая"
                    )
                ]
            ]
        )

        return


# =========================
# ПРОВЕРКА СЕРВЕРА
# =========================

@app.get("/")
def health():
    return (
        "MAX knife training bot: OK",
        200
    )


# =========================
# WEBHOOK MAX
# =========================

@app.post("/webhook")
def webhook():
    if (
        WEBHOOK_SECRET
        and request.headers.get(
            "X-Max-Bot-Api-Secret"
        ) != WEBHOOK_SECRET
    ):
        return "forbidden", 403

    update = (
        request.get_json(
            silent=True
        )
        or {}
    )

    print(
        "UPDATE:",
        update,
        flush=True
    )

    uid = user_id(update)

    if uid:
        typ = (
            update.get("update_type")
            or update.get("type", "")
        )

        if (
            typ == "message_callback"
            or update.get("callback")
        ):
            handle_callback(
                update,
                uid
            )

        elif (
            typ in (
                "message_created",
                "bot_started"
            )
            or update.get("message")
        ):
            text = text_of(update)

            handle_text(
                update,
                uid,
                text or "/start"
            )

    return jsonify({
        "ok": True
    })


# =========================
# РЕГИСТРАЦИЯ WEBHOOK
# =========================

@app.get("/setup")
def setup():
    key = request.args.get(
        "key",
        ""
    )

    if (
        not TOKEN
        or not WEBHOOK_SECRET
        or key != WEBHOOK_SECRET
    ):
        return "forbidden", 403

    base = request.url_root.rstrip("/")

    body = {
        "url": base + "/webhook",
        "update_types": [
            "message_created",
            "message_callback",
            "bot_started"
        ],
        "secret": WEBHOOK_SECRET
    }

    try:
        r = requests.post(
            f"{API}/subscriptions",
            headers=headers(),
            json=body,
            timeout=20,
            verify=False
        )

        print(
            "SETUP:",
            r.status_code,
            r.text,
            flush=True
        )

        return (
            r.text,
            r.status_code,
            {
                "Content-Type":
                "application/json"
            }
        )

    except Exception as e:
        print(
            "SETUP ERROR:",
            repr(e),
            flush=True
        )

        return jsonify({
            "ok": False,
            "error": str(e)
        }), 500


# =========================
# ЗАПУСК
# =========================

if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(
            os.environ.get(
                "PORT",
                "8080"
            )
        )
    )
