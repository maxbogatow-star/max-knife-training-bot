import os, sqlite3, secrets
from datetime import datetime
from flask import Flask, request, jsonify
import requests
import certifi

app = Flask(__name__)
TOKEN = os.environ.get('MAX_TOKEN','')
WEBHOOK_SECRET = os.environ.get('WEBHOOK_SECRET','')
ADMIN_ID = os.environ.get('ADMIN_ID','')
API = 'https://platform-api2.max.ru'
DB = os.environ.get('DB_PATH','/tmp/bot.db')

states = {}

def db():
    c=sqlite3.connect(DB); c.row_factory=sqlite3.Row
    c.execute('CREATE TABLE IF NOT EXISTS trainings(id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT, time TEXT, capacity INTEGER, active INTEGER DEFAULT 1)')
    c.execute('CREATE TABLE IF NOT EXISTS registrations(id INTEGER PRIMARY KEY AUTOINCREMENT, training_id INTEGER, user_id TEXT, name TEXT, phone TEXT, category TEXT, created_at TEXT, UNIQUE(training_id,user_id))')
    c.commit(); return c

def headers(): return {'Authorization': TOKEN, 'Content-Type':'application/json'}
def kb(rows): return [{'type':'inline_keyboard','payload':{'buttons':rows}}]
def btn(text,payload): return {'type':'callback','text':text,'payload':payload}

def send(uid,text,rows=None):
    body={'text':text}
    if rows: body['attachments']=kb(rows)
    requests.post(f'{API}/answers',params={'callback_id':cb_id},headers=headers(),json={'notification':text},timeout=15,verify=False)
    return r

def answer(cb_id,text='Готово'):
    if cb_id:
        requests.post(f'{API}/answers',params={'callback_id':cb_id},headers=headers(),json={'notification':text},timeout=15)

def main_menu(uid):
    send(uid,'🎯 Спортивное метание ножа | Самара\n\nВыберите действие:',[[btn('🎯 Записаться','book')],[btn('📋 Мои записи','mine')],[btn('❌ Отменить запись','cancel')],[btn('ℹ️ Информация','info')]])

def available():
    c=db(); rows=c.execute('''SELECT t.*, COUNT(r.id) cnt FROM trainings t LEFT JOIN registrations r ON r.training_id=t.id WHERE t.active=1 GROUP BY t.id ORDER BY t.date,t.time''').fetchall(); c.close()
    return [r for r in rows if r['cnt'] < r['capacity']]

def show_trainings(uid, action='choose'):
    rows=available()
    if not rows: send(uid,'Сейчас нет открытых тренировок. Как только появится новая дата, она будет доступна здесь.'); return
    buttons=[]
    for r in rows:
        left=r['capacity']-r['cnt']; buttons.append([btn(f"📅 {r['date']} • {r['time']} • мест {left}",f'{action}:{r["id"]}')])
    send(uid,'Выберите тренировку:',buttons)

def user_id(update):
    for path in [('message','sender','user_id'),('callback','user','user_id'),('user','user_id')]:
        x=update
        try:
            for p in path: x=x[p]
            if x is not None: return str(x)
        except Exception: pass
    return None

def text_of(update):
    try:return (update.get('message',{}).get('body',{}).get('text') or update.get('message',{}).get('text') or '').strip()
    except:return ''

def callback_of(update):
    c=update.get('callback') or {}
    return c.get('payload',''), c.get('callback_id')

def mine(uid, cancel_mode=False):
    c=db(); rows=c.execute('''SELECT r.id,t.date,t.time,r.name FROM registrations r JOIN trainings t ON t.id=r.training_id WHERE r.user_id=? ORDER BY t.date,t.time''',(uid,)).fetchall(); c.close()
    if not rows: send(uid,'У вас пока нет записей.'); return
    if cancel_mode:
        send(uid,'Какую запись отменить?',[[btn(f"❌ {r['date']} • {r['time']}",f'del:{r["id"]}')] for r in rows]); return
    send(uid,'📋 Ваши записи:\n\n'+'\n'.join(f"• {r['date']} в {r['time']} — {r['name']}" for r in rows))

def handle_callback(update,uid):
    payload,cbid=callback_of(update); answer(cbid)
    if payload=='book': show_trainings(uid); return
    if payload=='mine': mine(uid); return
    if payload=='cancel': mine(uid,True); return
    if payload=='info': send(uid,'📍 Самара, ул. Пионерская, 108, тир «Аверс».\n\nБот предназначен для записи на тренировки по спортивному метанию ножа.'); return
    if payload.startswith('choose:'):
        tid=int(payload.split(':')[1]); states[uid]={'step':'name','training_id':tid}; send(uid,'Введите ФИО участника:'); return
    if payload.startswith('cat:'):
        s=states.get(uid)
        if not s: main_menu(uid); return
        s['category']=payload.split(':',1)[1]
        c=db()
        try:
            tr=c.execute('SELECT * FROM trainings WHERE id=?',(s['training_id'],)).fetchone(); cnt=c.execute('SELECT COUNT(*) n FROM registrations WHERE training_id=?',(s['training_id'],)).fetchone()['n']
            if not tr or not tr['active'] or cnt>=tr['capacity']: send(uid,'К сожалению, места уже закончились.'); return
            c.execute('INSERT INTO registrations(training_id,user_id,name,phone,category,created_at) VALUES(?,?,?,?,?,?)',(s['training_id'],uid,s['name'],s['phone'],s['category'],datetime.now().isoformat())); c.commit()
        except sqlite3.IntegrityError:
            send(uid,'Вы уже записаны на эту тренировку.'); c.close(); states.pop(uid,None); return
        c.close(); states.pop(uid,None)
        send(uid,f"✅ Вы записаны!\n📅 {tr['date']}\n⏰ {tr['time']}\n👤 {s['name']}\n📞 {s['phone']}\nКатегория: {s['category']}")
        if ADMIN_ID: send(ADMIN_ID,f"🎯 Новая запись\n{s['name']}\n📅 {tr['date']} • {tr['time']}\n📞 {s['phone']}\nКатегория: {s['category']}\nMAX ID: {uid}")
        return
    if payload.startswith('del:'):
        rid=int(payload.split(':')[1]); c=db(); row=c.execute('SELECT * FROM registrations WHERE id=? AND user_id=?',(rid,uid)).fetchone()
        if row: c.execute('DELETE FROM registrations WHERE id=?',(rid,)); c.commit(); send(uid,'✅ Запись отменена.')
        c.close(); return

def handle_text(update,uid,text):
    if text in ['/start','start','Старт','старт','меню','Меню']: states.pop(uid,None); main_menu(uid); return
    if uid==ADMIN_ID and text.startswith('/slot '):
        try:
            _,date,time,cap=text.split(); cap=int(cap); c=db(); c.execute('INSERT INTO trainings(date,time,capacity) VALUES(?,?,?)',(date,time,cap)); c.commit(); c.close(); send(uid,f'✅ Тренировка создана: {date} {time}, мест: {cap}')
        except: send(uid,'Формат: /slot 20.09.2026 18:00 12')
        return
    if uid==ADMIN_ID and text=='/slots':
        c=db(); rows=c.execute('''SELECT t.*,COUNT(r.id) cnt FROM trainings t LEFT JOIN registrations r ON r.training_id=t.id GROUP BY t.id ORDER BY t.id DESC''').fetchall(); c.close(); send(uid,'\n'.join(f"#{r['id']} {r['date']} {r['time']} — {r['cnt']}/{r['capacity']} {'🟢' if r['active'] else '🔴'}" for r in rows) or 'Нет тренировок'); return
    if uid==ADMIN_ID and text.startswith('/close '):
        try: tid=int(text.split()[1]); c=db(); c.execute('UPDATE trainings SET active=0 WHERE id=?',(tid,)); c.commit(); c.close(); send(uid,f'🔴 Запись на тренировку #{tid} закрыта.')
        except: send(uid,'Формат: /close 1')
        return
    if uid==ADMIN_ID and text.startswith('/list '):
        try:
            tid=int(text.split()[1]); c=db(); tr=c.execute('SELECT * FROM trainings WHERE id=?',(tid,)).fetchone(); rows=c.execute('SELECT * FROM registrations WHERE training_id=? ORDER BY id',(tid,)).fetchall(); c.close(); send(uid,(f"📋 {tr['date']} {tr['time']}\n\n" if tr else '')+'\n'.join(f"{i+1}. {r['name']} — {r['phone']} — {r['category']}" for i,r in enumerate(rows)) or 'Записей нет')
        except: send(uid,'Формат: /list 1')
        return
    s=states.get(uid)
    if not s: main_menu(uid); return
    if s['step']=='name': s['name']=text; s['step']='phone'; send(uid,'Введите номер телефона (например, +7 927 000-00-00):'); return
    if s['step']=='phone': s['phone']=text; s['step']='category'; send(uid,'Выберите категорию:',[[btn('Общая','cat:Общая'),btn('ПОДА','cat:ПОДА')],[btn('Другая','cat:Другая')]]); return

@app.get('/')
def health(): return 'MAX knife training bot: OK',200

@app.post('/webhook')
def webhook():
    if WEBHOOK_SECRET and request.headers.get('X-Max-Bot-Api-Secret') != WEBHOOK_SECRET: return 'forbidden',403
    u=request.get_json(silent=True) or {}; uid=user_id(u)
    if uid:
        typ=u.get('update_type') or u.get('type','')
        if typ=='message_callback' or u.get('callback'): handle_callback(u,uid)
        elif typ in ('message_created','bot_started') or u.get('message'):
            t=text_of(u); handle_text(u,uid,t or '/start')
    return jsonify({'ok':True})

@app.get('/setup')
def setup():
    key=request.args.get('key','')
    if not TOKEN or not WEBHOOK_SECRET or key!=WEBHOOK_SECRET: return 'forbidden',403
    base=request.url_root.rstrip('/')
    body={'url':base+'/webhook','update_types':['message_created','message_callback','bot_started'],'secret':WEBHOOK_SECRET}
    r=requests.post(f'{API}/subscriptions',headers=headers(),json=body,timeout=20)
    return (r.text,r.status_code,{'Content-Type':'application/json'})

if __name__=='__main__':
    app.run(host='0.0.0.0',port=int(os.environ.get('PORT','8080')))
