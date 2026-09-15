import os, sqlite3
from datetime import datetime
import requests, urllib3
from flask import Flask, request, jsonify
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app=Flask(__name__)
TOKEN=os.environ.get("MAX_TOKEN","")
SECRET=os.environ.get("WEBHOOK_SECRET","")
ADMINS={x.strip() for x in os.environ.get("ADMIN_IDS","").split(",") if x.strip()}
API="https://platform-api2.max.ru"
DB=os.environ.get("DB_PATH","/tmp/bot.db")
states={}

def db():
    c=sqlite3.connect(DB); c.row_factory=sqlite3.Row
    c.execute("CREATE TABLE IF NOT EXISTS trainings(id INTEGER PRIMARY KEY AUTOINCREMENT,date TEXT,time TEXT,capacity INTEGER,active INTEGER DEFAULT 1)")
    c.execute("CREATE TABLE IF NOT EXISTS registrations(id INTEGER PRIMARY KEY AUTOINCREMENT,training_id INTEGER,user_id TEXT,name TEXT,phone TEXT,category TEXT,created_at TEXT,UNIQUE(training_id,user_id))")
    c.commit(); return c

def admin(uid): return str(uid) in ADMINS
def hdr(): return {"Authorization":TOKEN,"Content-Type":"application/json"}
def btn(t,p): return {"type":"callback","text":t,"payload":p}
def keyboard(rows): return [{"type":"inline_keyboard","payload":{"buttons":rows}}]

def send(uid,text,rows=None):
    body={"text":text}
    if rows: body["attachments"]=keyboard(rows)
    try:
        r=requests.post(API+"/messages",params={"user_id":uid},headers=hdr(),json=body,timeout=20,verify=False)
        print("SEND",r.status_code,r.text,flush=True)
    except Exception as e: print("SEND ERROR",repr(e),flush=True)

def answer(cid):
    if not cid:return
    try: requests.post(API+"/answers",params={"callback_id":cid},headers=hdr(),json={"notification":"Готово"},timeout=20,verify=False)
    except Exception as e: print("ANSWER ERROR",repr(e),flush=True)

def menu(uid):
    rows=[[btn("🎯 Записаться","book")],[btn("📋 Мои записи","mine")],[btn("❌ Отменить запись","cancel")],[btn("ℹ️ Информация","info")]]
    if admin(uid): rows.append([btn("⚙️ Администрирование","admin")])
    send(uid,"🎯 Спортивное метание ножа | Самара\n\nВыберите действие:",rows)

def amenu(uid):
    if not admin(uid): return menu(uid)
    send(uid,"⚙️ Администрирование\n\nВыберите действие:",[
        [btn("➕ Создать тренировку","a:new")],[btn("📅 Все тренировки","a:all")],
        [btn("👥 Записавшиеся","a:list")],[btn("🔴 Закрыть запись","a:close")],
        [btn("🟢 Открыть запись","a:open")],[btn("🗑 Удалить тренировку","a:delete")],
        [btn("⬅️ Главное меню","main")]])

def trainings():
    c=db(); r=c.execute("SELECT t.*,COUNT(r.id) cnt FROM trainings t LEFT JOIN registrations r ON r.training_id=t.id GROUP BY t.id ORDER BY t.id DESC").fetchall(); c.close(); return r

def pick(uid,action,title,mode=None):
    rs=trainings()
    if mode=="active": rs=[r for r in rs if r["active"]]
    if mode=="inactive": rs=[r for r in rs if not r["active"]]
    if not rs:return send(uid,"Подходящих тренировок нет.",[[btn("⬅️ Админ-меню","admin")]])
    rows=[[btn(f"{'🟢' if r['active'] else '🔴'} #{r['id']} {r['date']} • {r['time']} ({r['cnt']}/{r['capacity']})",f"{action}:{r['id']}")] for r in rs]
    rows.append([btn("⬅️ Админ-меню","admin")]); send(uid,title,rows)

def uid_of(u):
    for path in [("message","sender","user_id"),("callback","user","user_id"),("user","user_id")]:
        x=u
        try:
            for p in path:x=x[p]
            return str(x)
        except: pass

def text_of(u):
    return (u.get("message",{}).get("body",{}).get("text") or u.get("message",{}).get("text") or "").strip()

def mine(uid,cancel=False):
    c=db(); rs=c.execute("SELECT r.id,t.date,t.time,r.name FROM registrations r JOIN trainings t ON t.id=r.training_id WHERE r.user_id=? ORDER BY t.date,t.time",(uid,)).fetchall(); c.close()
    if not rs:return send(uid,"У вас пока нет записей.",[[btn("⬅️ Главное меню","main")]])
    if cancel:return send(uid,"Какую запись отменить?",[[btn(f"❌ {r['date']} • {r['time']}",f"del:{r['id']}")] for r in rs]+[[btn("⬅️ Главное меню","main")]])
    send(uid,"📋 Ваши записи:\n\n"+"\n".join(f"• {r['date']} в {r['time']} — {r['name']}" for r in rs),[[btn("⬅️ Главное меню","main")]])

def callback(u,uid):
    c=u.get("callback") or {}; p=c.get("payload",""); answer(c.get("callback_id"))
    if p=="main": states.pop(uid,None); return menu(uid)
    if p=="admin": return amenu(uid)
    if p=="book":
        rs=[r for r in trainings() if r["active"] and r["cnt"]<r["capacity"]]
        if not rs:return send(uid,"Сейчас нет открытых тренировок.",[[btn("⬅️ Главное меню","main")]])
        return send(uid,"Выберите тренировку:",[[btn(f"📅 {r['date']} • {r['time']} • мест {r['capacity']-r['cnt']}",f"choose:{r['id']}")] for r in rs]+[[btn("⬅️ Главное меню","main")]])
    if p=="mine": return mine(uid)
    if p=="cancel": return mine(uid,True)
    if p=="info": return send(uid,"📍 Самара, ул. Пионерская, 108, тир «Аверс».\n\nЗапись на тренировки по спортивному метанию ножа.",[[btn("⬅️ Главное меню","main")]])
    if p.startswith("a:") and not admin(uid):return menu(uid)
    if p=="a:new": states[uid]={"step":"adate"}; return send(uid,"Введите дату тренировки, например: 20.09.2026")
    if p=="a:all":
        rs=trainings(); text="📅 Все тренировки:\n\n"+("\n".join(f"{'🟢' if r['active'] else '🔴'} #{r['id']} — {r['date']} {r['time']} — {r['cnt']}/{r['capacity']}" for r in rs) if rs else "Тренировок нет.")
        return send(uid,text,[[btn("⬅️ Админ-меню","admin")]])
    if p=="a:list":return pick(uid,"alist","Выберите тренировку:")
    if p=="a:close":return pick(uid,"aclose","Какую тренировку закрыть?","active")
    if p=="a:open":return pick(uid,"aopen","Какую тренировку открыть?","inactive")
    if p=="a:delete":return pick(uid,"adel","Какую тренировку удалить?")
    if p.startswith("alist:"):
        tid=int(p.split(":")[1]); c=db(); tr=c.execute("SELECT * FROM trainings WHERE id=?",(tid,)).fetchone(); rs=c.execute("SELECT * FROM registrations WHERE training_id=? ORDER BY id",(tid,)).fetchall(); c.close()
        text=f"👥 {tr['date']} • {tr['time']}\n\n"+("\n".join(f"{i+1}. {r['name']} — {r['phone']} — {r['category']}" for i,r in enumerate(rs)) if rs else "Записей пока нет.")
        return send(uid,text,[[btn("⬅️ Админ-меню","admin")]])
    if p.startswith("aclose:") or p.startswith("aopen:"):
        tid=int(p.split(":")[1]); val=0 if p.startswith("aclose:") else 1; c=db(); c.execute("UPDATE trainings SET active=? WHERE id=?",(val,tid)); c.commit(); c.close()
        return send(uid,("🔴 Запись закрыта." if not val else "🟢 Запись открыта."),[[btn("⬅️ Админ-меню","admin")]])
    if p.startswith("adel:"):
        tid=int(p.split(":")[1]); return send(uid,f"⚠️ Удалить тренировку #{tid} и все записи?",[[btn("🗑 Да, удалить",f"adelok:{tid}")],[btn("⬅️ Нет","admin")]])
    if p.startswith("adelok:"):
        tid=int(p.split(":")[1]); c=db(); c.execute("DELETE FROM registrations WHERE training_id=?",(tid,)); c.execute("DELETE FROM trainings WHERE id=?",(tid,)); c.commit(); c.close(); return send(uid,"🗑 Тренировка удалена.",[[btn("⬅️ Админ-меню","admin")]])
    if p.startswith("choose:"): states[uid]={"step":"name","tid":int(p.split(":")[1])}; return send(uid,"Введите ФИО участника:")
    if p.startswith("cat:"):
        s=states.get(uid)
        if not s:return menu(uid)
        c=db(); tr=c.execute("SELECT * FROM trainings WHERE id=?",(s["tid"],)).fetchone(); cnt=c.execute("SELECT COUNT(*) n FROM registrations WHERE training_id=?",(s["tid"],)).fetchone()["n"]
        if not tr or not tr["active"] or cnt>=tr["capacity"]: c.close(); states.pop(uid,None); return send(uid,"Места уже закончились.")
        try:c.execute("INSERT INTO registrations(training_id,user_id,name,phone,category,created_at) VALUES(?,?,?,?,?,?)",(s["tid"],uid,s["name"],s["phone"],p.split(":",1)[1],datetime.now().isoformat())); c.commit()
        except sqlite3.IntegrityError:c.close(); states.pop(uid,None); return send(uid,"Вы уже записаны на эту тренировку.")
        c.close(); states.pop(uid,None); send(uid,f"✅ Вы записаны!\n📅 {tr['date']}\n⏰ {tr['time']}\n👤 {s['name']}\n📞 {s['phone']}",[[btn("⬅️ Главное меню","main")]])
        for a in ADMINS:send(a,f"🎯 Новая запись\n{s['name']}\n📅 {tr['date']} • {tr['time']}\n📞 {s['phone']}\nMAX ID: {uid}")
        return
    if p.startswith("del:"):
        rid=int(p.split(":")[1]); c=db(); c.execute("DELETE FROM registrations WHERE id=? AND user_id=?",(rid,uid)); c.commit(); c.close(); return send(uid,"✅ Запись отменена.",[[btn("⬅️ Главное меню","main")]])

def handle_text(uid,text):
    if text.lower() in ["/start","start","старт","меню"]:states.pop(uid,None); return menu(uid)
    s=states.get(uid)
    if not s:return menu(uid)
    if s["step"]=="adate" and admin(uid):
        try:datetime.strptime(text,"%d.%m.%Y")
        except:return send(uid,"Введите дату в формате ДД.ММ.ГГГГ, например 20.09.2026")
        s["date"]=text;s["step"]="atime";return send(uid,"Введите время, например 18:00")
    if s["step"]=="atime" and admin(uid):
        try:datetime.strptime(text,"%H:%M")
        except:return send(uid,"Введите время в формате ЧЧ:ММ, например 18:00")
        s["time"]=text;s["step"]="acap";return send(uid,"Введите количество мест, например 12")
    if s["step"]=="acap" and admin(uid):
        try:cap=int(text); assert 1<=cap<=500
        except:return send(uid,"Введите число мест от 1 до 500.")
        c=db(); cur=c.execute("INSERT INTO trainings(date,time,capacity) VALUES(?,?,?)",(s["date"],s["time"],cap)); c.commit(); c.close(); tid=cur.lastrowid; date=s["date"];time=s["time"];states.pop(uid,None)
        return send(uid,f"✅ Тренировка создана!\n#{tid}\n📅 {date}\n⏰ {time}\n👥 Мест: {cap}",[[btn("⬅️ Админ-меню","admin")]])
    if s["step"]=="name":s["name"]=text;s["step"]="phone";return send(uid,"Введите номер телефона:")
    if s["step"]=="phone":s["phone"]=text;s["step"]="category";return send(uid,"Выберите категорию:",[[btn("Общая","cat:Общая"),btn("ПОДА","cat:ПОДА")],[btn("Другая","cat:Другая")]])

@app.get("/")
def health():return "MAX knife training bot: OK",200

@app.post("/webhook")
def webhook():
    if SECRET and request.headers.get("X-Max-Bot-Api-Secret")!=SECRET:return "forbidden",403
    u=request.get_json(silent=True) or {}; print("UPDATE",u,flush=True); uid=uid_of(u)
    if uid:
        if (u.get("update_type") or u.get("type",""))=="message_callback" or u.get("callback"):callback(u,uid)
        elif u.get("message") or (u.get("update_type") in ("message_created","bot_started")):handle_text(uid,text_of(u) or "/start")
    return jsonify({"ok":True})

@app.get("/setup")
def setup():
    if request.args.get("key","")!=SECRET or not TOKEN or not SECRET:return "forbidden",403
    body={"url":"https://web-production-971c2.up.railway.app/webhook","update_types":["message_created","message_callback","bot_started"],"secret":SECRET}
    r=requests.post(API+"/subscriptions",headers=hdr(),json=body,timeout=20,verify=False)
    return r.text,r.status_code,{"Content-Type":"application/json"}

if __name__=="__main__":app.run(host="0.0.0.0",port=int(os.environ.get("PORT","8080")))
