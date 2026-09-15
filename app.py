import os
import re
import sqlite3
from datetime import datetime
import requests
import urllib3
from flask import Flask, request, jsonify

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)

TOKEN = os.environ.get("MAX_TOKEN", "")
SECRET = os.environ.get("WEBHOOK_SECRET", "")
ADMINS = {x.strip() for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()}
DB = os.environ.get("DB_PATH", "/tmp/bot.db")
API = "https://platform-api2.max.ru"

states = {}


def db():
    c = sqlite3.connect(DB, timeout=20)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("""CREATE TABLE IF NOT EXISTS trainings(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT NOT NULL,
        time TEXT NOT NULL,
        capacity INTEGER NOT NULL,
        active INTEGER DEFAULT 1
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS registrations(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        training_id INTEGER NOT NULL,
        user_id TEXT NOT NULL,
        name TEXT NOT NULL,
        phone TEXT NOT NULL,
        category TEXT NOT NULL,
        created_at TEXT NOT NULL,
        gender TEXT DEFAULT '',
        svo TEXT DEFAULT '',
        UNIQUE(training_id,user_id)
    )""")
    cols = {r[1] for r in c.execute("PRAGMA table_info(registrations)").fetchall()}
    if "gender" not in cols:
        c.execute("ALTER TABLE registrations ADD COLUMN gender TEXT DEFAULT ''")
    if "svo" not in cols:
        c.execute("ALTER TABLE registrations ADD COLUMN svo TEXT DEFAULT ''")
    c.commit()
    return c


def admin(uid):
    return str(uid) in ADMINS


def hdr():
    return {"Authorization": TOKEN, "Content-Type": "application/json"}


def btn(text, payload):
    return {"type": "callback", "text": text, "payload": payload}


def keyboard(rows):
    return [{"type": "inline_keyboard", "payload": {"buttons": rows}}]


def send(uid, text, rows=None):
    body = {"text": text}
    if rows:
        body["attachments"] = keyboard(rows)
    try:
        r = requests.post(
            API + "/messages",
            params={"user_id": uid},
            headers=hdr(),
            json=body,
            timeout=20,
            verify=False,
        )
        print("SEND", r.status_code, r.text, flush=True)
    except Exception as e:
        print("SEND ERROR", repr(e), flush=True)


def answer(callback_id):
    if not callback_id:
        return
    try:
        requests.post(
            API + "/answers",
            params={"callback_id": callback_id},
            headers=hdr(),
            json={"notification": "Готово"},
            timeout=20,
            verify=False,
        )
    except Exception as e:
        print("ANSWER ERROR", repr(e), flush=True)


def menu(uid):
    rows = [
        [btn("🎯 Записаться", "book")],
        [btn("📋 Мои записи", "mine")],
        [btn("❌ Отменить запись", "cancel")],
        [btn("ℹ️ Информация", "info")],
    ]
    if admin(uid):
        rows.append([btn("⚙️ Администрирование", "admin")])
    send(uid, "🎯 Спортивное метание ножа | Самара\n\nВыберите действие:", rows)


def amenu(uid):
    if not admin(uid):
        return menu(uid)
    send(uid, "⚙️ Администрирование\n\nВыберите действие:", [
        [btn("➕ Создать тренировку", "a:new")],
        [btn("📅 Все тренировки", "a:all")],
        [btn("👥 Записавшиеся", "a:list")],
        [btn("🧑‍💼 Удалить участника", "a:remove")],
        [btn("🔴 Закрыть запись", "a:close")],
        [btn("🟢 Открыть запись", "a:open")],
        [btn("🗑 Удалить тренировку", "a:delete")],
        [btn("⬅️ Главное меню", "main")],
    ])


def trainings():
    c = db()
    rs = c.execute("""
        SELECT t.*, COUNT(r.id) cnt
        FROM trainings t
        LEFT JOIN registrations r ON r.training_id=t.id
        GROUP BY t.id
        ORDER BY t.id DESC
    """).fetchall()
    c.close()
    return rs


def pick(uid, action, title, mode=None):
    rs = trainings()
    if mode == "active":
        rs = [r for r in rs if r["active"]]
    if mode == "inactive":
        rs = [r for r in rs if not r["active"]]
    if not rs:
        return send(uid, "Подходящих тренировок нет.", [[btn("⬅️ Админ-меню", "admin")]])
    rows = []
    for r in rs:
        icon = "🟢" if r["active"] else "🔴"
        rows.append([btn(
            f"{icon} #{r['id']} {r['date']} • {r['time']} ({r['cnt']}/{r['capacity']})",
            f"{action}:{r['id']}"
        )])
    rows.append([btn("⬅️ Админ-меню", "admin")])
    send(uid, title, rows)


def uid_of(u):
    for path in [
        ("callback", "user", "user_id"),
        ("message", "sender", "user_id"),
        ("user", "user_id"),
    ]:
        x = u
        try:
            for p in path:
                x = x[p]
            return str(x)
        except Exception:
            pass
    return None


def text_of(u):
    return (
        u.get("message", {}).get("body", {}).get("text")
        or u.get("message", {}).get("text")
        or ""
    ).strip()


def callback_data(u):
    cb = u.get("callback", {}) or {}
    return cb.get("payload", ""), cb.get("callback_id") or cb.get("id")


def valid_name(text):
    text = " ".join(text.strip().split())
    if len(text) < 5 or len(text) > 100:
        return False
    if any(ch.isdigit() for ch in text):
        return False
    parts = text.split()
    if len(parts) < 2:
        return False
    allowed = re.compile(r"^[A-Za-zА-Яа-яЁё\-\s]+$")
    return bool(allowed.fullmatch(text))


def normalize_phone(text):
    digits = re.sub(r"\D", "", text)
    if len(digits) == 11 and digits[0] in ("7", "8"):
        return "+7" + digits[1:]
    if len(digits) == 10 and digits[0] == "9":
        return "+7" + digits
    return None


def valid_future_date(date_text):
    try:
        d = datetime.strptime(date_text, "%d.%m.%Y").date()
        return d >= datetime.now().date()
    except ValueError:
        return False


def training_row(tid):
    c = db()
    r = c.execute("""
        SELECT t.*, COUNT(r.id) cnt
        FROM trainings t
        LEFT JOIN registrations r ON r.training_id=t.id
        WHERE t.id=?
        GROUP BY t.id
    """, (tid,)).fetchone()
    c.close()
    return r


def mine(uid, cancel=False):
    c = db()
    rs = c.execute("""
        SELECT r.id,t.date,t.time,r.name,r.category,r.gender,r.svo
        FROM registrations r
        JOIN trainings t ON t.id=r.training_id
        WHERE r.user_id=?
        ORDER BY r.id DESC
    """, (uid,)).fetchall()
    c.close()
    if not rs:
        return send(uid, "У вас пока нет записей.", [[btn("⬅️ Главное меню", "main")]])
    if cancel:
        rows = [[btn(f"❌ {r['date']} • {r['time']}", f"del:{r['id']}")] for r in rs]
        rows.append([btn("⬅️ Главное меню", "main")])
        return send(uid, "Какую запись отменить?", rows)
    text = "📋 Ваши записи:\n\n" + "\n\n".join(
        f"• {r['date']} в {r['time']}\n  {r['name']}\n  Пол: {r['gender'] or '—'}\n  Категория: {r['category']}\n  Участник СВО / ветеран: {r['svo'] or '—'}" for r in rs
    )
    send(uid, text, [[btn("⬅️ Главное меню", "main")]])


def show_participants(uid, tid, removal=False):
    c = db()
    t = c.execute("SELECT * FROM trainings WHERE id=?", (tid,)).fetchone()
    rs = c.execute("""
        SELECT id,name,phone,category,user_id,gender,svo
        FROM registrations
        WHERE training_id=?
        ORDER BY id
    """, (tid,)).fetchall()
    c.close()
    if not t:
        return send(uid, "Тренировка не найдена.", [[btn("⬅️ Админ-меню", "admin")]])
    if removal:
        if not rs:
            return send(uid, "На эту тренировку пока никто не записан.",
                        [[btn("⬅️ Админ-меню", "admin")]])
        rows = [[btn(f"❌ {r['name']}", f"a:rmreg:{r['id']}")] for r in rs]
        rows.append([btn("⬅️ Админ-меню", "admin")])
        return send(uid, f"🧑‍💼 Удаление участника\n\n{t['date']} • {t['time']}\nВыберите участника:", rows)

    if not rs:
        text = f"👥 {t['date']} • {t['time']}\n\nЗаписавшихся пока нет."
    else:
        lines = []
        for i, r in enumerate(rs, 1):
            lines.append(f"{i}. {r['name']}\n📞 {r['phone']}\n🚻 Пол: {r['gender'] or '—'}\n🏷 Категория: {r['category']}\n🎖 Участник СВО / ветеран: {r['svo'] or '—'}\nMAX ID: {r['user_id']}")
        text = f"👥 {t['date']} • {t['time']}\n\n" + "\n\n".join(lines)
    send(uid, text, [[btn("⬅️ Админ-меню", "admin")]])


def start_booking(uid, tid):
    r = training_row(tid)
    if not r or not r["active"]:
        return send(uid, "Запись на эту тренировку закрыта.", [[btn("⬅️ Главное меню", "main")]])
    if r["cnt"] >= r["capacity"]:
        return send(uid, "На этой тренировке мест уже нет.", [[btn("⬅️ Главное меню", "main")]])
    c = db()
    exists = c.execute(
        "SELECT 1 FROM registrations WHERE training_id=? AND user_id=?",
        (tid, uid)
    ).fetchone()
    c.close()
    if exists:
        return send(uid, "Вы уже записаны на эту тренировку.", [[btn("📋 Мои записи", "mine")]])
    states[uid] = {"step": "name", "training_id": tid}
    send(uid, "Введите ФИО полностью.\n\nНапример: Иванов Иван Иванович")


def callback(u, uid):
    payload, cid = callback_data(u)
    answer(cid)

    if payload == "main":
        states.pop(uid, None)
        return menu(uid)
    if payload == "admin":
        states.pop(uid, None)
        return amenu(uid)
    if payload == "mine":
        return mine(uid)
    if payload == "cancel":
        return mine(uid, True)
    if payload == "info":
        return send(uid,
            "ℹ️ Запись на тренировки по спортивному метанию ножа в Самаре.\n\n"
            "Выберите «Записаться», укажите ФИО и телефон, затем категорию. "
            "Количество мест ограничено.",
            [[btn("⬅️ Главное меню", "main")]]
        )

    if payload == "book":
        rs = [r for r in trainings() if r["active"] and r["cnt"] < r["capacity"]]
        if not rs:
            return send(uid, "Сейчас нет открытых тренировок со свободными местами.",
                        [[btn("⬅️ Главное меню", "main")]])
        rows = [[btn(
            f"🎯 {r['date']} • {r['time']} — свободно {r['capacity']-r['cnt']}",
            f"choose:{r['id']}"
        )] for r in rs]
        rows.append([btn("⬅️ Главное меню", "main")])
        return send(uid, "Выберите тренировку:", rows)

    if payload.startswith("choose:"):
        return start_booking(uid, int(payload.split(":")[-1]))

    if payload.startswith("gender:"):
        st = states.get(uid, {})
        if st.get("step") != "gender":
            return menu(uid)
        st["gender"] = payload.split(":", 1)[1]
        st["step"] = "category"
        return send(uid, "🏷 Выберите категорию:", [
            [btn("Общая", "cat:Общая"), btn("ПОДА", "cat:ПОДА")],
            [btn("⬅️ Отмена", "main")],
        ])

    if payload.startswith("cat:"):
        st = states.get(uid, {})
        if st.get("step") != "category":
            return menu(uid)
        st["category"] = payload.split(":", 1)[1]
        st["step"] = "svo"
        return send(uid, "🎖 Участник СВО / ветеран?", [
            [btn("Да", "svo:Да"), btn("Нет", "svo:Нет")],
            [btn("⬅️ Отмена", "main")],
        ])

    if payload.startswith("svo:"):
        st = states.get(uid, {})
        if st.get("step") != "svo":
            return menu(uid)
        st["svo"] = payload.split(":", 1)[1]
        st["step"] = "consent"
        return send(uid,
            "🔐 Согласие на обработку персональных данных\n\n"
            "Для записи бот сохраняет ваши ФИО, номер телефона, пол, категорию, "
            "ответ о статусе участника СВО / ветерана и MAX ID. "
            "Данные используются для организации тренировки.\n\n"
            "Подтверждая запись, вы соглашаетесь на обработку этих данных.",
            [[btn("✅ Согласен и записаться", "consent:yes")],
             [btn("❌ Не согласен", "consent:no")]]
        )

    if payload == "consent:no":
        states.pop(uid, None)
        return send(uid, "Запись отменена. Данные не сохранены.",
                    [[btn("⬅️ Главное меню", "main")]])

    if payload == "consent:yes":
        st = states.get(uid, {})
        if st.get("step") != "consent":
            return menu(uid)
        tid = st["training_id"]
        r = training_row(tid)
        if not r or not r["active"] or r["cnt"] >= r["capacity"]:
            states.pop(uid, None)
            return send(uid, "К сожалению, запись уже закрыта или свободные места закончились.",
                        [[btn("⬅️ Главное меню", "main")]])
        c = db()
        try:
            c.execute("""
                INSERT INTO registrations(training_id,user_id,name,phone,category,created_at,gender,svo)
                VALUES(?,?,?,?,?,?,?,?)
            """, (
                tid, uid, st["name"], st["phone"], st["category"],
                datetime.now().isoformat(timespec="seconds"),
                st["gender"], st["svo"]
            ))
            c.commit()
        except sqlite3.IntegrityError:
            c.close()
            states.pop(uid, None)
            return send(uid, "Вы уже записаны на эту тренировку.",
                        [[btn("📋 Мои записи", "mine")]])
        c.close()
        states.pop(uid, None)
        text = (
            f"✅ Вы записаны!\n\n"
            f"📅 {r['date']}\n"
            f"🕒 {r['time']}\n"
            f"👤 {st['name']}\n"
            f"📞 {st['phone']}\n"
            f"🚻 Пол: {st['gender']}\n"
            f"🏷 Категория: {st['category']}\n"
            f"🎖 Участник СВО / ветеран: {st['svo']}"
        )
        send(uid, text, [[btn("📋 Мои записи", "mine")], [btn("⬅️ Главное меню", "main")]])
        notice = (
            f"🎯 Новая запись\n\n"
            f"📅 {r['date']} • {r['time']}\n"
            f"👤 {st['name']}\n"
            f"📞 {st['phone']}\n"
            f"🚻 Пол: {st['gender']}\n"
            f"🏷 Категория: {st['category']}\n"
            f"🎖 Участник СВО / ветеран: {st['svo']}\n"
            f"MAX ID: {uid}"
        )
        for aid in ADMINS:
            send(aid, notice)
        return

    if payload.startswith("del:"):
        rid = int(payload.split(":")[-1])
        c = db()
        row = c.execute("""
            SELECT r.id,t.date,t.time
            FROM registrations r JOIN trainings t ON t.id=r.training_id
            WHERE r.id=? AND r.user_id=?
        """, (rid, uid)).fetchone()
        if row:
            c.execute("DELETE FROM registrations WHERE id=? AND user_id=?", (rid, uid))
            c.commit()
        c.close()
        if row:
            for aid in ADMINS:
                send(aid, f"❌ Участник отменил запись\n\n📅 {row['date']} • {row['time']}\nMAX ID: {uid}")
            return send(uid, "✅ Запись отменена.", [[btn("⬅️ Главное меню", "main")]])
        return send(uid, "Запись не найдена.", [[btn("⬅️ Главное меню", "main")]])

    if not admin(uid):
        return menu(uid)

    if payload == "a:new":
        states[uid] = {"step": "adate"}
        return send(uid, "📅 Введите дату тренировки в формате ДД.ММ.ГГГГ\nНапример: 23.09.2026")

    if payload == "a:all":
        rs = trainings()
        if not rs:
            return send(uid, "Тренировок пока нет.", [[btn("⬅️ Админ-меню", "admin")]])
        text = "📅 Все тренировки:\n\n" + "\n".join(
            f"{'🟢' if r['active'] else '🔴'} #{r['id']} — {r['date']} • {r['time']} — {r['cnt']}/{r['capacity']}"
            for r in rs
        )
        return send(uid, text, [[btn("⬅️ Админ-меню", "admin")]])

    if payload == "a:list":
        return pick(uid, "a:show", "Выберите тренировку:")
    if payload == "a:remove":
        return pick(uid, "a:removelist", "Выберите тренировку:")
    if payload == "a:close":
        return pick(uid, "a:doclose", "Какую тренировку закрыть?", "active")
    if payload == "a:open":
        return pick(uid, "a:doopen", "Какую тренировку открыть?", "inactive")
    if payload == "a:delete":
        return pick(uid, "a:askdelete", "Какую тренировку удалить?")

    if payload.startswith("a:show:"):
        return show_participants(uid, int(payload.split(":")[-1]))
    if payload.startswith("a:removelist:"):
        return show_participants(uid, int(payload.split(":")[-1]), True)

    if payload.startswith("a:rmreg:"):
        rid = int(payload.split(":")[-1])
        c = db()
        r = c.execute("""
            SELECT r.id,r.user_id,r.name,t.date,t.time
            FROM registrations r JOIN trainings t ON t.id=r.training_id
            WHERE r.id=?
        """, (rid,)).fetchone()
        if r:
            c.execute("DELETE FROM registrations WHERE id=?", (rid,))
            c.commit()
        c.close()
        if not r:
            return send(uid, "Запись уже отсутствует.", [[btn("⬅️ Админ-меню", "admin")]])
        send(r["user_id"], f"❌ Администратор отменил вашу запись на тренировку {r['date']} в {r['time']}.")
        return send(uid, f"✅ {r['name']} удалён(а) из списка.",
                    [[btn("⬅️ Админ-меню", "admin")]])

    if payload.startswith("a:doclose:"):
        tid = int(payload.split(":")[-1])
        c = db()
        c.execute("UPDATE trainings SET active=0 WHERE id=?", (tid,))
        c.commit(); c.close()
        return send(uid, "🔴 Запись на тренировку закрыта.", [[btn("⬅️ Админ-меню", "admin")]])

    if payload.startswith("a:doopen:"):
        tid = int(payload.split(":")[-1])
        c = db()
        c.execute("UPDATE trainings SET active=1 WHERE id=?", (tid,))
        c.commit(); c.close()
        return send(uid, "🟢 Запись на тренировку открыта.", [[btn("⬅️ Админ-меню", "admin")]])

    if payload.startswith("a:askdelete:"):
        tid = int(payload.split(":")[-1])
        r = training_row(tid)
        if not r:
            return amenu(uid)
        return send(uid,
            f"⚠️ Удалить тренировку #{tid}\n{r['date']} • {r['time']}?\n\n"
            "Все записи на неё тоже будут удалены.",
            [[btn("🗑 Да, удалить", f"a:dodelete:{tid}")],
             [btn("⬅️ Отмена", "admin")]]
        )

    if payload.startswith("a:dodelete:"):
        tid = int(payload.split(":")[-1])
        c = db()
        users = c.execute("SELECT user_id FROM registrations WHERE training_id=?", (tid,)).fetchall()
        t = c.execute("SELECT date,time FROM trainings WHERE id=?", (tid,)).fetchone()
        c.execute("DELETE FROM registrations WHERE training_id=?", (tid,))
        c.execute("DELETE FROM trainings WHERE id=?", (tid,))
        c.commit(); c.close()
        if t:
            for r in users:
                send(r["user_id"], f"⚠️ Тренировка {t['date']} в {t['time']} отменена администратором.")
        return send(uid, "🗑 Тренировка удалена.", [[btn("⬅️ Админ-меню", "admin")]])

    menu(uid)


def handle_text(uid, text):
    if text.lower() in ("/start", "start", "старт", "меню"):
        states.pop(uid, None)
        return menu(uid)

    st = states.get(uid)
    if not st:
        return menu(uid)

    step = st.get("step")

    if step == "name":
        name = " ".join(text.strip().split())
        if not valid_name(name):
            return send(uid,
                "⚠️ Проверьте ФИО.\n\n"
                "Введите минимум фамилию и имя буквами, без цифр.\n"
                "Например: Иванов Иван Иванович"
            )
        st["name"] = name
        st["step"] = "phone"
        return send(uid,
            "📞 Введите номер телефона.\n\n"
            "Можно так: 8 927 123-45-67 или +7 927 123-45-67"
        )

    if step == "phone":
        phone = normalize_phone(text)
        if not phone:
            return send(uid,
                "⚠️ Номер телефона указан неверно.\n\n"
                "Нужен российский мобильный номер из 10 цифр после +7.\n"
                "Например: +7 927 123-45-67"
            )
        st["phone"] = phone
        st["step"] = "gender"
        return send(uid, "🚻 Выберите пол:", [
            [btn("Мужчина", "gender:Мужчина"), btn("Женщина", "gender:Женщина")],
            [btn("⬅️ Отмена", "main")],
        ])

    if step == "adate":
        if not valid_future_date(text):
            return send(uid,
                "⚠️ Неверная дата или дата уже прошла.\n"
                "Введите в формате ДД.ММ.ГГГГ, например 23.09.2026"
            )
        st["date"] = text
        st["step"] = "atime"
        return send(uid, "🕒 Введите время в формате ЧЧ:ММ\nНапример: 17:00")

    if step == "atime":
        try:
            datetime.strptime(text, "%H:%M")
        except ValueError:
            return send(uid, "⚠️ Неверное время. Введите, например: 17:00")
        st["time"] = text
        st["step"] = "acap"
        return send(uid, "👥 Введите максимальное количество участников (от 1 до 500):")

    if step == "acap":
        try:
            cap = int(text)
            if not 1 <= cap <= 500:
                raise ValueError
        except ValueError:
            return send(uid, "⚠️ Введите целое число от 1 до 500.")
        c = db()
        duplicate = c.execute(
            "SELECT id FROM trainings WHERE date=? AND time=?",
            (st["date"], st["time"])
        ).fetchone()
        if duplicate:
            c.close()
            return send(uid,
                f"⚠️ Тренировка на {st['date']} в {st['time']} уже существует.",
                [[btn("⬅️ Админ-меню", "admin")]]
            )
        c.execute(
            "INSERT INTO trainings(date,time,capacity,active) VALUES(?,?,?,1)",
            (st["date"], st["time"], cap)
        )
        c.commit(); c.close()
        date, tm = st["date"], st["time"]
        states.pop(uid, None)
        return send(uid,
            f"✅ Тренировка создана!\n\n📅 {date}\n🕒 {tm}\n👥 Мест: {cap}",
            [[btn("⬅️ Админ-меню", "admin")]]
        )

    menu(uid)


@app.get("/")
def home():
    return "MAX knife training bot: OK", 200


@app.post("/webhook")
def webhook():
    if SECRET and request.headers.get("X-Max-Bot-Api-Secret") != SECRET:
        return "forbidden", 403
    u = request.get_json(silent=True) or {}
    print("UPDATE", u, flush=True)
    uid = uid_of(u)
    if uid:
        if (u.get("update_type") or u.get("type", "")) == "message_callback" or u.get("callback"):
            callback(u, uid)
        elif u.get("message") or u.get("update_type") in ("message_created", "bot_started"):
            handle_text(uid, text_of(u) or "/start")
    return jsonify({"ok": True})


@app.get("/setup")
def setup():
    if request.args.get("key", "") != SECRET or not TOKEN or not SECRET:
        return "forbidden", 403
    body = {
        "url": "https://web-production-971c2.up.railway.app/webhook",
        "update_types": ["message_created", "message_callback", "bot_started"],
        "secret": SECRET,
    }
    r = requests.post(
        API + "/subscriptions",
        headers=hdr(),
        json=body,
        timeout=20,
        verify=False,
    )
    return r.text, r.status_code, {"Content-Type": "application/json"}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
