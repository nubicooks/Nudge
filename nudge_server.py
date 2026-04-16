"""
Nudge — the Fair Exchange
Flask + SQLite + Twilio SMS
Deploy: Railway with persistent /data volume
"""
import os, json, sqlite3, secrets, hashlib
from datetime import datetime, timedelta
from functools import wraps
from flask import Flask, request, jsonify, send_from_directory, g, redirect

# ═══════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════
IS_RAILWAY = bool(os.environ.get('RAILWAY_ENVIRONMENT'))

app = Flask(__name__, static_folder=None)
app.secret_key = os.environ.get('SECRET_KEY', secrets.token_hex(32))

# Railway uses /data volume for persistence. Local uses ~/Nudge/
if IS_RAILWAY:
    DATA_DIR = '/data'
else:
    DATA_DIR = os.path.expanduser('~/Nudge')

os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, 'nudge.db')

BASE_URL = os.environ.get('NUDGE_URL', 'http://127.0.0.1:8888')

# Twilio
TWILIO_SID = os.environ.get('TWILIO_SID', '')
TWILIO_AUTH = os.environ.get('TWILIO_AUTH', '')
TWILIO_FROM = os.environ.get('TWILIO_FROM', '')
USE_TWILIO = bool(TWILIO_SID and TWILIO_AUTH and TWILIO_FROM)

if USE_TWILIO:
    from twilio.rest import Client as TwilioClient
    twilio = TwilioClient(TWILIO_SID, TWILIO_AUTH)
else:
    twilio = None
    print("⚠ Twilio not configured — SMS simulated")

# Stripe
STRIPE_PK = os.environ.get('STRIPE_PK', '')
STRIPE_SK = os.environ.get('STRIPE_SK', '')
USE_STRIPE = bool(STRIPE_SK)

if USE_STRIPE:
    import stripe
    stripe.api_key = STRIPE_SK
    print(f"  ✓ Stripe: {STRIPE_PK[:20]}...")
else:
    print("⚠ Stripe not configured — payments disabled")


# ═══════════════════════════════════════════════════
# DATABASE
# ═══════════════════════════════════════════════════
def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA journal_mode=WAL")
        g.db.execute("PRAGMA foreign_keys=ON")
    return g.db

@app.teardown_appcontext
def close_db(e=None):
    db = g.pop('db', None)
    if db: db.close()

def init_db():
    db = sqlite3.connect(DB_PATH)
    db.executescript("""
    CREATE TABLE IF NOT EXISTS businesses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        phone TEXT,
        email TEXT,
        session_price REAL DEFAULT 75,
        session_duration INTEGER DEFAULT 45,
        start_hour INTEGER DEFAULT 9,
        end_hour INTEGER DEFAULT 18,
        default_nudge_pct INTEGER DEFAULT 20,
        offer_timer_min INTEGER DEFAULT 30,
        max_offers INTEGER DEFAULT 2,
        platform_fee_pct INTEGER DEFAULT 20,
        api_key TEXT UNIQUE,
        admin_pin TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS clients (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        biz_id INTEGER REFERENCES businesses(id),
        name TEXT NOT NULL,
        phone TEXT,
        email TEXT,
        token TEXT UNIQUE,
        notes TEXT,
        status TEXT DEFAULT 'active',
        deleted_at TIMESTAMP,
        delete_reason TEXT,
        dob TEXT,
        gender TEXT,
        address TEXT,
        emergency_contact TEXT,
        emergency_phone TEXT,
        medical_notes TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS slots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        biz_id INTEGER REFERENCES businesses(id),
        date TEXT NOT NULL,
        time TEXT NOT NULL,
        client_id INTEGER REFERENCES clients(id),
        service TEXT,
        price REAL DEFAULT 0,
        status TEXT DEFAULT 'open',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(biz_id, date, time)
    );
    CREATE TABLE IF NOT EXISTS nudges (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        biz_id INTEGER REFERENCES businesses(id),
        from_slot_id INTEGER REFERENCES slots(id),
        to_slot_id INTEGER REFERENCES slots(id),
        from_client_id INTEGER REFERENCES clients(id),
        to_client_id INTEGER REFERENCES clients(id),
        attempt INTEGER DEFAULT 1,
        pct INTEGER DEFAULT 20,
        fee REAL DEFAULT 0,
        status TEXT DEFAULT 'pending',
        expires_at TIMESTAMP,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        resolved_at TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS reports (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        biz_id INTEGER,
        client_id INTEGER REFERENCES clients(id),
        slot_id INTEGER REFERENCES slots(id),
        report_html TEXT,
        report_token TEXT UNIQUE,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS invoices (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        biz_id INTEGER,
        client_id INTEGER REFERENCES clients(id),
        slot_id INTEGER REFERENCES slots(id),
        amount REAL,
        status TEXT DEFAULT 'unpaid',
        invoice_token TEXT UNIQUE,
        paid_at TIMESTAMP,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS nudge_attempts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        biz_id INTEGER,
        from_client_id INTEGER,
        to_slot_id INTEGER,
        count INTEGER DEFAULT 0,
        UNIQUE(biz_id, from_client_id, to_slot_id)
    );
    CREATE TABLE IF NOT EXISTS sms_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        biz_id INTEGER,
        to_phone TEXT,
        body TEXT,
        direction TEXT DEFAULT 'outbound',
        nudge_id INTEGER,
        twilio_sid TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS reviews (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        biz_id INTEGER,
        client_id INTEGER REFERENCES clients(id),
        slot_id INTEGER REFERENCES slots(id),
        rating INTEGER DEFAULT 5,
        text TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        biz_id INTEGER REFERENCES businesses(id),
        client_id INTEGER REFERENCES clients(id),
        sender TEXT NOT NULL,
        body TEXT NOT NULL,
        read INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS credits (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        biz_id INTEGER REFERENCES businesses(id),
        client_id INTEGER REFERENCES clients(id),
        amount REAL NOT NULL,
        source TEXT,
        nudge_id INTEGER,
        used_invoice_id INTEGER,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    CREATE INDEX IF NOT EXISTS idx_slots_date ON slots(biz_id, date);
    CREATE INDEX IF NOT EXISTS idx_nudges_status ON nudges(biz_id, status);
    CREATE INDEX IF NOT EXISTS idx_clients_token ON clients(token);
    CREATE INDEX IF NOT EXISTS idx_messages_client ON messages(biz_id, client_id);
    """)
    # Migrate existing databases — add new columns safely
    for col, ctype, default in [('status','TEXT',"'active'"),('deleted_at','TIMESTAMP','NULL'),('delete_reason','TEXT','NULL'),('dob','TEXT','NULL'),('gender','TEXT','NULL'),('address','TEXT','NULL'),('emergency_contact','TEXT','NULL'),('emergency_phone','TEXT','NULL'),('medical_notes','TEXT','NULL')]:
        try: db.execute(f"ALTER TABLE clients ADD COLUMN {col} {ctype} DEFAULT {default}")
        except: pass
    try: db.execute("ALTER TABLE businesses ADD COLUMN platform_fee_pct INTEGER DEFAULT 20")
    except: pass
    cur = db.execute("SELECT COUNT(*) FROM businesses")
    if cur.fetchone()[0] == 0:
        api_key = secrets.token_hex(24)
        pin = os.environ.get('ADMIN_PIN', '1234')
        db.execute("INSERT INTO businesses (name, phone, api_key, admin_pin) VALUES (?,?,?,?)",
            ('the Sol Standard', '(240) 356-3393', api_key, pin))
        print(f"✓ Business created | API key: {api_key} | PIN: {pin}")

        # Auto-seed demo clients and appointments
        today = datetime.now().strftime('%Y-%m-%d')
        tomorrow = (datetime.now() + timedelta(days=1)).strftime('%Y-%m-%d')
        day3 = (datetime.now() + timedelta(days=3)).strftime('%Y-%m-%d')
        day5 = (datetime.now() + timedelta(days=5)).strftime('%Y-%m-%d')

        # Generate slots for demo days
        for date_str in [today, tomorrow, day3, day5]:
            h, m = 9, 0
            while h < 18:
                ap = 'PM' if h >= 12 else 'AM'
                hr = h - 12 if h > 12 else (12 if h == 0 else h)
                t = f"{hr}:{m:02d} {ap}"
                db.execute("INSERT OR IGNORE INTO slots (biz_id, date, time, status) VALUES (1,?,?,'open')", (date_str, t))
                m += 45
                if m >= 60: h += 1; m -= 60

        demos = [
            ('Maya Johnson','(301) 555-0142','maya@email.com'),
            ('James Carter','(202) 555-0198','james@email.com'),
            ('Diane Williams','(240) 555-0267','diane@email.com'),
            ('Andre Thompson','(301) 555-0331','andre@email.com'),
            ('Lisa Park','(202) 555-0419','lisa@email.com'),
            ('Marcus Rivera','(240) 555-0588','marcus@email.com'),
        ]
        cids = []
        for name, phone, email in demos:
            tk = secrets.token_urlsafe(18)
            db.execute("INSERT INTO clients (biz_id,name,phone,email,token) VALUES (1,?,?,?,?)", (name, phone, email, tk))
            cids.append(db.execute("SELECT last_insert_rowid()").fetchone()[0])

        # Book appointments: 3 today, 2 tomorrow, 1 in 3 days
        book_plan = [(today,0,0),(today,2,1),(today,4,2),(tomorrow,0,3),(tomorrow,1,4),(day3,0,5)]
        for date_str, slot_idx, ci in book_plan:
            slots = db.execute("SELECT id FROM slots WHERE biz_id=1 AND date=? AND client_id IS NULL ORDER BY id", (date_str,)).fetchall()
            if slot_idx < len(slots) and ci < len(cids):
                db.execute("UPDATE slots SET client_id=?,service='Nothing Box Session',price=75,status='booked' WHERE id=?", (cids[ci], slots[slot_idx][0]))
                db.execute("INSERT INTO invoices (biz_id,client_id,slot_id,amount,invoice_token) VALUES (1,?,?,75,?)", (cids[ci], slots[slot_idx][0], secrets.token_urlsafe(18)))

        print(f"✓ Demo data seeded: {len(cids)} clients, 6 appointments")
    db.commit()
    db.close()


# ═══════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════
def gen_token():
    return secrets.token_urlsafe(18)

def send_sms(to_phone, body, biz_id=None, nudge_id=None):
    sid = None
    if USE_TWILIO and to_phone:
        try:
            msg = twilio.messages.create(body=body, from_=TWILIO_FROM, to=to_phone)
            sid = msg.sid
        except Exception as e:
            print(f"  SMS FAIL: {e}")
    else:
        print(f"  [SMS] → {to_phone}: {body[:80]}...")
    db = get_db()
    db.execute("INSERT INTO sms_log (biz_id,to_phone,body,direction,nudge_id,twilio_sid) VALUES (?,?,?,'outbound',?,?)",
        (biz_id, to_phone, body, nudge_id, sid))
    db.commit()

def get_biz(db):
    return db.execute("SELECT * FROM businesses LIMIT 1").fetchone()

def ensure_slots(db, biz_id, date_str):
    if db.execute("SELECT COUNT(*) FROM slots WHERE biz_id=? AND date=?", (biz_id, date_str)).fetchone()[0] > 0:
        return
    biz = db.execute("SELECT * FROM businesses WHERE id=?", (biz_id,)).fetchone()
    h, m = biz['start_hour'], 0
    while h < biz['end_hour']:
        ap = 'PM' if h >= 12 else 'AM'
        hr = h - 12 if h > 12 else (12 if h == 0 else h)
        t = f"{hr}:{m:02d} {ap}"
        db.execute("INSERT OR IGNORE INTO slots (biz_id,date,time,status) VALUES (?,?,?,'open')", (biz_id, date_str, t))
        m += biz['session_duration']
        if m >= 60: h += 1; m -= 60
    db.commit()

def get_attempts(db, biz_id, from_cid, to_sid):
    r = db.execute("SELECT count FROM nudge_attempts WHERE biz_id=? AND from_client_id=? AND to_slot_id=?",
        (biz_id, from_cid, to_sid)).fetchone()
    return r['count'] if r else 0

def inc_attempts(db, biz_id, from_cid, to_sid):
    c = get_attempts(db, biz_id, from_cid, to_sid)
    if c == 0:
        db.execute("INSERT INTO nudge_attempts (biz_id,from_client_id,to_slot_id,count) VALUES (?,?,?,1)", (biz_id, from_cid, to_sid))
    else:
        db.execute("UPDATE nudge_attempts SET count=count+1 WHERE biz_id=? AND from_client_id=? AND to_slot_id=?", (biz_id, from_cid, to_sid))
    db.commit()
    return c + 1

def get_credit_balance(db, biz_id, client_id):
    earned = db.execute("SELECT COALESCE(SUM(amount),0) FROM credits WHERE biz_id=? AND client_id=? AND used_invoice_id IS NULL", (biz_id, client_id)).fetchone()[0]
    return round(earned, 2)


# ═══════════════════════════════════════════════════
# AUTH
# ═══════════════════════════════════════════════════
def require_admin(f):
    @wraps(f)
    def w(*a, **kw):
        db = get_db()
        key = request.headers.get('X-API-Key') or request.args.get('api_key')
        pin = request.headers.get('X-Admin-Pin') or request.args.get('pin')
        biz = None
        if key:
            biz = db.execute("SELECT * FROM businesses WHERE api_key=?", (key,)).fetchone()
        elif pin:
            biz = db.execute("SELECT * FROM businesses WHERE admin_pin=?", (pin,)).fetchone()
        elif not IS_RAILWAY:
            biz = get_biz(db)  # Local dev: auto-auth
        if not biz:
            return jsonify({'error': 'Unauthorized'}), 401
        g.biz_id = biz['id']; g.biz = dict(biz)
        return f(*a, **kw)
    return w

def require_client(f):
    @wraps(f)
    def w(*a, **kw):
        token = kw.get('token') or request.args.get('token')
        if not token: return jsonify({'error': 'Invalid link'}), 401
        db = get_db()
        client = db.execute("SELECT * FROM clients WHERE token=?", (token,)).fetchone()
        if not client: return jsonify({'error': 'Invalid link'}), 401
        g.client_id = client['id']; g.client = dict(client)
        g.biz_id = client['biz_id']
        g.biz = dict(db.execute("SELECT * FROM businesses WHERE id=?", (client['biz_id'],)).fetchone())
        return f(*a, **kw)
    return w


# ═══════════════════════════════════════════════════
# PAGES
# ═══════════════════════════════════════════════════
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

@app.route('/')
def admin_page():
    return send_from_directory(SCRIPT_DIR, 'nudge_admin.html')

@app.route('/me/<token>')
def client_page(token):
    return send_from_directory(SCRIPT_DIR, 'nudge_client.html')

@app.route('/book')
def book_page():
    return send_from_directory(SCRIPT_DIR, 'nudge_book.html')

@app.route('/welcome/<token>')
def welcome_page(token):
    return send_from_directory(SCRIPT_DIR, 'nudge_welcome.html')

@app.route('/onboard/<token>')
def onboard_page(token):
    return send_from_directory(SCRIPT_DIR, 'nudge_onboard.html')


# ═══════════════════════════════════════════════════
# PUBLIC API — Self-service booking (no auth)
# ═══════════════════════════════════════════════════
@app.route('/api/public/slots/<date>')
def pub_slots(date):
    db = get_db()
    biz = get_biz(db)
    if not biz: return jsonify([])
    ensure_slots(db, biz['id'], date)
    rows = db.execute("""
        SELECT id, time, CASE WHEN client_id IS NOT NULL OR status='blocked' THEN 'taken' ELSE 'open' END as avail
        FROM slots WHERE biz_id=? AND date=? ORDER BY id
    """, (biz['id'], date)).fetchall()
    return jsonify([dict(r) for r in rows])

@app.route('/api/public/book', methods=['POST'])
def pub_book():
    d = request.json
    name = d.get('name', '').strip()
    phone = d.get('phone', '').strip()
    email = d.get('email', '').strip()
    slot_id = d.get('slot_id')
    if not name: return jsonify({'error': 'Name required'}), 400
    if not phone and not email: return jsonify({'error': 'Phone or email required'}), 400

    db = get_db()
    biz = get_biz(db)
    if not biz: return jsonify({'error': 'Business not found'}), 500

    slot = db.execute("SELECT * FROM slots WHERE id=? AND biz_id=? AND client_id IS NULL AND status!='blocked'",
        (slot_id, biz['id'])).fetchone()
    if not slot: return jsonify({'error': 'Slot no longer available'}), 400

    # Find or create client
    client = db.execute("SELECT * FROM clients WHERE biz_id=? AND name=? COLLATE NOCASE", (biz['id'], name)).fetchone()
    if client:
        cid = client['id']; token = client['token']
        if phone: db.execute("UPDATE clients SET phone=? WHERE id=?", (phone, cid))
        if email: db.execute("UPDATE clients SET email=? WHERE id=?", (email, cid))
    else:
        token = gen_token()
        db.execute("INSERT INTO clients (biz_id,name,phone,email,token) VALUES (?,?,?,?,?)",
            (biz['id'], name, phone, email, token))
        cid = db.execute("SELECT last_insert_rowid()").fetchone()[0]

    # Book it
    price = biz['session_price']
    db.execute("UPDATE slots SET client_id=?,service='Nothing Box Session',price=?,status='booked' WHERE id=?",
        (cid, price, slot_id))
    inv_token = gen_token()
    db.execute("INSERT INTO invoices (biz_id,client_id,slot_id,amount,invoice_token) VALUES (?,?,?,?,?)",
        (biz['id'], cid, slot_id, price, inv_token))
    db.commit()

    portal_link = f"{BASE_URL}/me/{token}"
    if phone:
        send_sms(phone, f"Hi {name.split()[0]}! Confirmed: {slot['time']} on {slot['date']} at {biz['name']}. View your appointments: {portal_link}",
            biz_id=biz['id'])

    return jsonify({'status': 'booked', 'token': token, 'portal': portal_link})

@app.route('/api/public/business')
def pub_biz():
    db = get_db()
    biz = get_biz(db)
    if not biz: return jsonify({})
    return jsonify({'name': biz['name'], 'phone': biz['phone'], 'price': biz['session_price']})

@app.route('/api/public/inquiry', methods=['POST'])
def pub_inquiry():
    d = request.json
    first = d.get('first', '').strip()
    last = d.get('last', '').strip()
    phone = d.get('phone', '').strip()
    email = d.get('email', '').strip()
    tier = d.get('tier', 'free').strip()
    if not first or not last: return jsonify({'error': 'Name required'}), 400
    if not phone and not email: return jsonify({'error': 'Phone or email required'}), 400
    name = first + ' ' + last
    tier_label = {'free': 'Try for Free', 'single': 'Single Session', 'pack': '4-Session Pack'}.get(tier, tier)
    db = get_db()
    biz = get_biz(db)
    if not biz: return jsonify({'error': 'Business not found'}), 500
    client = db.execute("SELECT * FROM clients WHERE biz_id=? AND name=? COLLATE NOCASE", (biz['id'], name)).fetchone()
    if client:
        cid = client['id']; token = client['token']
        if phone: db.execute("UPDATE clients SET phone=? WHERE id=?", (phone, cid))
        if email: db.execute("UPDATE clients SET email=? WHERE id=?", (email, cid))
        db.execute("UPDATE clients SET notes=? WHERE id=?", (f"Tier: {tier_label} | Status: Awaiting Response | Send welcome package", cid))
    else:
        token = gen_token()
        notes = f"Tier: {tier_label} | Status: Awaiting Response | Send welcome package"
        db.execute("INSERT INTO clients (biz_id,name,phone,email,token,notes) VALUES (?,?,?,?,?,?)",
            (biz['id'], name, phone, email, token, notes))
        cid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    db.commit()
    if phone:
        send_sms(phone, f"Hi {first}! Thanks for your interest in {biz['name']}. We'll be in touch soon to get your session scheduled.",
            biz_id=biz['id'])
    print(f"\n  *** NEW INQUIRY: {name} | {phone} | {email} | {tier_label} ***")
    print(f"  *** Action needed: Send welcome package ***\n")
    return jsonify({'status': 'received', 'client_id': cid})

@app.route('/report/<token>')
def view_report(token):
    db = sqlite3.connect(DB_PATH); db.row_factory = sqlite3.Row
    r = db.execute("SELECT report_html FROM reports WHERE report_token=?", (token,)).fetchone()
    db.close()
    return r['report_html'] if r else ("Report not found", 404)

@app.route('/invoice/<token>')
def view_invoice(token):
    db = sqlite3.connect(DB_PATH); db.row_factory = sqlite3.Row
    i = db.execute("""SELECT i.*,c.name,c.phone,c.email,s.date,s.time,s.service,b.name as bn,b.phone as bp
        FROM invoices i JOIN clients c ON i.client_id=c.id JOIN slots s ON i.slot_id=s.id
        JOIN businesses b ON i.biz_id=b.id WHERE i.invoice_token=?""", (token,)).fetchone()
    db.close()
    if not i: return "Not found", 404
    i = dict(i)
    status_cls = 'paid' if i['status'] == 'paid' else 'unpaid'
    pay_btn = ''
    if i['status'] != 'paid' and USE_STRIPE:
        pay_btn = f'<div style="margin-top:16px"><a href="/api/stripe/checkout/{token}" style="display:inline-block;padding:12px 32px;background:#cc0000;color:#fff;text-decoration:none;border-radius:8px;font-family:Libre Franklin;font-size:.8rem;font-weight:600">Pay Now &mdash; ${i["amount"]:.2f}</a></div>'
    return f"""<!DOCTYPE html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">
    <link href="https://fonts.googleapis.com/css2?family=Great+Vibes&family=Libre+Franklin:wght@300;400;500;600;700&family=IBM+Plex+Mono:wght@400;500;600&display=swap" rel="stylesheet">
    <style>
    *{{margin:0;padding:0;box-sizing:border-box}}
    body{{font-family:'Libre Franklin',sans-serif;max-width:560px;margin:40px auto;padding:24px;color:#2a1a0a;background:#faf5eb}}
    .header{{margin-bottom:28px}}
    h1{{font-family:'Great Vibes',cursive;color:#cc0000;font-size:2.2rem;margin-bottom:2px}}
    .product{{font-family:'IBM Plex Mono';font-size:.55rem;color:#8a7a6a;letter-spacing:2px;margin-bottom:12px}}
    .sub{{font-size:.75rem;color:#5a4a3a;margin-bottom:4px}}
    .client-info{{font-size:.6rem;color:#8a7a6a;line-height:1.8}}
    .divider{{height:1px;background:#e8dcc8;margin:20px 0}}
    table{{width:100%;border-collapse:collapse}}
    th{{text-align:left;font-size:.48rem;letter-spacing:.5px;color:#8a7a6a;padding:8px 10px;border-bottom:1px solid #e8dcc8;text-transform:uppercase}}
    th:last-child{{text-align:right}}
    td{{padding:12px 10px;border-bottom:1px solid #f2e8d6;font-size:.8rem}}
    td:last-child{{text-align:right;font-family:'IBM Plex Mono';font-weight:600}}
    .total td{{font-weight:700;font-size:1.05rem;color:#cc0000;border-bottom:none;padding-top:16px}}
    .status{{display:inline-block;padding:4px 14px;border-radius:12px;font-family:'IBM Plex Mono';font-size:.55rem;font-weight:600;letter-spacing:1px;margin-top:20px}}
    .unpaid{{background:rgba(204,0,0,.06);color:#cc0000}}
    .paid{{background:#e6f5ea;color:#1a7a2e}}
    .footer{{margin-top:32px;padding-top:20px;border-top:1px solid #e8dcc8;text-align:center}}
    .footer .biz{{font-size:.6rem;color:#5a4a3a;margin-bottom:6px}}
    .footer .contact{{font-size:.55rem;color:#8a7a6a;line-height:1.8}}
    .footer .contact a{{color:#cc0000;text-decoration:none}}
    .footer .contact a:hover{{text-decoration:underline}}
    .footer .powered{{font-family:'IBM Plex Mono';font-size:.45rem;color:#c4b5a5;margin-top:12px;letter-spacing:1px}}
    .footer .powered a{{color:#cc0000;text-decoration:none}}
    </style></head>
    <body>
    <div class="header">
      <h1>the Sol Standard</h1>
      <div class="product">THE NOTHING BOX</div>
      <div class="sub">Invoice for {i['name']}</div>
      <div class="client-info">
        {('<div>' + i['phone'] + '</div>') if i.get('phone') else ''}
        {('<div>' + i['email'] + '</div>') if i.get('email') else ''}
      </div>
    </div>
    <table>
      <thead><tr><th>Service</th><th>Date</th><th>Time</th><th>Amount</th></tr></thead>
      <tr><td>{i['service']}</td><td>{i['date']}</td><td>{i['time']}</td><td>${i['amount']:.2f}</td></tr>
      <tr class="total"><td colspan="3">Total</td><td>${i['amount']:.2f}</td></tr>
    </table>
    <div class="status {status_cls}">{i['status'].upper()}</div>
    {pay_btn}
    <div class="footer">
      <div class="biz">the Sol Standard &middot; The Nothing Box</div>
      <div class="contact">
        <a href="tel:{i['bp']}">{i['bp']}</a> &middot;
        <a href="mailto:hello@thesolstandard.com">hello@thesolstandard.com</a><br>
        <a href="https://thesolstandard.com" target="_blank">thesolstandard.com</a>
      </div>
      <div class="powered">Powered by <a href="#">Nudge</a> &mdash; the Fair Exchange</div>
    </div>
    </body></html>"""


# ═══════════════════════════════════════════════════
# ADMIN API
# ═══════════════════════════════════════════════════
@app.route('/api/admin/slots/<date>')
@require_admin
def a_slots(date):
    db = get_db(); ensure_slots(db, g.biz_id, date)
    return jsonify([dict(r) for r in db.execute("""
        SELECT s.*,c.name as client_name,c.phone as client_phone,c.token as client_token
        FROM slots s LEFT JOIN clients c ON s.client_id=c.id
        WHERE s.biz_id=? AND s.date=? ORDER BY s.id""", (g.biz_id, date)).fetchall()])

@app.route('/api/admin/book', methods=['POST'])
@require_admin
def a_book():
    d = request.json; name = d.get('name','').strip(); phone = d.get('phone','').strip()
    email = d.get('email','').strip(); slot_id = d.get('slot_id')
    service = d.get('service','Nothing Box Session'); price = d.get('price', g.biz['session_price'])
    expire_hours = d.get('expire_hours', 0)
    send_invoice = d.get('send_invoice', False)
    if not name: return jsonify({'error':'Name required'}), 400
    db = get_db()
    slot = db.execute("SELECT * FROM slots WHERE id=? AND biz_id=?", (slot_id, g.biz_id)).fetchone()
    if not slot or slot['client_id']: return jsonify({'error':'Unavailable'}), 400
    client = db.execute("SELECT * FROM clients WHERE biz_id=? AND name=? COLLATE NOCASE", (g.biz_id, name)).fetchone()
    if client:
        cid = client['id']; token = client['token']
        if phone: db.execute("UPDATE clients SET phone=? WHERE id=?", (phone, cid))
        if email: db.execute("UPDATE clients SET email=? WHERE id=?", (email, cid))
    else:
        token = gen_token()
        db.execute("INSERT INTO clients (biz_id,name,phone,email,token) VALUES (?,?,?,?,?)", (g.biz_id, name, phone, email, token))
        cid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    # Set status based on expiration
    status = 'booked'
    if expire_hours and expire_hours > 0 and price > 0:
        status = 'held'
    db.execute("UPDATE slots SET client_id=?,service=?,price=?,status=? WHERE id=?", (cid, service, price, slot_id, status))
    inv_token = gen_token()
    inv_status = 'paid' if price == 0 else 'unpaid'
    db.execute("INSERT INTO invoices (biz_id,client_id,slot_id,amount,invoice_token,status) VALUES (?,?,?,?,?,?)",
        (g.biz_id, cid, slot_id, price, inv_token, inv_status))
    db.commit()
    link = f"{BASE_URL}/me/{token}"
    inv_link = f"{BASE_URL}/invoice/{inv_token}"
    # Send confirmation SMS
    if phone:
        msg = f"Hi {name.split()[0]}! Confirmed: {slot['time']} on {slot['date']} at {g.biz['name']}."
        if expire_hours and expire_hours > 0 and price > 0:
            msg += f" Please pay within {expire_hours} hours to keep your slot."
        msg += f" View your appointments: {link}"
        send_sms(phone, msg, biz_id=g.biz_id)
    # Send invoice if requested
    if send_invoice and price > 0 and phone:
        send_sms(phone, f"Invoice for your session: {inv_link}", biz_id=g.biz_id)
    if send_invoice and price > 0:
        db.execute("INSERT INTO messages (biz_id,client_id,sender,body) VALUES (?,?,'admin',?)",
            (g.biz_id, cid, f"Here's your invoice: {inv_link}"))
        db.commit()
    return jsonify({'status':'booked','client_id':cid,'token':token,'portal_link':link,'invoice_link':inv_link})

@app.route('/api/admin/cancel/<int:sid>', methods=['POST'])
@require_admin
def a_cancel(sid):
    db = get_db()
    s = db.execute("SELECT s.*,c.name,c.phone FROM slots s LEFT JOIN clients c ON s.client_id=c.id WHERE s.id=? AND s.biz_id=?", (sid, g.biz_id)).fetchone()
    if not s: return jsonify({'error':'Not found'}), 404
    if s['phone']: send_sms(s['phone'], f"Hi {s['name'].split()[0]}, your {s['time']} appointment at {g.biz['name']} has been cancelled.", biz_id=g.biz_id)
    db.execute("UPDATE slots SET client_id=NULL,service=NULL,price=0,status='open' WHERE id=?", (sid,))
    db.execute("UPDATE invoices SET status='cancelled' WHERE slot_id=?", (sid,)); db.commit()
    return jsonify({'status':'cancelled'})

@app.route('/api/admin/nudge', methods=['POST'])
@require_admin
def a_nudge():
    d = request.json; db = get_db(); biz = g.biz
    fs = db.execute("SELECT s.*,c.name as cn,c.phone as cp,c.id as cid FROM slots s JOIN clients c ON s.client_id=c.id WHERE s.id=?", (d['from_slot_id'],)).fetchone()
    ts = db.execute("SELECT s.*,c.name as cn,c.phone as cp,c.id as cid FROM slots s JOIN clients c ON s.client_id=c.id WHERE s.id=?", (d['to_slot_id'],)).fetchone()
    if not fs or not ts: return jsonify({'error':'Invalid'}), 400
    mx = biz.get('max_offers', 2)
    att = get_attempts(db, g.biz_id, fs['cid'], d['to_slot_id'])
    if att >= mx: return jsonify({'error':f'Max {mx} offers reached'}), 400
    attempt = inc_attempts(db, g.biz_id, fs['cid'], d['to_slot_id'])
    exp = (datetime.now() + timedelta(minutes=biz.get('offer_timer_min', 30))).isoformat()
    fee = d.get('fee', 0)
    db.execute("INSERT INTO nudges (biz_id,from_slot_id,to_slot_id,from_client_id,to_client_id,attempt,pct,fee,expires_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (g.biz_id, d['from_slot_id'], d['to_slot_id'], fs['cid'], ts['cid'], attempt, d.get('pct',0), fee, exp))
    nid = db.execute("SELECT last_insert_rowid()").fetchone()[0]; db.commit()
    fin = " This is their final offer." if attempt >= mx else ""
    if ts['cp']: send_sms(ts['cp'], f"Hi {ts['cn'].split()[0]}! {fs['cn']} wants your {ts['time']} slot at {biz['name']}. Offer: ${fee:.2f} to swap to {fs['time']}.{fin} You have {biz.get('offer_timer_min',30)} min. Reply YES or NO.", biz_id=g.biz_id, nudge_id=nid)
    if fs['cp']: send_sms(fs['cp'], f"Nudge sent! Offer {attempt}/{mx} to {ts['cn'].split()[0]} for {ts['time']}. ${fee:.2f}. Timer: {biz.get('offer_timer_min',30)} min.", biz_id=g.biz_id, nudge_id=nid)
    return jsonify({'status':'sent','nudge_id':nid,'attempt':attempt})

@app.route('/api/admin/nudge/<int:nid>/resolve', methods=['POST'])
@require_admin
def a_resolve(nid):
    db = get_db()
    n = db.execute("SELECT * FROM nudges WHERE id=? AND biz_id=?", (nid, g.biz_id)).fetchone()
    if not n or n['status'] != 'pending': return jsonify({'error':'Not found'}), 400
    return _resolve(db, n, request.json.get('status'))

@app.route('/api/admin/nudges')
@require_admin
def a_nudges():
    db = get_db()
    return jsonify([dict(r) for r in db.execute("""
        SELECT n.*,fc.name as from_name,tc.name as to_name,fs.time as from_time,ts.time as to_time
        FROM nudges n JOIN clients fc ON n.from_client_id=fc.id JOIN clients tc ON n.to_client_id=tc.id
        JOIN slots fs ON n.from_slot_id=fs.id JOIN slots ts ON n.to_slot_id=ts.id
        WHERE n.biz_id=? ORDER BY n.created_at DESC LIMIT 20""", (g.biz_id,)).fetchall()])

@app.route('/api/admin/sms')
@require_admin
def a_sms():
    return jsonify([dict(r) for r in get_db().execute("SELECT * FROM sms_log WHERE biz_id=? ORDER BY created_at DESC LIMIT 20", (g.biz_id,)).fetchall()])

@app.route('/api/admin/stats')
@require_admin
def a_stats():
    db = get_db(); today = datetime.now().strftime('%Y-%m-%d')
    return jsonify({
        'booked': db.execute("SELECT COUNT(*) FROM slots WHERE biz_id=? AND date=? AND client_id IS NOT NULL", (g.biz_id, today)).fetchone()[0],
        'open': db.execute("SELECT COUNT(*) FROM slots WHERE biz_id=? AND date=? AND client_id IS NULL", (g.biz_id, today)).fetchone()[0],
        'revenue': db.execute("SELECT COALESCE(SUM(price),0) FROM slots WHERE biz_id=? AND date=? AND client_id IS NOT NULL", (g.biz_id, today)).fetchone()[0],
        'nudges': db.execute("SELECT COUNT(*) FROM nudges WHERE biz_id=?", (g.biz_id,)).fetchone()[0],
        'swaps': db.execute("SELECT COUNT(*) FROM nudges WHERE biz_id=? AND status='accepted'", (g.biz_id,)).fetchone()[0],
        'fees': db.execute("SELECT COALESCE(SUM(fee),0) FROM nudges WHERE biz_id=? AND status='accepted'", (g.biz_id,)).fetchone()[0]})

@app.route('/api/admin/settings', methods=['GET','POST'])
@require_admin
def a_settings():
    db = get_db()
    if request.method == 'POST':
        d = request.json
        db.execute("UPDATE businesses SET name=?,phone=?,session_price=?,session_duration=?,default_nudge_pct=?,offer_timer_min=?,max_offers=?,platform_fee_pct=? WHERE id=?",
            (d.get('name'),d.get('phone'),d.get('price'),d.get('duration'),d.get('default_pct'),d.get('timer'),d.get('max_offers',2),d.get('platform_fee_pct',20),g.biz_id))
        db.commit(); return jsonify({'status':'saved'})
    return jsonify(g.biz)

@app.route('/api/admin/report', methods=['POST'])
@require_admin
def a_report():
    d = request.json; db = get_db(); rt = gen_token()
    db.execute("INSERT INTO reports (biz_id,client_id,slot_id,report_html,report_token) VALUES (?,?,?,?,?)",
        (g.biz_id, d.get('client_id'), d.get('slot_id'), d.get('report_html',''), rt))
    db.commit()
    client = db.execute("SELECT * FROM clients WHERE id=?", (d.get('client_id'),)).fetchone()
    if client and client['phone']:
        send_sms(client['phone'], f"Hi {client['name'].split()[0]}! Your session report is ready: {BASE_URL}/report/{rt}", biz_id=g.biz_id)
    return jsonify({'link': f"{BASE_URL}/report/{rt}"})


# ═══════════════════════════════════════════════════
# ADMIN API — Block/Unblock slots
# ═══════════════════════════════════════════════════
@app.route('/api/admin/block/<int:sid>', methods=['POST'])
@require_admin
def a_block(sid):
    db = get_db()
    reason = (request.json or {}).get('reason', 'Blocked')
    db.execute("UPDATE slots SET status='blocked', service=? WHERE id=? AND biz_id=? AND client_id IS NULL", (reason, sid, g.biz_id))
    db.commit()
    return jsonify({'status': 'blocked'})

@app.route('/api/admin/unblock/<int:sid>', methods=['POST'])
@require_admin
def a_unblock(sid):
    db = get_db()
    db.execute("UPDATE slots SET status='open', service=NULL WHERE id=? AND biz_id=? AND status='blocked'", (sid, g.biz_id))
    db.commit()
    return jsonify({'status': 'unblocked'})


# ═══════════════════════════════════════════════════
# ADMIN API — Client list & detail
# ═══════════════════════════════════════════════════
@app.route('/api/admin/clients')
@require_admin
def a_clients():
    db = get_db()
    rows = db.execute("""
        SELECT c.*, COUNT(s.id) as visit_count,
            COALESCE(SUM(s.price),0) as total_spent,
            MAX(s.date) as last_visit
        FROM clients c
        LEFT JOIN slots s ON s.client_id=c.id AND s.status='booked'
        WHERE c.biz_id=? AND COALESCE(c.status,'active')!='deleted'
        GROUP BY c.id ORDER BY c.name
    """, (g.biz_id,)).fetchall()
    return jsonify([dict(r) for r in rows])

@app.route('/api/admin/clients/add', methods=['POST'])
@require_admin
def a_add_client():
    d = request.json; db = get_db()
    first = (d.get('first','') or '').strip()
    last = (d.get('last','') or '').strip()
    if not first or not last: return jsonify({'error': 'First and last name required'}), 400
    name = first + ' ' + last
    phone = (d.get('phone','') or '').strip()
    email = (d.get('email','') or '').strip()
    # Check if client already exists
    existing = db.execute("SELECT * FROM clients WHERE biz_id=? AND name=? COLLATE NOCASE", (g.biz_id, name)).fetchone()
    if existing: return jsonify({'error': f'{name} already exists'}), 400
    token = gen_token()
    db.execute("""INSERT INTO clients (biz_id,name,phone,email,token,dob,gender,address,emergency_contact,emergency_phone,medical_notes,notes)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (g.biz_id, name, phone, email, token,
         (d.get('dob','') or '').strip() or None,
         (d.get('gender','') or '').strip() or None,
         (d.get('address','') or '').strip() or None,
         (d.get('emergency_contact','') or '').strip() or None,
         (d.get('emergency_phone','') or '').strip() or None,
         (d.get('medical_notes','') or '').strip() or None,
         d.get('notes','').strip() or None))
    cid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    db.commit()
    print(f"\n  *** CLIENT ADDED: {name} (#{cid}) ***\n")
    return jsonify({'status': 'created', 'client_id': cid, 'token': token})

@app.route('/api/admin/clients/<int:cid>/demographics', methods=['POST'])
@require_admin
def a_client_demographics(cid):
    d = request.json; db = get_db()
    client = db.execute("SELECT * FROM clients WHERE id=? AND biz_id=?", (cid, g.biz_id)).fetchone()
    if not client: return jsonify({'error': 'Not found'}), 404
    db.execute("""UPDATE clients SET dob=?,gender=?,address=?,emergency_contact=?,emergency_phone=?,medical_notes=? WHERE id=?""",
        ((d.get('dob','') or '').strip() or None,
         (d.get('gender','') or '').strip() or None,
         (d.get('address','') or '').strip() or None,
         (d.get('emergency_contact','') or '').strip() or None,
         (d.get('emergency_phone','') or '').strip() or None,
         (d.get('medical_notes','') or '').strip() or None, cid))
    db.commit()
    return jsonify({'status': 'saved'})

@app.route('/api/admin/clients/<int:cid>')
@require_admin
def a_client_detail(cid):
    db = get_db()
    client = db.execute("SELECT * FROM clients WHERE id=? AND biz_id=?", (cid, g.biz_id)).fetchone()
    if not client: return jsonify({'error': 'Not found'}), 404
    visits = db.execute("""
        SELECT s.date, s.time, s.service, s.price, r.report_token, i.invoice_token, i.status as inv_status
        FROM slots s LEFT JOIN reports r ON r.slot_id=s.id AND r.client_id=?
        LEFT JOIN invoices i ON i.slot_id=s.id AND i.client_id=?
        WHERE s.client_id=? ORDER BY s.date DESC
    """, (cid, cid, cid)).fetchall()
    reviews = db.execute("SELECT rv.*, s.date, s.time FROM reviews rv JOIN slots s ON rv.slot_id=s.id WHERE rv.client_id=? ORDER BY rv.created_at DESC", (cid,)).fetchall()
    return jsonify({'client': dict(client), 'visits': [dict(v) for v in visits], 'reviews': [dict(r) for r in reviews]})

@app.route('/api/admin/clients/<int:cid>/notes', methods=['POST'])
@require_admin
def a_client_notes(cid):
    db = get_db()
    notes = (request.json or {}).get('notes', '')
    db.execute("UPDATE clients SET notes=? WHERE id=? AND biz_id=?", (notes, cid, g.biz_id))
    db.commit()
    return jsonify({'status': 'saved'})

@app.route('/api/admin/clients/<int:cid>/send-info', methods=['POST'])
@require_admin
def a_send_info(cid):
    db = get_db()
    client = db.execute("SELECT * FROM clients WHERE id=? AND biz_id=?", (cid, g.biz_id)).fetchone()
    if not client: return jsonify({'error': 'Not found'}), 404
    link = f"{BASE_URL}/welcome/{client['token']}"
    # Send via SMS
    if client['phone']:
        send_sms(client['phone'], f"Hi {client['name'].split()[0]}! Here's everything you need to know before your session at {g.biz['name']}: {link}", biz_id=g.biz_id)
    # Send via in-app message
    db.execute("INSERT INTO messages (biz_id,client_id,sender,body) VALUES (?,?,'admin',?)",
        (g.biz_id, cid, f"Here's everything you need to know before your session. Please read through and give your consent to proceed: {link}"))
    # Update notes
    notes = client['notes'] or ''
    if 'Info package sent' not in notes:
        if notes: notes += ' | '
        notes += f'Info package sent {datetime.now().strftime("%Y-%m-%d")}'
        db.execute("UPDATE clients SET notes=? WHERE id=?", (notes, cid))
    db.commit()
    return jsonify({'status': 'sent', 'link': link})

@app.route('/api/admin/clients/<int:cid>/send-welcome', methods=['POST'])
@require_admin
def a_send_welcome(cid):
    db = get_db()
    client = db.execute("SELECT * FROM clients WHERE id=? AND biz_id=?", (cid, g.biz_id)).fetchone()
    if not client: return jsonify({'error': 'Not found'}), 404
    link = f"{BASE_URL}/onboard/{client['token']}"
    # Send via SMS
    if client['phone']:
        send_sms(client['phone'], f"Hi {client['name'].split()[0]}! Your welcome packet is ready. Review the session details and book your appointment: {link}", biz_id=g.biz_id)
    # Send via in-app message
    db.execute("INSERT INTO messages (biz_id,client_id,sender,body) VALUES (?,?,'admin',?)",
        (g.biz_id, cid, f"Your welcome packet is ready! Review the details and pick a time for your session: {link}"))
    # Update notes
    notes = client['notes'] or ''
    if 'Welcome sent' not in notes:
        if notes: notes += ' | '
        notes += f'Welcome sent {datetime.now().strftime("%Y-%m-%d")}'
        db.execute("UPDATE clients SET notes=? WHERE id=?", (notes, cid))
    db.commit()
    return jsonify({'status': 'sent', 'link': link})

@app.route('/api/admin/clients/<int:cid>/delete', methods=['POST'])
@require_admin
def a_delete_client(cid):
    db = get_db()
    reason = (request.json or {}).get('reason', '').strip()
    client = db.execute("SELECT * FROM clients WHERE id=? AND biz_id=?", (cid, g.biz_id)).fetchone()
    if not client: return jsonify({'error': 'Not found'}), 404
    db.execute("UPDATE clients SET status='deleted',deleted_at=CURRENT_TIMESTAMP,delete_reason=? WHERE id=?", (reason or None, cid))
    # Cancel any upcoming appointments
    db.execute("UPDATE slots SET client_id=NULL,status='open',service=NULL,price=0 WHERE client_id=? AND date>=?",
        (cid, datetime.now().strftime('%Y-%m-%d')))
    db.commit()
    return jsonify({'status': 'deleted'})

@app.route('/api/admin/clients/<int:cid>/restore', methods=['POST'])
@require_admin
def a_restore_client(cid):
    db = get_db()
    db.execute("UPDATE clients SET status='active',deleted_at=NULL,delete_reason=NULL WHERE id=? AND biz_id=?", (cid, g.biz_id))
    db.commit()
    return jsonify({'status': 'restored'})

@app.route('/api/admin/clients/deleted')
@require_admin
def a_deleted_clients():
    db = get_db()
    rows = db.execute("""
        SELECT c.*, COUNT(s.id) as visit_count, COALESCE(SUM(s.price),0) as total_spent
        FROM clients c LEFT JOIN slots s ON s.client_id=c.id AND s.status='booked'
        WHERE c.biz_id=? AND c.status='deleted'
        GROUP BY c.id ORDER BY c.deleted_at DESC
    """, (g.biz_id,)).fetchall()
    return jsonify([dict(r) for r in rows])


# ═══════════════════════════════════════════════════
# ADMIN API — Revenue dashboard
# ═══════════════════════════════════════════════════
@app.route('/api/admin/revenue')
@require_admin
def a_revenue():
    db = get_db()
    today = datetime.now().strftime('%Y-%m-%d')
    week_ago = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d')
    month_start = datetime.now().strftime('%Y-%m-01')

    def rev(where, params):
        return db.execute(f"SELECT COALESCE(SUM(price),0) as r, COUNT(*) as c FROM slots WHERE biz_id=? AND client_id IS NOT NULL AND {where}", params).fetchone()

    t = rev("date=?", (g.biz_id, today))
    w = rev("date>=?", (g.biz_id, week_ago))
    m = rev("date>=?", (g.biz_id, month_start))
    a = rev("1=1", (g.biz_id,))
    swap_fees = db.execute("SELECT COALESCE(SUM(fee),0) FROM nudges WHERE biz_id=? AND status='accepted'", (g.biz_id,)).fetchone()[0]
    platform_pct = (biz['platform_fee_pct'] if biz.get('platform_fee_pct') else 20) / 100 if (biz := db.execute("SELECT * FROM businesses WHERE id=?", (g.biz_id,)).fetchone()) else 0.2
    platform_earned = round(swap_fees * platform_pct, 2)
    credits_outstanding = db.execute("SELECT COALESCE(SUM(amount),0) FROM credits WHERE biz_id=? AND used_invoice_id IS NULL", (g.biz_id,)).fetchone()[0]

    return jsonify({
        'today': {'revenue': t['r'], 'sessions': t['c']},
        'week': {'revenue': w['r'], 'sessions': w['c']},
        'month': {'revenue': m['r'], 'sessions': m['c']},
        'all_time': {'revenue': a['r'], 'sessions': a['c']},
        'swap_fees': swap_fees,
        'platform_earned': platform_earned,
        'credits_outstanding': round(credits_outstanding, 2)
    })


# ═══════════════════════════════════════════════════
# ADMIN API — Today's agenda
# ═══════════════════════════════════════════════════
@app.route('/api/admin/agenda')
@require_admin
def a_agenda():
    db = get_db()
    today = datetime.now().strftime('%Y-%m-%d')
    ensure_slots(db, g.biz_id, today)
    rows = db.execute("""
        SELECT s.*, c.name as client_name, c.phone as client_phone, c.notes as client_notes
        FROM slots s LEFT JOIN clients c ON s.client_id=c.id
        WHERE s.biz_id=? AND s.date=? AND s.client_id IS NOT NULL
        ORDER BY s.id
    """, (g.biz_id, today)).fetchall()
    return jsonify([dict(r) for r in rows])


# ═══════════════════════════════════════════════════
# ADMIN API — Export CSV
# ═══════════════════════════════════════════════════
@app.route('/api/admin/export/appointments')
@require_admin
def a_export_apts():
    db = get_db()
    rows = db.execute("""
        SELECT s.date, s.time, c.name, c.phone, c.email, s.service, s.price, s.status
        FROM slots s LEFT JOIN clients c ON s.client_id=c.id
        WHERE s.biz_id=? AND s.client_id IS NOT NULL ORDER BY s.date, s.id
    """, (g.biz_id,)).fetchall()
    csv = "Date,Time,Client,Phone,Email,Service,Price,Status\n"
    for r in rows:
        csv += f"{r['date']},{r['time']},{r['name']},{r['phone']},{r['email']},{r['service']},{r['price']},{r['status']}\n"
    return csv, 200, {'Content-Type': 'text/csv', 'Content-Disposition': 'attachment; filename=nudge_appointments.csv'}

@app.route('/api/admin/export/clients')
@require_admin
def a_export_clients():
    db = get_db()
    rows = db.execute("""
        SELECT c.name, c.phone, c.email, c.notes, COUNT(s.id) as visits, COALESCE(SUM(s.price),0) as spent
        FROM clients c LEFT JOIN slots s ON s.client_id=c.id AND s.status='booked'
        WHERE c.biz_id=? GROUP BY c.id ORDER BY c.name
    """, (g.biz_id,)).fetchall()
    csv = "Name,Phone,Email,Notes,Visits,Total Spent\n"
    for r in rows:
        csv += f"{r['name']},{r['phone']},{r['email']},{r['notes'] or ''},{r['visits']},{r['spent']}\n"
    return csv, 200, {'Content-Type': 'text/csv', 'Content-Disposition': 'attachment; filename=nudge_clients.csv'}


# ═══════════════════════════════════════════════════
# ADMIN API — Seed demo data
# ═══════════════════════════════════════════════════
@app.route('/api/admin/seed', methods=['POST'])
@require_admin
def a_seed():
    db = get_db()
    if db.execute("SELECT COUNT(*) FROM clients WHERE biz_id=?", (g.biz_id,)).fetchone()[0] > 0:
        return jsonify({'status': 'already seeded'})
    today = datetime.now().strftime('%Y-%m-%d')
    tomorrow = (datetime.now() + timedelta(days=1)).strftime('%Y-%m-%d')
    day3 = (datetime.now() + timedelta(days=3)).strftime('%Y-%m-%d')
    ensure_slots(db, g.biz_id, today)
    ensure_slots(db, g.biz_id, tomorrow)
    ensure_slots(db, g.biz_id, day3)
    demos = [
        ('Maya Johnson','(301) 555-0142','maya@email.com'),
        ('James Carter','(202) 555-0198','james@email.com'),
        ('Diane Williams','(240) 555-0267','diane@email.com'),
        ('Andre Thompson','(301) 555-0331','andre@email.com'),
        ('Lisa Park','(202) 555-0419','lisa@email.com'),
        ('Marcus Rivera','(240) 555-0588','marcus@email.com'),
    ]
    cids = []
    for name, phone, email in demos:
        tk = gen_token()
        db.execute("INSERT INTO clients (biz_id,name,phone,email,token) VALUES (?,?,?,?,?)", (g.biz_id, name, phone, email, tk))
        cids.append(db.execute("SELECT last_insert_rowid()").fetchone()[0])
    # Book some slots
    slots_today = db.execute("SELECT id FROM slots WHERE biz_id=? AND date=? AND client_id IS NULL ORDER BY id", (g.biz_id, today)).fetchall()
    slots_tomorrow = db.execute("SELECT id FROM slots WHERE biz_id=? AND date=? AND client_id IS NULL ORDER BY id", (g.biz_id, tomorrow)).fetchall()
    slots_day3 = db.execute("SELECT id FROM slots WHERE biz_id=? AND date=? AND client_id IS NULL ORDER BY id", (g.biz_id, day3)).fetchall()
    bookings = [(slots_today,0),(slots_today,2),(slots_today,4),(slots_tomorrow,0),(slots_tomorrow,1),(slots_day3,0)]
    for i, (sl, idx) in enumerate(bookings):
        if idx < len(sl) and i < len(cids):
            db.execute("UPDATE slots SET client_id=?,service='Nothing Box Session',price=75,status='booked' WHERE id=?", (cids[i], sl[idx]['id']))
            db.execute("INSERT INTO invoices (biz_id,client_id,slot_id,amount,invoice_token) VALUES (?,?,?,75,?)", (g.biz_id, cids[i], sl[idx]['id'], gen_token()))
    db.commit()
    return jsonify({'status': 'seeded', 'clients': len(cids)})


# ═══════════════════════════════════════════════════
# CLIENT API
# ═══════════════════════════════════════════════════
@app.route('/api/client/<token>/profile')
@require_client
def c_profile(token):
    db = get_db()
    today = datetime.now().strftime('%Y-%m-%d')
    visits = db.execute("SELECT COUNT(*) FROM slots WHERE client_id=? AND status='booked' AND date<?", (g.client_id, today)).fetchone()[0]
    # Session options based on visit count
    session_opts = [{'duration': 45, 'label': 'Standard Session — 45 min', 'price': 75}]
    if visits >= 3:
        session_opts.append({'duration': 60, 'label': 'Extended Session — 60 min', 'price': 95})
        session_opts.append({'duration': 90, 'label': 'Deep Session — 90 min', 'price': 130})
    return jsonify({
        'name': g.client['name'], 'phone': g.client['phone'], 'email': g.client['email'],
        'business': g.biz['name'], 'biz_phone': g.biz['phone'],
        'visits': visits, 'session_options': session_opts,
        'credit_balance': get_credit_balance(db, g.biz_id, g.client_id)
    })

@app.route('/api/client/<token>/consent', methods=['POST'])
@require_client
def c_consent(token):
    db = get_db()
    d = request.json
    ts = d.get('timestamp', datetime.now().isoformat())
    notes = g.client.get('notes') or ''
    if 'Consent signed' not in notes:
        if notes: notes += ' | '
        notes += f'Consent signed {ts[:10]}'
        db.execute("UPDATE clients SET notes=? WHERE id=?", (notes, g.client_id))
        db.commit()
    print(f"\n  *** CONSENT SIGNED: {g.client['name']} at {ts} ***\n")
    return jsonify({'status': 'consented'})

@app.route('/api/client/<token>/appointments')
@require_client
def c_apts(token):
    db = get_db()
    return jsonify([dict(r) for r in db.execute("""
        SELECT s.id,s.date,s.time,s.service,s.price,s.status,
            r.report_token,i.invoice_token,i.amount as inv_amount,i.status as inv_status,
            rv.id as review_id,rv.rating as review_rating,rv.text as review_text
        FROM slots s LEFT JOIN reports r ON r.slot_id=s.id AND r.client_id=?
        LEFT JOIN invoices i ON i.slot_id=s.id AND i.client_id=?
        LEFT JOIN reviews rv ON rv.slot_id=s.id AND rv.client_id=?
        WHERE s.client_id=? AND s.biz_id=? ORDER BY s.date DESC""",
        (g.client_id, g.client_id, g.client_id, g.client_id, g.biz_id)).fetchall()])

@app.route('/api/client/<token>/available/<date>')
@require_client
def c_avail(token, date):
    db = get_db(); ensure_slots(db, g.biz_id, date)
    return jsonify([{**dict(r),
        'is_yours': r['client_id'] == g.client_id if r['client_id'] else False,
        'label': 'Your appointment' if r['client_id'] == g.client_id else ('Booked' if r['client_id'] else 'Available')
    } for r in db.execute("SELECT * FROM slots WHERE biz_id=? AND date=? ORDER BY id", (g.biz_id, date)).fetchall()])

@app.route('/api/client/<token>/nudge', methods=['POST'])
@require_client
def c_nudge(token):
    d = request.json; db = get_db(); biz = g.biz
    to_slot_id = d.get('to_slot_id')
    from_slot_id = d.get('from_slot_id')  # Optional - if they have an existing appointment
    fee = d.get('fee', 0)

    ts = db.execute("SELECT s.*,c.name as cn,c.phone as cp,c.id as cid FROM slots s JOIN clients c ON s.client_id=c.id WHERE s.id=? AND s.biz_id=?",
        (to_slot_id, g.biz_id)).fetchone()
    if not ts: return jsonify({'error':'Slot not available for nudge'}), 400

    # If they provided a from_slot, verify they own it
    if from_slot_id:
        fs = db.execute("SELECT * FROM slots WHERE id=? AND client_id=?", (from_slot_id, g.client_id)).fetchone()
        if not fs: return jsonify({'error':'Not your slot'}), 400

    mx = biz.get('max_offers', 2)
    att = get_attempts(db, g.biz_id, g.client_id, to_slot_id)
    if att >= mx: return jsonify({'error':f'Max {mx} offers reached'}), 400
    attempt = inc_attempts(db, g.biz_id, g.client_id, to_slot_id)
    exp = (datetime.now() + timedelta(minutes=biz.get('offer_timer_min', 30))).isoformat()

    # from_slot_id can be NULL for buy-only nudges
    db.execute("INSERT INTO nudges (biz_id,from_slot_id,to_slot_id,from_client_id,to_client_id,attempt,fee,expires_at) VALUES (?,?,?,?,?,?,?,?)",
        (g.biz_id, from_slot_id, to_slot_id, g.client_id, ts['cid'], attempt, fee, exp))
    nid = db.execute("SELECT last_insert_rowid()").fetchone()[0]; db.commit()
    fin = " This is their final offer." if attempt >= mx else ""
    if ts['cp']: send_sms(ts['cp'], f"Hi {ts['cn'].split()[0]}! Someone wants your {ts['time']} slot at {biz['name']}. Offer: ${fee:.2f}.{fin} You have {biz.get('offer_timer_min',30)} min. Reply YES or NO.", biz_id=g.biz_id, nudge_id=nid)
    return jsonify({'status':'sent','nudge_id':nid,'attempt':attempt,'max':mx})

@app.route('/api/client/<token>/book', methods=['POST'])
@require_client
def c_book(token):
    d = request.json; db = get_db(); biz = g.biz
    slot_id = d.get('slot_id')
    session_type = d.get('session_type', 45)  # duration in minutes
    slot = db.execute("SELECT * FROM slots WHERE id=? AND biz_id=? AND client_id IS NULL AND status NOT IN ('blocked','held')",
        (slot_id, g.biz_id)).fetchone()
    if not slot: return jsonify({'error':'Slot not available'}), 400

    # Validate session type based on visit count
    today = datetime.now().strftime('%Y-%m-%d')
    visits = db.execute("SELECT COUNT(*) FROM slots WHERE client_id=? AND status='booked' AND date<?", (g.client_id, today)).fetchone()[0]
    pricing = {45: 75, 60: 95, 90: 130}
    if session_type in (60, 90) and visits < 3:
        return jsonify({'error': 'Extended sessions unlock after 3 completed sessions'}), 400
    price = pricing.get(session_type, 75)
    service = {45: 'Nothing Box — 45 min', 60: 'Nothing Box Extended — 60 min', 90: 'Nothing Box Deep — 90 min'}.get(session_type, 'Nothing Box Session')
    is_free = 'Try for Free' in (g.client.get('notes') or '')

    # Apply credits
    credit_bal = get_credit_balance(db, g.biz_id, g.client_id)
    credit_used = min(credit_bal, price) if not is_free else 0
    charge_amount = price - credit_used if not is_free else 0

    if USE_STRIPE and not is_free and charge_amount > 0:
        db.execute("UPDATE slots SET client_id=?,status='held',service=?,price=? WHERE id=?",
            (g.client_id, service, price, slot_id))
        db.commit()
        inv_token = gen_token()
        try:
            line_items = [{
                'price_data': {
                    'currency': 'usd',
                    'unit_amount': int(charge_amount * 100),
                    'product_data': {
                        'name': service,
                        'description': f"{slot['date']} at {slot['time']}" + (f" (${credit_used:.2f} credit applied)" if credit_used > 0 else ""),
                    },
                },
                'quantity': 1,
            }]
            session = stripe.checkout.Session.create(
                payment_method_types=['card'],
                line_items=line_items,
                mode='payment',
                customer_email=g.client.get('email') or None,
                metadata={'slot_id': str(slot_id), 'client_id': str(g.client_id), 'biz_id': str(g.biz_id), 'inv_token': inv_token, 'client_token': g.client.get('token',''), 'credit_used': str(credit_used)},
                success_url=BASE_URL + '/api/stripe/book-success?session_id={CHECKOUT_SESSION_ID}',
                cancel_url=BASE_URL + '/api/stripe/book-cancel?slot_id=' + str(slot_id) + '&token=' + g.client.get('token',''),
            )
            return jsonify({'status': 'checkout', 'checkout_url': session.url, 'credit_applied': credit_used})
        except Exception as e:
            db.execute("UPDATE slots SET client_id=NULL,status='open',service=NULL,price=0 WHERE id=?", (slot_id,))
            db.commit()
            return jsonify({'error': f'Payment setup failed: {e}'}), 500
    else:
        # Free, fully credit-covered, or no Stripe
        db.execute("UPDATE slots SET client_id=?,service=?,price=?,status='booked' WHERE id=?",
            (g.client_id, service, price if not is_free else 0, slot_id))
        inv_token = gen_token()
        db.execute("INSERT INTO invoices (biz_id,client_id,slot_id,amount,invoice_token,status) VALUES (?,?,?,?,?,?)",
            (g.biz_id, g.client_id, slot_id, charge_amount, inv_token, 'paid'))
        # Mark credits as used
        if credit_used > 0:
            remaining = credit_used
            inv_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
            unused = db.execute("SELECT id,amount FROM credits WHERE biz_id=? AND client_id=? AND used_invoice_id IS NULL ORDER BY created_at ASC",
                (g.biz_id, g.client_id)).fetchall()
            for cr in unused:
                if remaining <= 0: break
                use = min(cr['amount'], remaining)
                db.execute("UPDATE credits SET used_invoice_id=? WHERE id=?", (inv_id, cr['id']))
                remaining -= use
        db.commit()
        msg = f"Hi {g.client['name'].split()[0]}! Confirmed: {service} at {slot['time']} on {slot['date']}."
        if credit_used > 0: msg += f" ${credit_used:.2f} credit applied."
        if is_free: msg += " No charge — free session."
        if g.client.get('phone'):
            send_sms(g.client['phone'], msg, biz_id=g.biz_id)
        return jsonify({'status':'booked','slot_id':slot_id,'credit_applied':credit_used})

@app.route('/api/client/<token>/nudge/<int:nid>/respond', methods=['POST'])
@require_client
def c_respond(token, nid):
    db = get_db()
    n = db.execute("SELECT * FROM nudges WHERE id=? AND to_client_id=? AND status='pending'", (nid, g.client_id)).fetchone()
    if not n: return jsonify({'error':'Not found'}), 404
    return _resolve(db, n, request.json.get('status'))

@app.route('/api/client/<token>/nudges')
@require_client
def c_nudges(token):
    db = get_db()
    inc = db.execute("""SELECT n.*,fc.name as from_name,ts.time as to_time,
            CASE WHEN n.from_slot_id IS NOT NULL THEN fs.time ELSE NULL END as from_time
        FROM nudges n JOIN clients fc ON n.from_client_id=fc.id
        LEFT JOIN slots fs ON n.from_slot_id=fs.id
        JOIN slots ts ON n.to_slot_id=ts.id
        WHERE n.to_client_id=? AND n.status='pending' ORDER BY n.created_at DESC""", (g.client_id,)).fetchall()
    out = db.execute("""SELECT n.*,tc.name as to_name,ts.time as to_time,
            CASE WHEN n.from_slot_id IS NOT NULL THEN fs.time ELSE NULL END as from_time
        FROM nudges n JOIN clients tc ON n.to_client_id=tc.id
        LEFT JOIN slots fs ON n.from_slot_id=fs.id
        JOIN slots ts ON n.to_slot_id=ts.id
        WHERE n.from_client_id=? ORDER BY n.created_at DESC LIMIT 10""", (g.client_id,)).fetchall()
    return jsonify({'incoming':[dict(r) for r in inc],'outgoing':[dict(r) for r in out]})

@app.route('/api/client/<token>/review', methods=['POST'])
@require_client
def c_review(token):
    d = request.json; db = get_db()
    slot_id = d.get('slot_id')
    rating = d.get('rating', 5)
    text = d.get('text', '').strip()
    if not text: return jsonify({'error': 'Review text required'}), 400
    # Verify client owns this slot
    slot = db.execute("SELECT * FROM slots WHERE id=? AND client_id=?", (slot_id, g.client_id)).fetchone()
    if not slot: return jsonify({'error': 'Invalid appointment'}), 400
    # Check if already reviewed
    existing = db.execute("SELECT id FROM reviews WHERE client_id=? AND slot_id=?", (g.client_id, slot_id)).fetchone()
    if existing: return jsonify({'error': 'Already reviewed'}), 400
    db.execute("INSERT INTO reviews (biz_id,client_id,slot_id,rating,text) VALUES (?,?,?,?,?)",
        (g.biz_id, g.client_id, slot_id, rating, text))
    db.commit()
    return jsonify({'status': 'submitted'})

@app.route('/api/admin/reviews')
@require_admin
def a_reviews():
    db = get_db()
    rows = db.execute("""
        SELECT rv.*, c.name as client_name, s.date, s.time
        FROM reviews rv JOIN clients c ON rv.client_id=c.id
        JOIN slots s ON rv.slot_id=s.id
        WHERE rv.biz_id=? ORDER BY rv.created_at DESC
    """, (g.biz_id,)).fetchall()
    return jsonify([dict(r) for r in rows])


# ═══════════════════════════════════════════════════
# SHARED RESOLVE
# ═══════════════════════════════════════════════════
def _resolve(db, n, status):
    if status not in ('accepted','declined'): return jsonify({'error':'Invalid'}), 400
    biz = db.execute("SELECT * FROM businesses WHERE id=?", (n['biz_id'],)).fetchone()
    db.execute("UPDATE nudges SET status=?,resolved_at=CURRENT_TIMESTAMP WHERE id=?", (status, n['id']))
    fc = db.execute("SELECT * FROM clients WHERE id=?", (n['from_client_id'],)).fetchone()
    tc = db.execute("SELECT * FROM clients WHERE id=?", (n['to_client_id'],)).fetchone()
    if status == 'accepted':
        # Split fee: platform takes a cut, seller gets the rest as credit
        fee = n['fee'] or 0
        platform_pct = (biz['platform_fee_pct'] if biz and biz['platform_fee_pct'] else 20) / 100
        platform_cut = round(fee * platform_pct, 2)
        seller_credit = round(fee - platform_cut, 2)

        ts = db.execute("SELECT * FROM slots WHERE id=?", (n['to_slot_id'],)).fetchone()
        if n['from_slot_id']:
            # SWAP mode
            fs = db.execute("SELECT * FROM slots WHERE id=?", (n['from_slot_id'],)).fetchone()
            if fs and ts:
                db.execute("UPDATE slots SET client_id=?,service=?,price=? WHERE id=?", (ts['client_id'],ts['service'],ts['price'],fs['id']))
                db.execute("UPDATE slots SET client_id=?,service=?,price=? WHERE id=?", (fs['client_id'],fs['service'],fs['price'],ts['id']))
            # Credit the seller
            if seller_credit > 0:
                db.execute("INSERT INTO credits (biz_id,client_id,amount,source,nudge_id) VALUES (?,?,?,?,?)",
                    (n['biz_id'], n['to_client_id'], seller_credit, f"Nudge swap — sold {ts['time'] if ts else '?'} slot", n['id']))
            if tc and tc['phone']: send_sms(tc['phone'], f"Swap confirmed! ${seller_credit:.2f} credit added to your account. Moved to {fs['time'] if fs else '?'}.", biz_id=n['biz_id'])
            if fc and fc['phone']: send_sms(fc['phone'], f"{tc['name'].split()[0]} accepted! You're at {ts['time'] if ts else '?'} now. ${fee:.2f} charged.", biz_id=n['biz_id'])
        else:
            # BUY mode
            if ts:
                db.execute("UPDATE slots SET client_id=?,service='Nothing Box Session',price=? WHERE id=?", (n['from_client_id'], biz['session_price'], ts['id']))
                inv_token = gen_token()
                db.execute("INSERT INTO invoices (biz_id,client_id,slot_id,amount,invoice_token) VALUES (?,?,?,?,?)", (n['biz_id'], n['from_client_id'], ts['id'], biz['session_price'], inv_token))
            # Credit the seller
            if seller_credit > 0:
                db.execute("INSERT INTO credits (biz_id,client_id,amount,source,nudge_id) VALUES (?,?,?,?,?)",
                    (n['biz_id'], n['to_client_id'], seller_credit, f"Nudge buy — sold {ts['time'] if ts else '?'} slot", n['id']))
            if tc and tc['phone']: send_sms(tc['phone'], f"Slot sold! ${seller_credit:.2f} credit added. Book a new time from your portal.", biz_id=n['biz_id'])
            if fc and fc['phone']: send_sms(fc['phone'], f"You got the {ts['time'] if ts else '?'} slot! ${fee:.2f} charged.", biz_id=n['biz_id'])
        print(f"\n  *** NUDGE ACCEPTED: fee=${fee:.2f} | platform=${platform_cut:.2f} | seller credit=${seller_credit:.2f} ***\n")
    else:
        mx = biz['max_offers'] if biz else 2
        rem = mx - get_attempts(db, n['biz_id'], n['from_client_id'], n['to_slot_id'])
        if fc and fc['phone']: send_sms(fc['phone'], f"{tc['name'].split()[0]} declined.{f' {rem} offer left.' if rem > 0 else ' No offers remaining.'}", biz_id=n['biz_id'])
    db.commit()
    return jsonify({'status':status})


# ═══════════════════════════════════════════════════
# MESSAGING — Client ↔ Admin
# ═══════════════════════════════════════════════════
@app.route('/api/admin/messages/<int:cid>')
@require_admin
def a_messages(cid):
    db = get_db()
    rows = db.execute("SELECT * FROM messages WHERE biz_id=? AND client_id=? ORDER BY created_at ASC", (g.biz_id, cid)).fetchall()
    # Mark client messages as read
    db.execute("UPDATE messages SET read=1 WHERE biz_id=? AND client_id=? AND sender='client' AND read=0", (g.biz_id, cid))
    db.commit()
    return jsonify([dict(r) for r in rows])

@app.route('/api/admin/messages/<int:cid>/send', methods=['POST'])
@require_admin
def a_send_msg(cid):
    db = get_db()
    body = (request.json or {}).get('body', '').strip()
    if not body: return jsonify({'error': 'Message required'}), 400
    client = db.execute("SELECT * FROM clients WHERE id=? AND biz_id=?", (cid, g.biz_id)).fetchone()
    if not client: return jsonify({'error': 'Client not found'}), 404
    db.execute("INSERT INTO messages (biz_id,client_id,sender,body) VALUES (?,?,'admin',?)", (g.biz_id, cid, body))
    db.commit()
    # Also send SMS notification if client has phone
    if client['phone']:
        send_sms(client['phone'], f"New message from {g.biz['name']}: {body[:120]}", biz_id=g.biz_id)
    return jsonify({'status': 'sent'})

@app.route('/api/admin/messages/unread')
@require_admin
def a_unread():
    db = get_db()
    rows = db.execute("""
        SELECT m.client_id, c.name, COUNT(*) as unread, MAX(m.created_at) as latest
        FROM messages m JOIN clients c ON m.client_id=c.id
        WHERE m.biz_id=? AND m.sender='client' AND m.read=0
        GROUP BY m.client_id ORDER BY latest DESC
    """, (g.biz_id,)).fetchall()
    return jsonify([dict(r) for r in rows])

@app.route('/api/client/<token>/messages')
@require_client
def c_messages(token):
    db = get_db()
    rows = db.execute("SELECT * FROM messages WHERE biz_id=? AND client_id=? ORDER BY created_at ASC", (g.biz_id, g.client_id)).fetchall()
    # Mark admin messages as read
    db.execute("UPDATE messages SET read=1 WHERE biz_id=? AND client_id=? AND sender='admin' AND read=0", (g.biz_id, g.client_id))
    db.commit()
    return jsonify([dict(r) for r in rows])

@app.route('/api/client/<token>/messages/send', methods=['POST'])
@require_client
def c_send_msg(token):
    db = get_db()
    body = (request.json or {}).get('body', '').strip()
    if not body: return jsonify({'error': 'Message required'}), 400
    db.execute("INSERT INTO messages (biz_id,client_id,sender,body) VALUES (?,?,'client',?)", (g.biz_id, g.client_id, body))
    db.commit()
    print(f"\n  *** NEW MESSAGE from {g.client['name']}: {body[:80]} ***\n")
    return jsonify({'status': 'sent'})

@app.route('/api/client/<token>/messages/unread')
@require_client
def c_unread(token):
    db = get_db()
    count = db.execute("SELECT COUNT(*) FROM messages WHERE biz_id=? AND client_id=? AND sender='admin' AND read=0",
        (g.biz_id, g.client_id)).fetchone()[0]
    return jsonify({'unread': count})


# ═══════════════════════════════════════════════════
# STRIPE PAYMENTS
# ═══════════════════════════════════════════════════
@app.route('/api/stripe/checkout/<invoice_token>')
def stripe_checkout(invoice_token):
    if not USE_STRIPE: return "Payments not configured", 503
    db = sqlite3.connect(DB_PATH); db.row_factory = sqlite3.Row
    i = db.execute("""SELECT i.*,c.name,c.email,s.date,s.time,s.service
        FROM invoices i JOIN clients c ON i.client_id=c.id JOIN slots s ON i.slot_id=s.id
        WHERE i.invoice_token=? AND i.status!='paid'""", (invoice_token,)).fetchone()
    db.close()
    if not i: return "Invoice not found or already paid", 404
    try:
        session = stripe.checkout.Session.create(
            payment_method_types=['card'],
            line_items=[{
                'price_data': {
                    'currency': 'usd',
                    'unit_amount': int(i['amount'] * 100),
                    'product_data': {
                        'name': i['service'] or 'Nothing Box Session',
                        'description': f"{i['date']} at {i['time']}",
                    },
                },
                'quantity': 1,
            }],
            mode='payment',
            customer_email=i['email'] if i['email'] else None,
            metadata={'invoice_token': invoice_token, 'client_id': str(i['client_id']), 'slot_id': str(i['slot_id'])},
            success_url=BASE_URL + '/api/stripe/success?session_id={CHECKOUT_SESSION_ID}&invoice=' + invoice_token,
            cancel_url=BASE_URL + '/invoice/' + invoice_token,
        )
        return redirect(session.url)
    except Exception as e:
        print(f"  Stripe error: {e}")
        return f"Payment error: {e}", 500

@app.route('/api/stripe/success')
def stripe_success():
    session_id = request.args.get('session_id')
    invoice_token = request.args.get('invoice')
    if session_id and USE_STRIPE:
        try:
            session = stripe.checkout.Session.retrieve(session_id)
            if session.payment_status == 'paid':
                db = sqlite3.connect(DB_PATH); db.row_factory = sqlite3.Row
                db.execute("UPDATE invoices SET status='paid',paid_at=CURRENT_TIMESTAMP WHERE invoice_token=?", (invoice_token,))
                db.commit()
                client = db.execute("""SELECT c.name,c.phone FROM invoices i JOIN clients c ON i.client_id=c.id
                    WHERE i.invoice_token=?""", (invoice_token,)).fetchone()
                db.close()
                if client and client['phone']:
                    send_sms_direct(client['phone'], f"Payment received! Thank you, {client['name'].split()[0]}. Your invoice is marked as paid.")
                print(f"\n  *** PAYMENT RECEIVED: {invoice_token} ***\n")
        except Exception as e:
            print(f"  Stripe verify error: {e}")
    return redirect(f'/invoice/{invoice_token}')

@app.route('/api/stripe/webhook', methods=['POST'])
def stripe_webhook():
    payload = request.data
    sig = request.headers.get('Stripe-Signature')
    # For now, just handle checkout.session.completed without signature verification
    # Add STRIPE_WEBHOOK_SECRET env var and verify signature for production
    try:
        event = json.loads(payload)
        if event.get('type') == 'checkout.session.completed':
            session = event['data']['object']
            invoice_token = session.get('metadata', {}).get('invoice_token')
            if invoice_token and session.get('payment_status') == 'paid':
                db = sqlite3.connect(DB_PATH)
                db.execute("UPDATE invoices SET status='paid',paid_at=CURRENT_TIMESTAMP WHERE invoice_token=?", (invoice_token,))
                db.commit(); db.close()
                print(f"  *** WEBHOOK: Invoice {invoice_token} paid ***")
    except Exception as e:
        print(f"  Webhook error: {e}")
    return '', 200

@app.route('/api/stripe/config')
def stripe_config():
    return jsonify({'pk': STRIPE_PK if USE_STRIPE else '', 'enabled': USE_STRIPE})

@app.route('/api/stripe/book-success')
def stripe_book_success():
    session_id = request.args.get('session_id')
    if not session_id or not USE_STRIPE:
        return redirect('/')
    try:
        session = stripe.checkout.Session.retrieve(session_id)
        if session.payment_status == 'paid':
            meta = session.metadata
            slot_id = int(meta['slot_id'])
            client_id = int(meta['client_id'])
            biz_id = int(meta['biz_id'])
            inv_token = meta.get('inv_token', gen_token())
            client_token = meta.get('client_token', '')
            db = sqlite3.connect(DB_PATH); db.row_factory = sqlite3.Row
            # Confirm booking
            db.execute("UPDATE slots SET status='booked' WHERE id=? AND status='held'", (slot_id,))
            # Create paid invoice
            amount = session.amount_total / 100
            db.execute("INSERT INTO invoices (biz_id,client_id,slot_id,amount,invoice_token,status,paid_at) VALUES (?,?,?,?,?,'paid',CURRENT_TIMESTAMP)",
                (biz_id, client_id, slot_id, amount, inv_token))
            db.commit()
            client = db.execute("SELECT * FROM clients WHERE id=?", (client_id,)).fetchone()
            slot = db.execute("SELECT * FROM slots WHERE id=?", (slot_id,)).fetchone()
            biz = db.execute("SELECT * FROM businesses WHERE id=?", (biz_id,)).fetchone()
            db.close()
            if client and client['phone']:
                send_sms_direct(client['phone'], f"Payment received! Confirmed: {slot['time']} on {slot['date']} at {biz['name']}.")
            print(f"\n  *** BOOKING PAID: {client['name'] if client else '?'} — {slot['date']} {slot['time']} — ${amount:.2f} ***\n")
            return redirect(f'/me/{client_token}' if client_token else '/')
    except Exception as e:
        print(f"  Stripe book-success error: {e}")
    return redirect('/')

@app.route('/api/stripe/book-cancel')
def stripe_book_cancel():
    slot_id = request.args.get('slot_id')
    client_token = request.args.get('token', '')
    if slot_id:
        db = sqlite3.connect(DB_PATH)
        db.execute("UPDATE slots SET client_id=NULL,status='open',service=NULL,price=0 WHERE id=? AND status='held'", (int(slot_id),))
        db.commit(); db.close()
    return redirect(f'/me/{client_token}' if client_token else '/')

def send_sms_direct(to_phone, body):
    """Send SMS without Flask request context (for Stripe callback)"""
    if USE_TWILIO and to_phone:
        try:
            twilio.messages.create(body=body, from_=TWILIO_FROM, to=to_phone)
        except Exception as e:
            print(f"  SMS FAIL: {e}")
    else:
        print(f"  [SMS] → {to_phone}: {body[:80]}...")


# ═══════════════════════════════════════════════════
# SMS WEBHOOK
# ═══════════════════════════════════════════════════
@app.route('/api/sms/webhook', methods=['POST'])
def sms_hook():
    ph = request.form.get('From',''); body = request.form.get('Body','').strip().upper()
    db = sqlite3.connect(DB_PATH); db.row_factory = sqlite3.Row
    db.execute("INSERT INTO sms_log (to_phone,body,direction) VALUES (?,?,'inbound')", (ph, body))
    client = db.execute("SELECT * FROM clients WHERE phone=?", (ph,)).fetchone()
    if client:
        n = db.execute("SELECT * FROM nudges WHERE to_client_id=? AND status='pending' ORDER BY created_at DESC LIMIT 1", (client['id'],)).fetchone()
        if n and body in ('YES','Y','ACCEPT'):
            db.execute("UPDATE nudges SET status='accepted',resolved_at=CURRENT_TIMESTAMP WHERE id=?", (n['id'],))
            fs = db.execute("SELECT * FROM slots WHERE id=?", (n['from_slot_id'],)).fetchone()
            ts = db.execute("SELECT * FROM slots WHERE id=?", (n['to_slot_id'],)).fetchone()
            if fs and ts:
                db.execute("UPDATE slots SET client_id=?,service=?,price=? WHERE id=?", (ts['client_id'],ts['service'],ts['price'],fs['id']))
                db.execute("UPDATE slots SET client_id=?,service=?,price=? WHERE id=?", (fs['client_id'],fs['service'],fs['price'],ts['id']))
            db.commit(); db.close()
            return '<Response><Message>Swap confirmed! Payment processing.</Message></Response>', 200, {'Content-Type':'text/xml'}
        elif n and body in ('NO','N','DECLINE'):
            db.execute("UPDATE nudges SET status='declined',resolved_at=CURRENT_TIMESTAMP WHERE id=?", (n['id'],))
            db.commit(); db.close()
            return '<Response><Message>Declined. Your appointment stays the same.</Message></Response>', 200, {'Content-Type':'text/xml'}
    db.commit(); db.close()
    return '<Response></Response>', 200, {'Content-Type':'text/xml'}


# ═══════════════════════════════════════════════════
# EXPIRE CHECK
# ═══════════════════════════════════════════════════
@app.before_request
def expire_nudges():
    try:
        db = get_db()
        for n in db.execute("SELECT * FROM nudges WHERE status='pending' AND expires_at<?", (datetime.now().isoformat(),)).fetchall():
            db.execute("UPDATE nudges SET status='expired',resolved_at=CURRENT_TIMESTAMP WHERE id=?", (n['id'],))
            fc = db.execute("SELECT * FROM clients WHERE id=?", (n['from_client_id'],)).fetchone()
            if fc and fc['phone']: send_sms(fc['phone'], "Your nudge offer expired.", biz_id=n['biz_id'])
        # Release held slots older than 15 minutes (abandoned Stripe checkouts)
        cutoff = (datetime.now() - timedelta(minutes=15)).isoformat()
        db.execute("UPDATE slots SET client_id=NULL,status='open',service=NULL,price=0 WHERE status='held' AND created_at<?", (cutoff,))
        db.commit()
    except: pass


# ═══════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════
init_db()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8888))
    print(f"\n{'='*52}\n  NUDGE — the Fair Exchange\n  {BASE_URL}\n{'='*52}")
    if USE_TWILIO: print(f"  ✓ Twilio: {TWILIO_FROM}")
    else: print("  ⚠ SMS simulated")
    if IS_RAILWAY: print(f"  ✓ Railway (data: {DATA_DIR})")
    else: print(f"  Local dev (data: {DATA_DIR})")
    print(f"{'='*52}\n")
    app.run(host='0.0.0.0', port=port, debug=not IS_RAILWAY)
