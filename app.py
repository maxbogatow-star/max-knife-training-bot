import os
import re
import csv
import io
import time
import sqlite3
import threading
from datetime import datetime, timedelta
from urllib.parse import quote_plus

import requests
import urllib3
from flask import Flask, request, jsonify, Response

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)

TOKEN = os.environ.get("MAX_TOKEN", "")
SECRET = os.environ.get("WEBHOOK_SECRET", "")
ADMINS = {x.strip() for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()}
DB = os.environ.get("DB_PATH", "/tmp/bot.db")
API = "https://platform-api2.max.ru"
PUBLIC_URL = os.environ.get("PUBLIC_URL", "https://web-production-971c2.up.railway.app").rstrip("/")

CONTACT_MAXIM = "+7 927 777-83-80"
CONTACT_YAKOVLEV = "+7 904 745-03-99"

states = {}
reminder_thread_started = False


def conn():
    c = sqlite3.connect(DB, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA foreign_keys=ON")
    return c


def ensure_column(c, table, name, definition):
    cols = {r[1] for r in c.execute(f"PRAGMA table_info({table})").fetchall()}
    if name not in cols:
        c.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def init_db():
    c = conn()
    c.execute("""CREATE TABLE IF NOT EXISTS trainings(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT NOT NULL,
        time TEXT NOT NULL,
        capacity INTEGER NOT NULL,
        active INTEGER DEFAULT 1
    )""")
    ensure_column(c, "trainings", "venue", "TEXT DEFAULT ''")
    ensure_column(c, "trainings", "address", "TEXT DEFAULT ''")
    ensure_column(c, "trainings", "distance", "TEXT DEFAULT ''")
    ensure_column(c, "trainings", "note", "TEXT DEFAULT ''")
    ensure_column(c, "trainings", "archived", "INTEGER DEFAULT 0")
    ensure_column(c, "trainings", "reminder_sent", "INTEGER DEFAULT 0")

    c.execute("""CREATE TABLE IF NOT EXISTS registrations(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        training_id INTEGER NOT NULL,
        user_id TEXT NOT NULL,
        name TEXT NOT NULL,
        phone TEXT NOT NULL,
        category TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(training_id,user_id)
    )""")
    ensure_column(c, "registrations", "gender", "TEXT DEFAULT ''")
    ensure_column(c, "registrations", "svo", "TEXT DEFAULT ''")
    ensure_column(c, "registrations", "consent_at", "TEXT DEFAULT ''")
    ensure_column(c, "registrations", "attendance", "TEXT DEFAULT ''")

    c.execute("""CREATE TABLE IF NOT EXISTS waitlist(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        training_id INTEGER NOT NULL,
        user_id TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(training_id,user_id)
    )""")
    c.commit()
    c.close()


init_db()


def admin(uid):
    return str(uid) in ADMINS


def hdr():
    return {"Authorization": TOKEN, "Content-Type": "application/json"}


def btn(text, payload):
    return {"type": "callback", "text": text, "payload": payload}


def link_btn(text, url):
    return {"type": "link", "text": text, "url": url}


def keyboard(rows):
    return [{"type": "inline_keyboard", "payload": {"buttons": rows}}]


def send(uid, text, rows=None):
    body = {"text": text}
    if rows:
        body["attachments"] = keyboard(rows)
    try:
        r = requests.post(API + "/messages", params={"user_id": uid}, headers=hdr(),
                          json=body, timeout=20, verify=False)
        print("SEND", r.status_code, r.text[:500], flush=True)
        return r.ok
    except Exception as e:
        print("SEND ERROR", repr(e), flush=True)
        return False


def answer(callback_id):
    if not callback_id:
        return
    try:
        requests.post(API + "/answers", params={"callback_id": callback_id}, headers=hdr(),
                      json={"notification": "Готово"}, timeout=20, verify=False)
    except Exception as e:
        print("ANSWER ERROR", repr(e), flush=True)


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
    return (u.get("message", {}).get("body", {}).get("text")
            or u.get("message", {}).get("text") or "").strip()


def callback_data(u):
    cb = u.get("callback", {}) or {}
    return cb.get("payload", ""), cb.get("callback_id") or cb.get("id")


def valid_name(text):
    text = " ".join(text.strip().split())
    return (5 <= len(text) <= 100 and len(text.split()) >= 2
            and not any(ch.isdigit() for ch in text)
            and bool(re.fullmatch(r"[A-Za-zА-Яа-яЁё\-\s]+", text)))


def normalize_phone(text):
    d = re.sub(r"\D", "", text)
    if len(d) == 11 and d[0] in "78":
        d = "7" + d[1:]
    elif len(d) == 10 and d[0] == "9":
        d = "7" + d
    else:
        return None
    return f"+7 {d[1:4]} {d[4:7]}-{d[7:9]}-{d[9:]}"


def parse_dt(date_text, time_text):
    try:
        return datetime.strptime(f"{date_text} {time_text}", "%d.%m.%Y %H:%M")
    except ValueError:
        return None


def training(tid):
    c = conn()
    r = c.execute("""SELECT t.*, COUNT(r.id) cnt
                     FROM trainings t LEFT JOIN registrations r ON r.training_id=t.id
                     WHERE t.id=? GROUP BY t.id""", (tid,)).fetchone()
    c.close()
    return r


def all_trainings(include_archived=False):
    c = conn()
    q = """SELECT t.*, COUNT(r.id) cnt
           FROM trainings t LEFT JOIN registrations r ON r.training_id=t.id"""
    if not include_archived:
        q += " WHERE COALESCE(t.archived,0)=0"
    q += " GROUP BY t.id ORDER BY t.id DESC"
    rs = c.execute(q).fetchall()
    c.close()
    return rs


def training_text(t):
    return (
        f"📅 {t['date']} • {t['time']}\n"
        f"📍 {t['venue'] or 'Место уточняется'}"
        + (f"\n🏠 {t['address']}" if t["address"] else "")
        + (f"\n🎯 Дистанция: {t['distance']}" if t["distance"] else "")
        + f"\n👥 Записано: {t['cnt']}/{t['capacity']}"
        + (f"\nℹ️ {t['note']}" if t["note"] else "")
    )


def map_url(address):
    return "https://yandex.ru/maps/?text=" + quote_plus(address)


def menu(uid):
    rows = [
        [btn("🎯 Записаться", "book")],
        [btn("📋 Мои записи", "mine"), btn("❌ Отменить", "cancel")],
        [btn("📍 Где тренируемся", "places"), btn("🎒 Что взять", "memo")],
        [btn("☎️ Связь", "contacts")],
    ]
    if admin(uid):
        rows.append([btn("⚙️ Администрирование", "admin")])
    send(uid, "🎯 Спортивное метание ножа | Самара\n\nВыберите действие:", rows)


def amenu(uid):
    if not admin(uid):
        return menu(uid)
    send(uid, "⚙️ Администрирование", [
        [btn("➕ Создать тренировку", "a:new"), btn("✏️ Изменить", "a:edit")],
        [btn("👥 Записавшиеся", "a:list"), btn("🧑‍💼 Удалить участника", "a:remove")],
        [btn("✅ Посещаемость", "a:attendance"), btn("📊 Статистика", "a:stats")],
        [btn("📢 Рассылка", "a:broadcast"), btn("📥 Выгрузка CSV", "a:export")],
        [btn("🔴 Закрыть", "a:close"), btn("🟢 Открыть", "a:open")],
        [btn("🗃 Архивировать", "a:archive"), btn("🗂 Архив", "a:archives")],
        [btn("🗑 Удалить", "a:delete")],
        [btn("⬅️ Главное меню", "main")],
    ])


def pick(uid, action, title, filt="all", archived=False):
    rs = all_trainings(include_archived=archived)
    if filt == "active":
        rs = [r for r in rs if r["active"] and not r["archived"]]
    elif filt == "inactive":
        rs = [r for r in rs if not r["active"] and not r["archived"]]
    elif filt == "archive":
        rs = [r for r in rs if r["archived"]]
    else:
        rs = [r for r in rs if not r["archived"]]
    if not rs:
        return send(uid, "Подходящих тренировок нет.", [[btn("⬅️ Админ-меню", "admin")]])
    rows = []
    for r in rs:
        icon = "🗃" if r["archived"] else ("🟢" if r["active"] else "🔴")
        rows.append([btn(f"{icon} #{r['id']} {r['date']} • {r['time']} ({r['cnt']}/{r['capacity']})",
                         f"{action}:{r['id']}")])
    rows.append([btn("⬅️ Админ-меню", "admin")])
    send(uid, title, rows)


def notify_training_users(tid, text):
    c = conn()
    users = c.execute("SELECT user_id FROM registrations WHERE training_id=?", (tid,)).fetchall()
    c.close()
    for r in users:
        send(r["user_id"], text)


def promote_waitlist(tid):
    t = training(tid)
    if not t or not t["active"] or t["cnt"] >= t["capacity"]:
        return
    c = conn()
    w = c.execute("SELECT * FROM waitlist WHERE training_id=? ORDER BY id LIMIT 1", (tid,)).fetchone()
    if w:
        c.execute("DELETE FROM waitlist WHERE id=?", (w["id"],))
        c.commit()
    c.close()
    if w:
        send(w["user_id"],
             f"🎉 Освободилось место!\n\n{training_text(t)}\n\n"
             "Место не бронируется автоматически. Нажмите «Записаться», чтобы занять его.",
             [[btn("🎯 Записаться", "book")]])


def mine(uid, cancel=False):
    c = conn()
    rs = c.execute("""SELECT r.id,t.*,r.name,r.gender,r.category,r.svo
                      FROM registrations r JOIN trainings t ON t.id=r.training_id
                      WHERE r.user_id=? ORDER BY r.id DESC""", (uid,)).fetchall()
    c.close()
    if not rs:
        return send(uid, "У вас пока нет записей.", [[btn("⬅️ Главное меню", "main")]])
    if cancel:
        rows = [[btn(f"❌ {r['date']} • {r['time']}", f"del:{r['id']}")] for r in rs]
        rows.append([btn("⬅️ Главное меню", "main")])
        return send(uid, "Какую запись отменить?", rows)
    blocks = []
    for r in rs:
        block = (f"📅 {r['date']} • {r['time']}\n📍 {r['venue'] or 'Место уточняется'}"
                 + (f"\n🏠 {r['address']}" if r["address"] else "")
                 + (f"\n🎯 {r['distance']}" if r["distance"] else "")
                 + f"\n👤 {r['name']}\n🚻 {r['gender'] or '—'}"
                 + f"\n🏷 {r['category']}\n🎖 СВО/ветеран: {r['svo'] or '—'}")
        blocks.append(block)
    send(uid, "📋 Ваши записи:\n\n" + "\n\n".join(blocks),
         [[btn("⬅️ Главное меню", "main")]])


def show_participants(uid, tid, removal=False):
    c = conn()
    t = c.execute("SELECT * FROM trainings WHERE id=?", (tid,)).fetchone()
    rs = c.execute("""SELECT id,name,phone,category,user_id,gender,svo,attendance
                      FROM registrations WHERE training_id=? ORDER BY id""", (tid,)).fetchall()
    c.close()
    if not t:
        return amenu(uid)
    if removal:
        if not rs:
            return send(uid, "Записавшихся нет.", [[btn("⬅️ Админ-меню", "admin")]])
        rows = [[btn(f"❌ {r['name']}", f"a:rmreg:{r['id']}")] for r in rs]
        rows.append([btn("⬅️ Админ-меню", "admin")])
        return send(uid, f"Удаление участника — {t['date']} • {t['time']}", rows)
    if not rs:
        text = f"👥 {t['date']} • {t['time']}\n\nЗаписавшихся пока нет."
    else:
        text = f"👥 {t['date']} • {t['time']}\n\n" + "\n\n".join(
            f"{i}. {r['name']}\n📞 {r['phone']}\n🚻 {r['gender'] or '—'}"
            f"\n🏷 {r['category']}\n🎖 СВО/ветеран: {r['svo'] or '—'}"
            f"\n✅ Посещение: {r['attendance'] or 'не отмечено'}"
            for i, r in enumerate(rs, 1))
    send(uid, text, [[btn("⬅️ Админ-меню", "admin")]])


def attendance_list(uid, tid):
    c = conn()
    rs = c.execute("SELECT id,name,attendance FROM registrations WHERE training_id=? ORDER BY id",
                   (tid,)).fetchall()
    c.close()
    if not rs:
        return send(uid, "Записавшихся нет.", [[btn("⬅️ Админ-меню", "admin")]])
    rows = []
    for r in rs:
        mark = "✅" if r["attendance"] == "Пришёл" else ("❌" if r["attendance"] == "Не пришёл" else "▫️")
        rows.append([btn(f"{mark} {r['name']}", f"a:attone:{r['id']}")])
    rows.append([btn("⬅️ Админ-меню", "admin")])
    send(uid, "✅ Выберите участника:", rows)


def stats(uid, tid):
    c = conn()
    total = c.execute("SELECT COUNT(*) n FROM registrations WHERE training_id=?", (tid,)).fetchone()["n"]
    men = c.execute("SELECT COUNT(*) n FROM registrations WHERE training_id=? AND gender='Мужчина'", (tid,)).fetchone()["n"]
    women = c.execute("SELECT COUNT(*) n FROM registrations WHERE training_id=? AND gender='Женщина'", (tid,)).fetchone()["n"]
    poda = c.execute("SELECT COUNT(*) n FROM registrations WHERE training_id=? AND category='ПОДА'", (tid,)).fetchone()["n"]
    svo = c.execute("SELECT COUNT(*) n FROM registrations WHERE training_id=? AND svo='Да'", (tid,)).fetchone()["n"]
    came = c.execute("SELECT COUNT(*) n FROM registrations WHERE training_id=? AND attendance='Пришёл'", (tid,)).fetchone()["n"]
    missed = c.execute("SELECT COUNT(*) n FROM registrations WHERE training_id=? AND attendance='Не пришёл'", (tid,)).fetchone()["n"]
    waiting = c.execute("SELECT COUNT(*) n FROM waitlist WHERE training_id=?", (tid,)).fetchone()["n"]
    c.close()
    t = training(tid)
    send(uid, f"📊 Статистика\n\n{training_text(t)}\n\n"
              f"🚻 Мужчины: {men}\n🚻 Женщины: {women}\n"
              f"♿ ПОДА: {poda}\n🎖 СВО/ветераны: {svo}\n"
              f"✅ Пришли: {came}\n❌ Не пришли: {missed}\n"
              f"⏳ Лист ожидания: {waiting}\n👥 Всего записано: {total}",
         [[btn("⬅️ Админ-меню", "admin")]])


def start_booking(uid, tid):
    t = training(tid)
    if not t or not t["active"] or t["archived"]:
        return send(uid, "Запись на эту тренировку закрыта.", [[btn("⬅️ Главное меню", "main")]])
    c = conn()
    exists = c.execute("SELECT 1 FROM registrations WHERE training_id=? AND user_id=?", (tid, uid)).fetchone()
    c.close()
    if exists:
        return send(uid, "Вы уже записаны.", [[btn("📋 Мои записи", "mine")]])
    if t["cnt"] >= t["capacity"]:
        return send(uid, "Свободных мест нет. Добавить вас в лист ожидания?",
                    [[btn("⏳ Да, в лист ожидания", f"wait:{tid}")],
                     [btn("⬅️ Главное меню", "main")]])
    states[uid] = {"step": "name", "training_id": tid}
    send(uid, "Введите ФИО полностью.\nНапример: Иванов Иван Иванович")


def export_token(tid):
    return f"{SECRET}:{tid}"


def callback(u, uid):
    payload, cid = callback_data(u)
    answer(cid)

    if payload == "main":
        states.pop(uid, None); return menu(uid)
    if payload == "admin":
        states.pop(uid, None); return amenu(uid)
    if payload == "mine":
        return mine(uid)
    if payload == "cancel":
        return mine(uid, True)
    if payload == "contacts":
        return send(uid, "☎️ Связь с организаторами\n\n"
                    f"Максим Меделяев: {CONTACT_MAXIM}\n"
                    f"Яковлев Андрей Владимирович,\nпрезидент Федерации спортивного метания ножа: {CONTACT_YAKOVLEV}",
                    [[btn("⬅️ Главное меню", "main")]])
    if payload == "memo":
        return send(uid, "🎒 Что взять на тренировку\n\n"
                    "• удобную спортивную одежду и закрытую обувь;\n"
                    "• воду;\n"
                    "• при необходимости — личные средства реабилитации;\n"
                    "• прибыть заранее.\n\n"
                    "Инвентарь и дополнительные требования уточняйте у организаторов.",
                    [[btn("☎️ Связь", "contacts")], [btn("⬅️ Главное меню", "main")]])
    if payload == "places":
        rs = [r for r in all_trainings() if r["active"]]
        if not rs:
            return send(uid, "Открытых тренировок сейчас нет.", [[btn("⬅️ Главное меню", "main")]])
        text = "📍 Ближайшие тренировки:\n\n" + "\n\n".join(training_text(r) for r in rs[:5])
        return send(uid, text, [[btn("⬅️ Главное меню", "main")]])

    if payload == "book":
        rs = [r for r in all_trainings() if r["active"] and not r["archived"]]
        if not rs:
            return send(uid, "Сейчас нет открытых тренировок.", [[btn("⬅️ Главное меню", "main")]])
        rows = [[btn(f"🎯 {r['date']} • {r['time']} — {max(0,r['capacity']-r['cnt'])} мест",
                     f"choose:{r['id']}")] for r in rs]
        rows.append([btn("⬅️ Главное меню", "main")])
        return send(uid, "Выберите тренировку:", rows)

    if payload.startswith("choose:"):
        return start_booking(uid, int(payload.split(":")[-1]))

    if payload.startswith("wait:"):
        tid = int(payload.split(":")[-1])
        c = conn()
        try:
            c.execute("INSERT INTO waitlist(training_id,user_id,created_at) VALUES(?,?,?)",
                      (tid, uid, datetime.now().isoformat(timespec="seconds")))
            c.commit()
            msg = "✅ Вы добавлены в лист ожидания. Если место освободится, бот сообщит вам."
        except sqlite3.IntegrityError:
            msg = "Вы уже находитесь в листе ожидания."
        c.close()
        return send(uid, msg, [[btn("⬅️ Главное меню", "main")]])

    if payload.startswith("gender:"):
        st = states.get(uid, {})
        if st.get("step") != "gender": return menu(uid)
        st["gender"] = payload.split(":", 1)[1]
        st["step"] = "category"
        return send(uid, "🏷 Выберите категорию:",
                    [[btn("Общая", "cat:Общая"), btn("ПОДА", "cat:ПОДА")],
                     [btn("⬅️ Отмена", "main")]])

    if payload.startswith("cat:"):
        st = states.get(uid, {})
        if st.get("step") != "category": return menu(uid)
        st["category"] = payload.split(":", 1)[1]
        st["step"] = "svo"
        return send(uid, "🎖 Участник СВО / ветеран?",
                    [[btn("Да", "svo:Да"), btn("Нет", "svo:Нет")],
                     [btn("⬅️ Отмена", "main")]])

    if payload.startswith("svo:"):
        st = states.get(uid, {})
        if st.get("step") != "svo": return menu(uid)
        st["svo"] = payload.split(":", 1)[1]
        st["step"] = "consent"
        return send(uid, "🔐 Согласие на обработку персональных данных\n\n"
                    "Для записи бот сохраняет ФИО, телефон, пол, категорию, ответ о статусе "
                    "участника СВО/ветерана и MAX ID. Данные используются для организации тренировок.\n\n"
                    "Нажимая «Согласен», вы подтверждаете согласие на обработку указанных данных.",
                    [[btn("✅ Согласен и записаться", "consent:yes")],
                     [btn("❌ Не согласен", "consent:no")]])

    if payload == "consent:no":
        states.pop(uid, None)
        return send(uid, "Запись отменена. Данные не сохранены.", [[btn("⬅️ Главное меню", "main")]])

    if payload == "consent:yes":
        st = states.get(uid, {})
        if st.get("step") != "consent": return menu(uid)
        tid = st["training_id"]
        t = training(tid)
        if not t or not t["active"] or t["cnt"] >= t["capacity"]:
            states.pop(uid, None)
            return send(uid, "Места уже закончились. Можно записаться в лист ожидания.",
                        [[btn("⏳ В лист ожидания", f"wait:{tid}")], [btn("⬅️ Меню", "main")]])
        c = conn()
        try:
            now = datetime.now().isoformat(timespec="seconds")
            c.execute("""INSERT INTO registrations
                         (training_id,user_id,name,phone,category,created_at,gender,svo,consent_at)
                         VALUES(?,?,?,?,?,?,?,?,?)""",
                      (tid,uid,st["name"],st["phone"],st["category"],now,st["gender"],st["svo"],now))
            c.execute("DELETE FROM waitlist WHERE training_id=? AND user_id=?", (tid,uid))
            c.commit()
        except sqlite3.IntegrityError:
            c.close(); states.pop(uid, None)
            return send(uid, "Вы уже записаны.", [[btn("📋 Мои записи", "mine")]])
        c.close()
        states.pop(uid, None)
        t = training(tid)
        msg = (f"✅ Вы записаны!\n\n{training_text(t)}\n"
               f"\n👤 {st['name']}\n📞 {st['phone']}\n🚻 Пол: {st['gender']}"
               f"\n🏷 Категория: {st['category']}\n🎖 Участник СВО/ветеран: {st['svo']}")
        rows = []
        if t["address"]:
            rows.append([link_btn("🗺 Открыть на карте", map_url(t["address"]))])
        rows.extend([[btn("📋 Мои записи", "mine")], [btn("⬅️ Главное меню", "main")]])
        send(uid, msg, rows)
        for aid in ADMINS:
            send(aid, f"🎯 Новая запись\n\n{training_text(t)}\n\n👤 {st['name']}\n📞 {st['phone']}"
                      f"\n🚻 {st['gender']}\n🏷 {st['category']}\n🎖 СВО/ветеран: {st['svo']}")
        return

    if payload.startswith("del:"):
        rid = int(payload.split(":")[-1])
        c = conn()
        r = c.execute("""SELECT r.training_id,t.date,t.time FROM registrations r
                         JOIN trainings t ON t.id=r.training_id WHERE r.id=? AND r.user_id=?""",
                      (rid,uid)).fetchone()
        if r:
            c.execute("DELETE FROM registrations WHERE id=? AND user_id=?", (rid,uid)); c.commit()
        c.close()
        if r:
            for aid in ADMINS:
                send(aid, f"❌ Участник отменил запись\n📅 {r['date']} • {r['time']}")
            promote_waitlist(r["training_id"])
            return send(uid, "✅ Запись отменена.", [[btn("⬅️ Главное меню", "main")]])
        return send(uid, "Запись не найдена.", [[btn("⬅️ Главное меню", "main")]])

    if not admin(uid):
        return menu(uid)

    if payload == "a:new":
        states[uid] = {"step":"adate"}
        return send(uid, "📅 Введите дату ДД.ММ.ГГГГ\nНапример: 23.09.2026")
    if payload == "a:list": return pick(uid,"a:show","Выберите тренировку:")
    if payload == "a:remove": return pick(uid,"a:removelist","Выберите тренировку:")
    if payload == "a:attendance": return pick(uid,"a:attlist","Выберите тренировку:")
    if payload == "a:stats": return pick(uid,"a:statone","Выберите тренировку:")
    if payload == "a:broadcast": return pick(uid,"a:bcastone","Кому отправить сообщение?")
    if payload == "a:export": return pick(uid,"a:exportone","Какую тренировку выгрузить?")
    if payload == "a:edit": return pick(uid,"a:editone","Какую тренировку изменить?")
    if payload == "a:close": return pick(uid,"a:doclose","Какую закрыть?","active")
    if payload == "a:open": return pick(uid,"a:doopen","Какую открыть?","inactive")
    if payload == "a:archive": return pick(uid,"a:doarchive","Какую архивировать?")
    if payload == "a:archives": return pick(uid,"a:archshow","Архив:", "archive", True)
    if payload == "a:delete": return pick(uid,"a:askdelete","Какую удалить?")

    if payload.startswith("a:show:"): return show_participants(uid,int(payload.split(":")[-1]))
    if payload.startswith("a:removelist:"): return show_participants(uid,int(payload.split(":")[-1]),True)
    if payload.startswith("a:attlist:"): return attendance_list(uid,int(payload.split(":")[-1]))
    if payload.startswith("a:statone:"): return stats(uid,int(payload.split(":")[-1]))

    if payload.startswith("a:attone:"):
        rid=int(payload.split(":")[-1])
        c=conn(); r=c.execute("SELECT name,attendance FROM registrations WHERE id=?",(rid,)).fetchone(); c.close()
        if not r: return amenu(uid)
        return send(uid,f"Посещаемость: {r['name']}",
                    [[btn("✅ Пришёл",f"a:attset:{rid}:yes"),btn("❌ Не пришёл",f"a:attset:{rid}:no")],
                     [btn("⬅️ Админ-меню","admin")]])

    if payload.startswith("a:attset:"):
        _,_,rid,val=payload.split(":")
        value="Пришёл" if val=="yes" else "Не пришёл"
        c=conn(); c.execute("UPDATE registrations SET attendance=? WHERE id=?",(value,int(rid))); c.commit(); c.close()
        return send(uid,f"✅ Отмечено: {value}",[[btn("⬅️ Админ-меню","admin")]])

    if payload.startswith("a:rmreg:"):
        rid=int(payload.split(":")[-1])
        c=conn()
        r=c.execute("""SELECT r.training_id,r.user_id,r.name,t.date,t.time FROM registrations r
                       JOIN trainings t ON t.id=r.training_id WHERE r.id=?""",(rid,)).fetchone()
        if r:
            c.execute("DELETE FROM registrations WHERE id=?",(rid,)); c.commit()
        c.close()
        if r:
            send(r["user_id"],f"❌ Администратор отменил вашу запись на {r['date']} в {r['time']}.")
            promote_waitlist(r["training_id"])
            return send(uid,f"✅ {r['name']} удалён(а).",[[btn("⬅️ Админ-меню","admin")]])
        return amenu(uid)

    if payload.startswith("a:bcastone:"):
        tid=int(payload.split(":")[-1])
        states[uid]={"step":"broadcast","training_id":tid}
        return send(uid,"📢 Введите сообщение. Оно будет отправлено всем записанным на эту тренировку.")

    if payload.startswith("a:exportone:"):
        tid=int(payload.split(":")[-1])
        url=f"{PUBLIC_URL}/export/{tid}?key={quote_plus(export_token(tid))}"
        return send(uid,f"📥 Выгрузка участников CSV:\n{url}\n\nСсылка предназначена для администратора.",
                    [[btn("⬅️ Админ-меню","admin")]])

    if payload.startswith("a:editone:"):
        tid=int(payload.split(":")[-1])
        t=training(tid)
        if not t: return amenu(uid)
        states[uid]={"step":"editfield","training_id":tid}
        return send(uid,f"✏️ Что изменить?\n\n{training_text(t)}",
                    [[btn("📅 Дату","edit:date"),btn("🕒 Время","edit:time")],
                     [btn("📍 Площадку","edit:venue"),btn("🏠 Адрес","edit:address")],
                     [btn("🎯 Дистанцию","edit:distance"),btn("👥 Кол-во мест","edit:capacity")],
                     [btn("ℹ️ Примечание","edit:note")],[btn("⬅️ Админ-меню","admin")]])

    if payload.startswith("edit:"):
        st=states.get(uid,{})
        if st.get("step")!="editfield": return amenu(uid)
        field=payload.split(":",1)[1]
        st["field"]=field; st["step"]="editvalue"
        labels={"date":"дату ДД.ММ.ГГГГ","time":"время ЧЧ:ММ","venue":"название площадки",
                "address":"полный адрес","distance":"дистанцию (например: 3 м / 5 м)",
                "capacity":"количество мест","note":"примечание"}
        return send(uid,f"Введите {labels[field]}:")

    if payload.startswith("a:doclose:") or payload.startswith("a:doopen:"):
        open_it=payload.startswith("a:doopen:")
        tid=int(payload.split(":")[-1])
        c=conn(); c.execute("UPDATE trainings SET active=? WHERE id=?",(1 if open_it else 0,tid)); c.commit(); c.close()
        return send(uid,"🟢 Запись открыта." if open_it else "🔴 Запись закрыта.",
                    [[btn("⬅️ Админ-меню","admin")]])

    if payload.startswith("a:doarchive:"):
        tid=int(payload.split(":")[-1])
        c=conn(); c.execute("UPDATE trainings SET archived=1,active=0 WHERE id=?",(tid,)); c.commit(); c.close()
        return send(uid,"🗃 Тренировка перенесена в архив.",[[btn("⬅️ Админ-меню","admin")]])

    if payload.startswith("a:archshow:"):
        t=training(int(payload.split(":")[-1]))
        return send(uid,"🗃 Архивная тренировка\n\n"+training_text(t),[[btn("⬅️ Админ-меню","admin")]])

    if payload.startswith("a:askdelete:"):
        tid=int(payload.split(":")[-1]); t=training(tid)
        return send(uid,f"⚠️ Удалить тренировку?\n\n{training_text(t)}",
                    [[btn("🗑 Да, удалить",f"a:dodelete:{tid}")],[btn("⬅️ Отмена","admin")]])

    if payload.startswith("a:dodelete:"):
        tid=int(payload.split(":")[-1]); t=training(tid)
        notify_training_users(tid,f"⚠️ Тренировка {t['date']} в {t['time']} отменена администратором.")
        c=conn()
        c.execute("DELETE FROM registrations WHERE training_id=?",(tid,))
        c.execute("DELETE FROM waitlist WHERE training_id=?",(tid,))
        c.execute("DELETE FROM trainings WHERE id=?",(tid,))
        c.commit(); c.close()
        return send(uid,"🗑 Тренировка удалена.",[[btn("⬅️ Админ-меню","admin")]])

    menu(uid)


def handle_text(uid, text):
    if text.lower() in ("/start","start","старт","меню"):
        states.pop(uid,None); return menu(uid)
    st=states.get(uid)
    if not st: return menu(uid)
    step=st.get("step")

    if step=="name":
        name=" ".join(text.strip().split())
        if not valid_name(name):
            return send(uid,"⚠️ Проверьте ФИО. Минимум фамилия и имя, без цифр.")
        st["name"]=name; st["step"]="phone"
        return send(uid,"📞 Введите российский мобильный номер.\nНапример: +7 927 123-45-67")

    if step=="phone":
        p=normalize_phone(text)
        if not p: return send(uid,"⚠️ Номер указан неверно. Например: +7 927 123-45-67")
        st["phone"]=p; st["step"]="gender"
        return send(uid,"🚻 Выберите пол:",
                    [[btn("Мужчина","gender:Мужчина"),btn("Женщина","gender:Женщина")],
                     [btn("⬅️ Отмена","main")]])

    if step=="adate":
        try:
            d=datetime.strptime(text,"%d.%m.%Y").date()
            if d<datetime.now().date(): raise ValueError
        except ValueError:
            return send(uid,"⚠️ Неверная или прошедшая дата. Формат: ДД.ММ.ГГГГ")
        st["date"]=text; st["step"]="atime"; return send(uid,"🕒 Введите время ЧЧ:ММ")

    if step=="atime":
        try: datetime.strptime(text,"%H:%M")
        except ValueError: return send(uid,"⚠️ Неверное время. Например: 17:00")
        st["time"]=text; st["step"]="avenue"; return send(uid,"📍 Введите название площадки.\nНапример: тир «Аверс»")

    if step=="avenue":
        if len(text.strip())<2: return send(uid,"⚠️ Введите название площадки.")
        st["venue"]=text.strip(); st["step"]="aaddress"; return send(uid,"🏠 Введите полный адрес.")

    if step=="aaddress":
        if len(text.strip())<5: return send(uid,"⚠️ Введите полный адрес.")
        st["address"]=text.strip(); st["step"]="adistance"; return send(uid,"🎯 Введите дистанцию.\nНапример: 3 м / 5 м")

    if step=="adistance":
        st["distance"]=text.strip(); st["step"]="acap"; return send(uid,"👥 Введите количество мест (1–500).")

    if step=="acap":
        try:
            cap=int(text)
            if not 1<=cap<=500: raise ValueError
        except ValueError: return send(uid,"⚠️ Введите целое число от 1 до 500.")
        st["capacity"]=cap; st["step"]="anote"
        return send(uid,"ℹ️ Введите примечание для участников.\nЕсли примечания нет — отправьте дефис: -")

    if step=="anote":
        note="" if text.strip()=="-" else text.strip()
        c=conn()
        c.execute("""INSERT INTO trainings(date,time,capacity,active,venue,address,distance,note,archived,reminder_sent)
                     VALUES(?,?,?,?,?,?,?,?,0,0)""",
                  (st["date"],st["time"],st["capacity"],1,st["venue"],st["address"],st["distance"],note))
        c.commit(); tid=c.execute("SELECT last_insert_rowid()").fetchone()[0]; c.close()
        states.pop(uid,None); t=training(tid)
        return send(uid,"✅ Тренировка создана!\n\n"+training_text(t),[[btn("⬅️ Админ-меню","admin")]])

    if step=="broadcast":
        tid=st["training_id"]
        if len(text)>2000: return send(uid,"⚠️ Сообщение слишком длинное. Сократите до 2000 символов.")
        notify_training_users(tid,"📢 Сообщение организатора\n\n"+text)
        states.pop(uid,None)
        return send(uid,"✅ Сообщение отправлено участникам.",[[btn("⬅️ Админ-меню","admin")]])

    if step=="editvalue":
        tid=st["training_id"]; field=st["field"]; value=text.strip()
        if field=="date":
            try:
                d=datetime.strptime(value,"%d.%m.%Y").date()
                if d<datetime.now().date(): raise ValueError
            except ValueError: return send(uid,"⚠️ Неверная дата.")
        elif field=="time":
            try: datetime.strptime(value,"%H:%M")
            except ValueError: return send(uid,"⚠️ Неверное время.")
        elif field=="capacity":
            try:
                value=int(value)
                if not 1<=value<=500: raise ValueError
            except ValueError: return send(uid,"⚠️ Количество мест: от 1 до 500.")
            t=training(tid)
            if value<t["cnt"]: return send(uid,f"⚠️ Уже записано {t['cnt']} человек. Нельзя поставить меньше.")
        c=conn()
        c.execute(f"UPDATE trainings SET {field}=?, reminder_sent=0 WHERE id=?",(value,tid))
        c.commit(); c.close()
        states.pop(uid,None); t=training(tid)
        c2=conn()
        users=c2.execute("SELECT user_id FROM registrations WHERE training_id=?",(tid,)).fetchall()
        c2.close()
        for ur in users:
            rows=[[link_btn("🗺 Открыть на карте",map_url(t["address"]))]] if t["address"] else None
            send(ur["user_id"],"⚠️ Изменение по вашей тренировке\n\n"+training_text(t),rows)
        return send(uid,"✅ Изменения сохранены. Записанные участники уведомлены.\n\n"+training_text(t),
                    [[btn("⬅️ Админ-меню","admin")]])

    menu(uid)


def reminder_worker():
    while True:
        try:
            now=datetime.now()
            c=conn()
            rs=c.execute("""SELECT * FROM trainings
                            WHERE active=1 AND archived=0 AND COALESCE(reminder_sent,0)=0""").fetchall()
            c.close()
            for t0 in rs:
                dt=parse_dt(t0["date"],t0["time"])
                if not dt: continue
                delta=dt-now
                if timedelta(hours=23) <= delta <= timedelta(hours=25):
                    # Claim before sending, preventing duplicate reminders from multiple workers.
                    c=conn()
                    cur=c.execute("""UPDATE trainings SET reminder_sent=1
                                     WHERE id=? AND COALESCE(reminder_sent,0)=0""",(t0["id"],))
                    c.commit(); claimed=cur.rowcount==1; c.close()
                    if claimed:
                        t=training(t0["id"])
                        msg="⏰ Напоминание: тренировка завтра!\n\n"+training_text(t)
                        c2=conn()
                        users=c2.execute("SELECT user_id FROM registrations WHERE training_id=?",(t["id"],)).fetchall()
                        c2.close()
                        for ur in users:
                            rows=[[link_btn("🗺 Открыть на карте",map_url(t["address"]))]] if t["address"] else None
                            send(ur["user_id"],msg,rows)
                elif dt < now - timedelta(hours=6):
                    c=conn(); c.execute("UPDATE trainings SET archived=1,active=0 WHERE id=?",(t0["id"],)); c.commit(); c.close()
        except Exception as e:
            print("REMINDER ERROR",repr(e),flush=True)
        time.sleep(300)


def start_reminder_thread():
    global reminder_thread_started
    if not reminder_thread_started:
        reminder_thread_started=True
        threading.Thread(target=reminder_worker,daemon=True).start()


start_reminder_thread()


@app.get("/")
def home():
    return "MAX knife training bot: OK",200


@app.post("/webhook")
def webhook():
    if SECRET and request.headers.get("X-Max-Bot-Api-Secret") != SECRET:
        return "forbidden",403
    u=request.get_json(silent=True) or {}
    print("UPDATE",u,flush=True)
    uid=uid_of(u)
    if uid:
        if (u.get("update_type") or u.get("type",""))=="message_callback" or u.get("callback"):
            callback(u,uid)
        elif u.get("message") or u.get("update_type") in ("message_created","bot_started"):
            handle_text(uid,text_of(u) or "/start")
    return jsonify({"ok":True})


@app.get("/export/<int:tid>")
def export_csv(tid):
    if request.args.get("key","") != export_token(tid):
        return "forbidden",403
    c=conn()
    t=c.execute("SELECT * FROM trainings WHERE id=?",(tid,)).fetchone()
    rs=c.execute("""SELECT name,phone,gender,category,svo,attendance,created_at
                    FROM registrations WHERE training_id=? ORDER BY id""",(tid,)).fetchall()
    c.close()
    if not t: return "not found",404
    sio=io.StringIO()
    w=csv.writer(sio,delimiter=";")
    w.writerow(["ФИО","Телефон","Пол","Категория","Участник СВО/ветеран","Посещение","Дата записи"])
    for r in rs:
        w.writerow([r["name"],r["phone"],r["gender"],r["category"],r["svo"],r["attendance"],r["created_at"]])
    data="\ufeff"+sio.getvalue()
    filename=f"training_{tid}_{t['date'].replace('.','-')}.csv"
    return Response(data,mimetype="text/csv; charset=utf-8",
                    headers={"Content-Disposition":f'attachment; filename="{filename}"'})


# /setup intentionally disabled after successful webhook registration.
@app.get("/setup")
def setup_disabled():
    return "setup disabled",404


if __name__=="__main__":
    app.run(host="0.0.0.0",port=int(os.environ.get("PORT","8080")))
