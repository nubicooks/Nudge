"""
Nudge — the Fair Exchange
Flask + SQLite + Twilio SMS
Deploy: Railway with persistent /data volume
"""
import os, json, sqlite3, secrets, hashlib, shutil, time
from datetime import datetime, timedelta
from functools import wraps
from flask import Flask, request, jsonify, send_from_directory, g, redirect, make_response

# ═══════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════
IS_RAILWAY = bool(os.environ.get('RAILWAY_ENVIRONMENT'))

app = Flask(__name__, static_folder=None)
app.secret_key = os.environ.get('SECRET_KEY', secrets.token_hex(32))

# Security headers
@app.after_request
def security_headers(response):
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['X-XSS-Protection'] = '1; mode=block'
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    if IS_RAILWAY:
        response.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains'
    return response

# Rate limiting for admin auth (simple in-memory)
_auth_attempts = {}
def check_rate_limit(ip):
    now = time.time()
    attempts = _auth_attempts.get(ip, [])
    attempts = [t for t in attempts if now - t < 300]  # 5 min window
    _auth_attempts[ip] = attempts
    if len(attempts) >= 10: return False  # 10 attempts per 5 min
    attempts.append(now)
    _auth_attempts[ip] = attempts
    return True

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
    for col, ctype, default in [('status','TEXT',"'active'"),('deleted_at','TIMESTAMP','NULL'),('delete_reason','TEXT','NULL'),('dob','TEXT','NULL'),('gender','TEXT','NULL'),('address','TEXT','NULL'),('emergency_contact','TEXT','NULL'),('emergency_phone','TEXT','NULL'),('medical_notes','TEXT','NULL'),('archived','INTEGER','0'),('archived_at','TIMESTAMP','NULL'),('sms_opt_out','INTEGER','0'),('eligibility_confirmed','INTEGER','0'),('eligibility_confirmed_at','TIMESTAMP','NULL'),('consent_acknowledged','INTEGER','0'),('consent_acknowledged_at','TIMESTAMP','NULL'),('session_address','TEXT','NULL'),('session_notes','TEXT','NULL'),('consent_signature','TEXT','NULL'),('consent_clauses','TEXT','NULL'),('consent_ip','TEXT','NULL')]:
        try: db.execute(f"ALTER TABLE clients ADD COLUMN {col} {ctype} DEFAULT {default}")
        except: pass
    for col, ctype, default in [('practitioner_note','TEXT','NULL'),('session_data','TEXT','NULL'),('sent_at','TIMESTAMP','NULL'),('status','TEXT',"'draft'")]:
        try: db.execute(f"ALTER TABLE reports ADD COLUMN {col} {ctype} DEFAULT {default}")
        except: pass
    for col, ctype, default in [
        ('email','TEXT','NULL'),
        ('website','TEXT','NULL'),
        ('service_area','TEXT','NULL'),
        ('practitioner_name','TEXT','NULL'),
        ('practitioner_initial','TEXT','NULL'),
        ('pack_price','REAL','250'),
        ('free_trial','INTEGER','1'),
        ('buffer_min','INTEGER','15'),
        ('available_days','TEXT',"'1,2,3,4,5,6,7'"),
        ('sms_signature','TEXT','NULL'),
        ('auto_reply','TEXT','NULL'),
        ('notify_email','INTEGER','1'),
        ('notify_sms','INTEGER','0'),
        ('default_report_note','TEXT','NULL'),
        ('data_retention_days','INTEGER','0')
    ]:
        try: db.execute(f"ALTER TABLE businesses ADD COLUMN {col} {ctype} DEFAULT {default}")
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

def send_sms_to_client(client_id, body, biz_id=None, nudge_id=None):
    """Send SMS to a client, respecting archive status and opt-out flag.
    Returns True if sent, False if blocked."""
    db = get_db()
    c = db.execute("SELECT phone, archived, sms_opt_out, status FROM clients WHERE id=?", (client_id,)).fetchone()
    if not c or not c['phone']:
        return False
    if c['archived'] or c['sms_opt_out'] or (c['status'] == 'deleted'):
        print(f"  [SMS blocked — client #{client_id} archived/opt-out/deleted]")
        return False
    send_sms(c['phone'], body, biz_id=biz_id, nudge_id=nudge_id)
    return True

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
        ip = request.remote_addr or '0.0.0.0'
        if not check_rate_limit(ip):
            return jsonify({'error': 'Too many attempts. Try again in 5 minutes.'}), 429
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
        if client['archived']:
            return jsonify({'error': 'This session has been archived. Please contact the practitioner.'}), 403
        if (client['status'] or 'active') == 'deleted':
            return jsonify({'error': 'Invalid link'}), 401
        g.client_id = client['id']; g.client = dict(client)
        g.biz_id = client['biz_id']
        g.biz = dict(db.execute("SELECT * FROM businesses WHERE id=?", (client['biz_id'],)).fetchone())
        return f(*a, **kw)
    return w


# ═══════════════════════════════════════════════════
# REPORT GENERATION
# ═══════════════════════════════════════════════════
REPORT_BRAND_CSS = """
:root{--bg:#050608;--surface:#0a0d10;--card:#0f1215;--border:#1a1e24;--teal:#00e5c7;--teal-dim:rgba(0,229,199,.08);--gold:#e8a44a;--gold-dim:rgba(232,164,74,.08);--text:#e2e4e9;--text2:#c4c8cf;--text3:#9ca3af;--dim:#5c6370;--red:#ef4444}
*{margin:0;padding:0;box-sizing:border-box}
body{background:var(--bg);color:var(--text);font-family:'DM Sans',system-ui,sans-serif;min-height:100vh;line-height:1.8;padding:32px 20px 60px}
.wrap{max-width:900px;margin:0 auto;background:var(--bg);border:1px solid var(--border);border-radius:12px;overflow:hidden;padding:40px}
h1,h2,h3{font-family:'Cormorant Garamond',Georgia,serif;font-weight:300;letter-spacing:.5px;color:var(--text);margin:0}
.head{border-bottom:1px solid var(--border);padding-bottom:20px;margin-bottom:28px}
.brand{font-family:'Cormorant Garamond',serif;font-size:16px;letter-spacing:2px}
.brand .sol{color:var(--gold)}
.brand .sub{display:block;font-family:'Space Mono',monospace;font-size:8px;letter-spacing:4px;color:var(--dim);margin-top:2px}
.head h1{font-size:32px;margin:18px 0 6px}
.meta{display:grid;grid-template-columns:repeat(4,1fr);gap:16px;font-family:'Space Mono',monospace;font-size:10px;color:var(--dim);letter-spacing:2px;margin-top:16px;padding-top:16px;border-top:1px solid var(--border)}
.meta b{color:var(--text3);font-weight:400;display:block;font-size:11px;margin-top:2px}
.section{margin-bottom:36px}
.sec-label{font-family:'Space Mono',monospace;font-size:9px;letter-spacing:3px;color:var(--teal);margin-bottom:10px;display:block;padding-bottom:8px;border-bottom:1px solid var(--border)}
.section h2{font-size:22px;margin-bottom:8px}
.section p{font-size:13.5px;color:var(--text3);line-height:1.9;margin:0 0 12px}
.framer{font-family:'Cormorant Garamond',serif;font-style:italic;font-size:15px;color:var(--text3);margin:0;line-height:1.8;padding:16px 0}

/* Practitioner's Corner */
.pc-wrap{background:var(--surface);border:1px solid var(--border);border-radius:8px;overflow:hidden}
.pc-banner{background:var(--card);padding:16px 28px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:14px}
.pc-avatar{width:36px;height:36px;border-radius:50%;background:rgba(0,229,199,.08);border:1px solid rgba(0,229,199,.3);display:flex;align-items:center;justify-content:center;font-family:'Cormorant Garamond',serif;font-size:15px;color:var(--teal);flex-shrink:0;letter-spacing:1px}
.pc-who{flex:1;min-width:0}
.pc-name{font-family:'Cormorant Garamond',serif;font-size:17px;color:var(--text);letter-spacing:.3px}
.pc-role{font-family:'Space Mono',monospace;font-size:8.5px;color:var(--dim);letter-spacing:2px;text-transform:uppercase;margin-top:2px;display:block}
.pc-sid{font-family:'Space Mono',monospace;font-size:9px;color:var(--dim);letter-spacing:1.5px;text-align:right;line-height:1.6}
.pc-body{padding:26px 32px}
.pc-quote{font-family:'Cormorant Garamond',serif;font-size:48px;color:var(--border);line-height:0.4;margin-bottom:4px;display:block}
.pc-note{font-family:'DM Sans',sans-serif;font-size:14.5px;color:var(--text);line-height:2}
.pc-note p{margin:0 0 14px}
.pc-note p:last-child{margin:0}
.pc-foot{padding:18px 32px;border-top:1px solid var(--border);background:var(--bg);display:flex;justify-content:space-between;align-items:center}
.pc-hw{font-family:'Cormorant Garamond',serif;font-size:20px;color:var(--gold)}
.pc-date{font-family:'Space Mono',monospace;font-size:9px;color:var(--dim);letter-spacing:2px;text-transform:uppercase}
.pc-empty{background:var(--bg);border:1px dashed var(--border);border-radius:6px;padding:18px 22px;margin-top:14px;font-family:'DM Sans',sans-serif;font-size:12px;color:var(--dim);font-style:italic;line-height:1.7;text-align:center}

/* Document notes */
.legal{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:20px 24px;margin-top:12px}
.legal p{font-size:12px;color:var(--dim);line-height:1.8;margin:0 0 8px;font-family:'DM Sans',sans-serif}
.legal p:last-child{margin:0}
.legal p b{color:var(--text3);font-weight:400}

.rp-foot{padding-top:24px;margin-top:24px;border-top:1px solid var(--border);text-align:center;font-family:'Space Mono',monospace;font-size:9px;color:var(--dim);letter-spacing:2px}
.rp-foot a{color:var(--teal);text-decoration:none}

/* Stub placeholder for unfilled sections (Phase 1) */
.stub{background:var(--surface);border:1px dashed var(--border);border-radius:8px;padding:24px 28px;text-align:center}
.stub h3{font-family:'Cormorant Garamond',serif;font-size:18px;color:var(--text3);margin-bottom:8px;font-weight:400}
.stub p{font-size:12.5px;color:var(--dim);line-height:1.8;margin:0;font-family:'DM Sans',sans-serif}

@media(max-width:720px){.wrap{padding:24px 18px}.meta{grid-template-columns:repeat(2,1fr)}}
"""

def _report_header_html(client, slot, session_id):
    date = slot['date'] if slot and 'date' in slot.keys() else datetime.now().strftime('%Y-%m-%d')
    return f"""
<div class="head">
  <div class="brand">the <span class="sol">Sol</span> Standard<span class="sub">THE NOTHING BOX &middot; SESSION REPORT</span></div>
  <h1>Session &middot; {esc_html(client['name'].split()[0])}</h1>
  <div class="meta">
    <span>DATE<b>{esc_html(date)}</b></span>
    <span>DURATION<b>45 min</b></span>
    <span>SESSION ID<b>{esc_html(session_id)}</b></span>
    <span>PRACTITIONER<b>Ndubisi</b></span>
  </div>
</div>
<p class="framer">A record of what was played, what was measured, how the signals coupled across channels, and what the instrument observed. Nothing else.</p>
"""

def _report_practitioner_corner_html(client, note, session_id, date_str):
    if note:
        note_paragraphs = ''.join(f'<p>{esc_html(p)}</p>' for p in note.split('\n') if p.strip())
        body = f"""
<div class="pc-wrap">
  <div class="pc-banner">
    <div class="pc-avatar">N</div>
    <div class="pc-who">
      <div class="pc-name">Ndubisi</div>
      <span class="pc-role">Practitioner &middot; The Sol Standard</span>
    </div>
    <div class="pc-sid">SESSION {esc_html(session_id)}<br>{esc_html(date_str)}</div>
  </div>
  <div class="pc-body">
    <span class="pc-quote">"</span>
    <div class="pc-note">{note_paragraphs}</div>
  </div>
  <div class="pc-foot">
    <div class="pc-hw">Ndubisi</div>
    <div class="pc-date">written {esc_html(date_str)}</div>
  </div>
</div>
"""
    else:
        body = """
<div class="pc-empty">
Some sessions end without a written note. That is also an answer. The data above is complete on its own.
</div>
"""
    return body

def _report_legal_html():
    return """
<div class="legal">
  <p><b>This is a sensor record.</b> It contains the readings an instrument made during a specific 45-minute period in a specific room.</p>
  <p><b>This is not a medical document.</b> It does not contain a diagnosis, a prognosis, a treatment record, or a recommendation. It describes the behavior of an instrument, not the condition of a person.</p>
  <p><b>The numbers describe the instrument.</b> They do not describe the person the instrument was near.</p>
  <p><b>No comparison is made to any other session or any other individual.</b> The data in this report stands alone.</p>
</div>
"""

def esc_html(s):
    if s is None: return ''
    return (str(s).replace('&','&amp;').replace('<','&lt;').replace('>','&gt;')
        .replace('"','&quot;').replace("'",'&#39;'))

def _report_stub_html(client, slot_id, practitioner_note):
    """Phase 1 stub — used when no real session_data is supplied.
    Shows header, Practitioner's Corner if note provided, and legal footer.
    Data sections render as placeholders awaiting real session data."""
    db = get_db()
    slot = None
    if slot_id:
        slot = db.execute("SELECT * FROM slots WHERE id=?", (slot_id,)).fetchone()
    date_str = slot['date'] if slot and slot['date'] else datetime.now().strftime('%Y-%m-%d')
    session_id = f"SS-{date_str.replace('-','')}-C{client['id']:03d}"

    stub_block = """
<section class="section">
  <span class="sec-label">PARTS ONE THROUGH EIGHT &middot; DATA SECTIONS</span>
  <div class="stub">
    <h3>Session data pending</h3>
    <p>The full report with sequence, channels, coupling matrix, observations, and plain-language translation will be generated here after the session is run and the instrument data is processed.</p>
  </div>
</section>
"""

    pc_block = _report_practitioner_corner_html(client, practitioner_note, session_id, date_str)

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Session Report &middot; {esc_html(client['name'])}</title>
<link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:wght@300;400&family=DM+Sans:wght@300;400;500&family=Space+Mono:wght@400;700&display=swap" rel="stylesheet">
<style>{REPORT_BRAND_CSS}</style>
</head><body>
<div class="wrap">
  {_report_header_html(client, slot, session_id)}
  {stub_block}
  <section class="section">
    <span class="sec-label">PART NINE &middot; PRACTITIONER'S CORNER</span>
    <h2>A note from the person who ran the box.</h2>
    {pc_block}
  </section>
  <section class="section">
    <span class="sec-label">DOCUMENT NOTES</span>
    {_report_legal_html()}
  </section>
  <footer class="rp-foot">
    the Sol Standard &middot; the Nothing Box &middot; Session {esc_html(session_id)}<br>
    <a href="mailto:hello@thesolstandard.com">hello@thesolstandard.com</a> &middot; (240) 356-3393
  </footer>
</div>
</body></html>"""

def generate_report_html(client, slot_id, practitioner_note, session_data):
    """Full report generator. Phase 2 path.
    If session_data is None or empty, falls back to stub.
    When real data is supplied, renders the full 9-part report.
    Optionally uses Claude API for natural-language sections if CLAUDE_API_KEY is set."""
    if not session_data:
        return _report_stub_html(client, slot_id, practitioner_note)
    # Phase 2: real data is present. The full template integration goes here.
    # For now, use the stub with a marker so we know data arrived.
    # TODO: integrate the full 9-part template from the designed report when
    # the session CSV schema is finalized.
    return _report_stub_html(client, slot_id, practitioner_note)



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

@app.route('/consent')
def consent_page():
    return send_from_directory(SCRIPT_DIR, 'consent.html')

@app.route('/welcome/<token>')
def welcome_page(token):
    return send_from_directory(SCRIPT_DIR, 'nudge_welcome.html')

@app.route('/onboard/<token>')
def onboard_page(token):
    return send_from_directory(SCRIPT_DIR, 'nudge_onboard.html')

@app.route('/report/<token>')
def report_page(token):
    """Public-facing report page. Token is the report's report_token (unique per report).
    Archived clients still get access via the report URL (they already had the session).
    Deleted clients do not."""
    db = get_db()
    r = db.execute("""SELECT r.*, c.name, c.status as c_status
        FROM reports r JOIN clients c ON r.client_id=c.id
        WHERE r.report_token=?""", (token,)).fetchone()
    if not r:
        return make_response("<!DOCTYPE html><html><body style='background:#050608;color:#9ca3af;font-family:system-ui;padding:60px;text-align:center'><h2 style='font-weight:300'>Report not found</h2><p>This link may be invalid or expired.</p></body></html>", 404)
    if (r['c_status'] or 'active') == 'deleted':
        return make_response("<!DOCTYPE html><html><body style='background:#050608;color:#9ca3af;font-family:system-ui;padding:60px;text-align:center'><h2 style='font-weight:300'>Report unavailable</h2></body></html>", 404)
    if r['status'] == 'draft':
        # Don't show drafts to clients
        return make_response("<!DOCTYPE html><html><body style='background:#050608;color:#9ca3af;font-family:system-ui;padding:60px;text-align:center'><h2 style='font-weight:300'>Report not yet available</h2><p>Please check back shortly.</p></body></html>", 403)
    resp = make_response(r['report_html'] or '<h1>Report not generated</h1>')
    resp.headers['Content-Type'] = 'text/html; charset=utf-8'
    return resp


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

@app.route('/api/public/consent', methods=['POST'])
def pub_consent():
    """Accept a signed consent submission. Creates a provisional client record with status='consented'
    and returns a token. Full client info (name/phone/email) is collected at /book."""
    d = request.json or {}
    eligible = bool(d.get('eligible'))
    clauses = d.get('clauses') or {}
    signature = (d.get('signature') or '').strip()
    if not eligible: return jsonify({'error': 'not_eligible',
        'message': 'Sessions are not available to anyone with a medical implant.'}), 400
    required_clauses = ['not_medical', 'privacy', 'liability']
    for k in required_clauses:
        if not clauses.get(k):
            return jsonify({'error': 'missing_clause',
                'message': 'Please check all acknowledgment boxes before signing.'}), 400
    if not signature: return jsonify({'error': 'signature_required',
        'message': 'Please type your name to sign.'}), 400
    db = get_db()
    biz = get_biz(db)
    if not biz: return jsonify({'error': 'Business not found'}), 500
    import json as _json
    clauses_json = _json.dumps(clauses)
    client_ip = request.headers.get('X-Forwarded-For', request.remote_addr or '').split(',')[0].strip()
    # Create a provisional record. The real name/phone/email come in at /book via the same token.
    token = gen_token()
    placeholder_name = f"[consent pending] {signature}"
    db.execute("""INSERT INTO clients
        (biz_id,name,token,status,
         eligibility_confirmed,eligibility_confirmed_at,
         consent_acknowledged,consent_acknowledged_at,
         consent_signature,consent_clauses,consent_ip)
        VALUES (?,?,?,'consented',1,CURRENT_TIMESTAMP,1,CURRENT_TIMESTAMP,?,?,?)""",
        (biz['id'], placeholder_name, token, signature, clauses_json, client_ip))
    cid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    db.commit()
    print(f"\n  *** CONSENT RECEIVED: signed '{signature}' | token={token[:8]}... ***")
    return jsonify({'status': 'consented', 'token': token, 'client_id': cid})

@app.route('/api/public/client-by-token/<token>')
def pub_client_by_token(token):
    """Look up an existing consented client by their token (used by /book after consent)."""
    db = get_db()
    biz = get_biz(db)
    if not biz: return jsonify({'error': 'Business not found'}), 500
    c = db.execute("""SELECT id, name, phone, email, session_address, consent_acknowledged
        FROM clients WHERE biz_id=? AND token=? AND status!='deleted'""",
        (biz['id'], token)).fetchone()
    if not c: return jsonify({'error': 'not_found'}), 404
    parts = (c['name'] or '').split(' ', 1)
    return jsonify({
        'first': parts[0] if parts else '',
        'last': parts[1] if len(parts) > 1 else '',
        'phone': c['phone'] or '',
        'email': c['email'] or '',
        'address': c['session_address'] or '',
        'consented': bool(c['consent_acknowledged']),
    })

@app.route('/api/public/inquiry', methods=['POST'])
def pub_inquiry():
    d = request.json
    first = d.get('first', '').strip()
    last = d.get('last', '').strip()
    phone = d.get('phone', '').strip()
    email = d.get('email', '').strip()
    tier = d.get('tier', 'free').strip()
    eligibility = bool(d.get('eligibility_confirmed'))
    consent = bool(d.get('consent_acknowledged'))
    session_address = (d.get('session_address') or '').strip()
    session_notes = (d.get('session_notes') or '').strip()
    consent_token = (d.get('consent_token') or '').strip()
    # Validation
    if not first or not last: return jsonify({'error': 'Name required'}), 400
    if not phone and not email: return jsonify({'error': 'Phone or email required'}), 400
    if not eligibility:
        return jsonify({'error': 'eligibility_not_confirmed',
            'message': 'Sessions are not available to anyone with an active electronic implant. If you do not have one of the listed implants, please confirm eligibility before booking.'}), 400
    if not consent:
        return jsonify({'error': 'consent_not_acknowledged',
            'message': 'Please acknowledge the information packet and the not-a-medical-device framing to proceed.'}), 400
    name = first + ' ' + last
    tier_label = {'free': 'Try for Free', 'single': 'Single Session', 'pack': '4-Session Pack'}.get(tier, tier)
    db = get_db()
    biz = get_biz(db)
    if not biz: return jsonify({'error': 'Business not found'}), 500
    # If we have a consent_token, update the provisional record from the consent flow
    client = None
    if consent_token:
        client = db.execute("SELECT * FROM clients WHERE biz_id=? AND token=?",
            (biz['id'], consent_token)).fetchone()
    # Otherwise look up by name as before
    if not client:
        client = db.execute("SELECT * FROM clients WHERE biz_id=? AND name=? COLLATE NOCASE",
            (biz['id'], name)).fetchone()
    if client:
        cid = client['id']; token = client['token']
        if phone: db.execute("UPDATE clients SET phone=? WHERE id=?", (phone, cid))
        if email: db.execute("UPDATE clients SET email=? WHERE id=?", (email, cid))
        db.execute("""UPDATE clients SET name=?, notes=?, status='new',
            eligibility_confirmed=1, eligibility_confirmed_at=CURRENT_TIMESTAMP,
            consent_acknowledged=1, consent_acknowledged_at=CURRENT_TIMESTAMP,
            session_address=COALESCE(NULLIF(?,''), session_address),
            session_notes=COALESCE(NULLIF(?,''), session_notes)
            WHERE id=?""",
            (name, f"Tier: {tier_label}", session_address, session_notes, cid))
    else:
        token = gen_token()
        notes = f"Tier: {tier_label}"
        db.execute("""INSERT INTO clients
            (biz_id,name,phone,email,token,notes,status,
             eligibility_confirmed,eligibility_confirmed_at,
             consent_acknowledged,consent_acknowledged_at,
             session_address,session_notes)
            VALUES (?,?,?,?,?,?,'new',1,CURRENT_TIMESTAMP,1,CURRENT_TIMESTAMP,?,?)""",
            (biz['id'], name, phone, email, token, notes,
             session_address or None, session_notes or None))
        cid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    db.commit()
    if phone:
        send_sms(phone, f"Hi {first}! Thanks for your interest in {biz['name']}. We'll be in touch soon to get your session scheduled.",
            biz_id=biz['id'])
    print(f"\n  *** NEW INQUIRY: {name} | {phone} | {email} | {tier_label} ***")
    print(f"  *** Eligibility confirmed, consent acknowledged ***")
    if session_address: print(f"  *** Address: {session_address} ***")
    if session_notes: print(f"  *** Notes: {session_notes} ***\n")
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
@app.route('/api/admin/ping')
@require_admin
def a_ping():
    return jsonify({'ok': True, 'biz': g.biz.get('name'), 'biz_id': g.biz_id})

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
    # Set client to active
    db.execute("UPDATE clients SET status='active' WHERE id=? AND COALESCE(status,'new') NOT IN ('active','deleted')", (cid,))
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
        d = request.json or {}
        # Build UPDATE dynamically so we don't overwrite fields the client didn't send
        updatable = ['name','phone','email','website','service_area','practitioner_name','practitioner_initial',
                     'session_price','pack_price','session_duration','buffer_min','available_days',
                     'start_hour','end_hour','offer_timer_min','max_offers','default_nudge_pct','platform_fee_pct',
                     'free_trial','sms_signature','auto_reply','notify_email','notify_sms',
                     'default_report_note','data_retention_days']
        # Map frontend legacy field names to column names
        aliases = {'price':'session_price','duration':'session_duration','timer':'offer_timer_min','default_pct':'default_nudge_pct'}
        sets, vals = [], []
        for k in updatable:
            src = k
            # Accept alias keys too
            for a, real in aliases.items():
                if real == k and a in d and k not in d:
                    src = a; break
            if src in d:
                sets.append(f"{k}=?")
                vals.append(d.get(src))
        if sets:
            vals.append(g.biz_id)
            db.execute(f"UPDATE businesses SET {','.join(sets)} WHERE id=?", vals)
            db.commit()
        return jsonify({'status':'saved'})
    # GET — return all settings plus integration status (read-only indicators)
    out = dict(g.biz)
    out.pop('api_key', None)      # never return this
    out.pop('admin_pin', None)    # never return this
    out['integrations'] = {
        'stripe': {'configured': bool(os.environ.get('STRIPE_SK')),
                   'mode': ('live' if (os.environ.get('STRIPE_SK','').startswith('sk_live_')) else ('test' if os.environ.get('STRIPE_SK','').startswith('sk_test_') else 'none'))},
        'twilio': {'configured': USE_TWILIO, 'from': TWILIO_FROM if USE_TWILIO else None},
        'claude': {'configured': bool(os.environ.get('CLAUDE_API_KEY') or os.environ.get('ANTHROPIC_API_KEY'))}
    }
    return jsonify(out)

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

@app.route('/api/admin/block-range', methods=['POST'])
@require_admin
def a_block_range():
    """Block open slots across a date range, optionally filtered by time-of-day.
    POST body: {
      start_date: 'YYYY-MM-DD', end_date: 'YYYY-MM-DD',
      start_time?: 'HH:MM' (inclusive lower bound), end_time?: 'HH:MM' (exclusive upper bound),
      reason?: str, force?: bool
    }
    If start_time/end_time are provided, only slots whose time falls in [start_time, end_time) are blocked.
    If omitted, the whole day is blocked (existing behavior).
    When force=true, booked slots in scope are also cleared."""
    d = request.json or {}
    start = (d.get('start_date') or '').strip()
    end = (d.get('end_date') or start).strip()
    reason = (d.get('reason') or 'Blocked').strip() or 'Blocked'
    force = bool(d.get('force'))
    start_time = (d.get('start_time') or '').strip() or None
    end_time = (d.get('end_time') or '').strip() or None
    if not start or not end: return jsonify({'error': 'start_date and end_date required'}), 400
    if end < start: return jsonify({'error': 'end_date must be on or after start_date'}), 400
    if (start_time and not end_time) or (end_time and not start_time):
        return jsonify({'error': 'start_time and end_time must both be provided, or both omitted'}), 400
    if start_time and end_time and end_time <= start_time:
        return jsonify({'error': 'end_time must be after start_time'}), 400
    db = get_db()
    try:
        d0 = datetime.strptime(start, '%Y-%m-%d')
        d1 = datetime.strptime(end, '%Y-%m-%d')
    except Exception:
        return jsonify({'error': 'Invalid date format'}), 400
    # Ensure slots exist for the range
    cur = d0
    while cur <= d1:
        ensure_slots(db, g.biz_id, cur.strftime('%Y-%m-%d'))
        cur += timedelta(days=1)
    # Build the time filter fragment
    time_filter = ''
    time_params = []
    if start_time and end_time:
        time_filter = ' AND time>=? AND time<?'
        time_params = [start_time, end_time]
    # Count what's booked in scope (date range + optional time window)
    booked_sql = f"SELECT COUNT(*) FROM slots WHERE biz_id=? AND date>=? AND date<=?{time_filter} AND client_id IS NOT NULL AND status='booked'"
    booked_count = db.execute(booked_sql, [g.biz_id, start, end] + time_params).fetchone()[0]
    if booked_count > 0 and not force:
        scope = f"{start}" + (f" to {end}" if end != start else "")
        if start_time and end_time:
            scope += f" ({start_time}–{end_time})"
        return jsonify({'error': 'has_bookings', 'booked_count': booked_count,
            'message': f'{booked_count} booked appointment(s) in {scope}. They will not be cancelled. Add force=true to cancel them, or contact clients to reschedule first.'}), 409
    if force and booked_count > 0:
        upd = f"UPDATE slots SET client_id=NULL, status='blocked', service=?, price=0 WHERE biz_id=? AND date>=? AND date<=?{time_filter}"
        db.execute(upd, [reason, g.biz_id, start, end] + time_params)
    else:
        upd = f"UPDATE slots SET status='blocked', service=? WHERE biz_id=? AND date>=? AND date<=?{time_filter} AND client_id IS NULL AND status!='booked'"
        db.execute(upd, [reason, g.biz_id, start, end] + time_params)
    db.commit()
    count_sql = f"SELECT COUNT(*) FROM slots WHERE biz_id=? AND date>=? AND date<=?{time_filter} AND status='blocked'"
    blocked_count = db.execute(count_sql, [g.biz_id, start, end] + time_params).fetchone()[0]
    return jsonify({'status': 'blocked', 'blocked_count': blocked_count,
        'cancelled_bookings': booked_count if force else 0})

@app.route('/api/admin/unblock-range', methods=['POST'])
@require_admin
def a_unblock_range():
    d = request.json or {}
    start = (d.get('start_date') or '').strip()
    end = (d.get('end_date') or start).strip()
    start_time = (d.get('start_time') or '').strip() or None
    end_time = (d.get('end_time') or '').strip() or None
    if not start or not end: return jsonify({'error': 'start_date and end_date required'}), 400
    db = get_db()
    time_filter = ''
    time_params = []
    if start_time and end_time:
        time_filter = ' AND time>=? AND time<?'
        time_params = [start_time, end_time]
    upd = f"UPDATE slots SET status='open', service=NULL WHERE biz_id=? AND date>=? AND date<=?{time_filter} AND status='blocked' AND client_id IS NULL"
    db.execute(upd, [g.biz_id, start, end] + time_params)
    db.commit()
    return jsonify({'status': 'unblocked'})

@app.route('/api/admin/blocked-days')
@require_admin
def a_blocked_days():
    """Return upcoming dates that have any blocked slots, grouped by date.
    Shows fully-blocked days, partial-block days, and the reason (first found)."""
    db = get_db()
    today = datetime.now().strftime('%Y-%m-%d')
    rows = db.execute("""
        SELECT date,
            SUM(CASE WHEN status='blocked' THEN 1 ELSE 0 END) as blocked,
            SUM(CASE WHEN status='open' THEN 1 ELSE 0 END) as open_ct,
            SUM(CASE WHEN status='booked' THEN 1 ELSE 0 END) as booked,
            COUNT(*) as total,
            MIN(CASE WHEN status='blocked' THEN service END) as reason
        FROM slots
        WHERE biz_id=? AND date>=?
        GROUP BY date
        HAVING blocked > 0
        ORDER BY date
    """, (g.biz_id, today)).fetchall()
    return jsonify([dict(r) for r in rows])


# ═══════════════════════════════════════════════════
# ADMIN API — Client list & detail
# ═══════════════════════════════════════════════════
@app.route('/api/admin/clients')
@require_admin
def a_clients():
    db = get_db()
    show = request.args.get('show', 'active')  # 'active', 'archived', 'all'
    if show == 'archived':
        filt = "AND COALESCE(c.archived,0)=1 AND COALESCE(c.status,'active')!='deleted'"
    elif show == 'all':
        filt = "AND COALESCE(c.status,'active')!='deleted'"
    else:
        filt = "AND COALESCE(c.archived,0)=0 AND COALESCE(c.status,'active')!='deleted'"
    rows = db.execute(f"""
        SELECT c.*, COUNT(s.id) as visit_count,
            COALESCE(SUM(s.price),0) as total_spent,
            MAX(s.date) as last_visit
        FROM clients c
        LEFT JOIN slots s ON s.client_id=c.id AND s.status='booked'
        WHERE c.biz_id=? {filt}
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
    if client['archived']: return jsonify({'error': 'Client is archived. Unarchive to resume communication.'}), 400
    link = f"{BASE_URL}/welcome/{client['token']}"
    send_sms_to_client(cid, f"Hi {client['name'].split()[0]}! Here's everything you need to know before your session at {g.biz['name']}: {link}", biz_id=g.biz_id)
    db.execute("INSERT INTO messages (biz_id,client_id,sender,body) VALUES (?,?,'admin',?)",
        (g.biz_id, cid, f"Here's everything you need to know before your session. Please read through and give your consent to proceed: {link}"))
    notes = client['notes'] or ''
    if 'Info package sent' not in notes:
        if notes: notes += ' | '
        notes += f'Info package sent {datetime.now().strftime("%Y-%m-%d")}'
    db.execute("UPDATE clients SET notes=?,status='info_sent' WHERE id=?", (notes, cid))
    db.commit()
    return jsonify({'status': 'sent', 'link': link})

@app.route('/api/admin/clients/<int:cid>/send-welcome', methods=['POST'])
@require_admin
def a_send_welcome(cid):
    db = get_db()
    client = db.execute("SELECT * FROM clients WHERE id=? AND biz_id=?", (cid, g.biz_id)).fetchone()
    if not client: return jsonify({'error': 'Not found'}), 404
    if client['archived']: return jsonify({'error': 'Client is archived. Unarchive to resume communication.'}), 400
    link = f"{BASE_URL}/onboard/{client['token']}"
    send_sms_to_client(cid, f"Hi {client['name'].split()[0]}! Your welcome packet is ready. Review the session details and book your appointment: {link}", biz_id=g.biz_id)
    db.execute("INSERT INTO messages (biz_id,client_id,sender,body) VALUES (?,?,'admin',?)",
        (g.biz_id, cid, f"Your welcome packet is ready! Review the details and pick a time for your session: {link}"))
    notes = client['notes'] or ''
    if 'Welcome sent' not in notes:
        if notes: notes += ' | '
        notes += f'Welcome sent {datetime.now().strftime("%Y-%m-%d")}'
    db.execute("UPDATE clients SET notes=?,status='welcome_sent' WHERE id=?", (notes, cid))
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
# ADMIN API — Archive / Unarchive
# ═══════════════════════════════════════════════════
@app.route('/api/admin/clients/<int:cid>/archive', methods=['POST'])
@require_admin
def a_archive_client(cid):
    db = get_db()
    client = db.execute("SELECT * FROM clients WHERE id=? AND biz_id=?", (cid, g.biz_id)).fetchone()
    if not client: return jsonify({'error': 'Not found'}), 404
    # Archive + revoke portal + opt out of SMS
    db.execute("UPDATE clients SET archived=1,archived_at=CURRENT_TIMESTAMP,sms_opt_out=1 WHERE id=?", (cid,))
    db.commit()
    print(f"\n  *** CLIENT ARCHIVED: {client['name']} (#{cid}) ***\n")
    return jsonify({'status': 'archived'})

@app.route('/api/admin/clients/<int:cid>/unarchive', methods=['POST'])
@require_admin
def a_unarchive_client(cid):
    db = get_db()
    client = db.execute("SELECT * FROM clients WHERE id=? AND biz_id=?", (cid, g.biz_id)).fetchone()
    if not client: return jsonify({'error': 'Not found'}), 404
    db.execute("UPDATE clients SET archived=0,archived_at=NULL,sms_opt_out=0 WHERE id=?", (cid,))
    db.commit()
    return jsonify({'status': 'unarchived'})

@app.route('/api/admin/clients/archived')
@require_admin
def a_archived_clients():
    db = get_db()
    rows = db.execute("""
        SELECT c.*, COUNT(s.id) as visit_count, COALESCE(SUM(s.price),0) as total_spent,
            MAX(s.date) as last_visit
        FROM clients c LEFT JOIN slots s ON s.client_id=c.id AND s.status='booked'
        WHERE c.biz_id=? AND COALESCE(c.archived,0)=1 AND COALESCE(c.status,'active')!='deleted'
        GROUP BY c.id ORDER BY c.archived_at DESC
    """, (g.biz_id,)).fetchall()
    return jsonify([dict(r) for r in rows])

# ═══════════════════════════════════════════════════
# ADMIN API — Reports (Phase 2 ready, Phase 1 stubs)
# ═══════════════════════════════════════════════════
@app.route('/api/admin/reports/<int:cid>', methods=['GET'])
@require_admin
def a_list_reports(cid):
    db = get_db()
    rows = db.execute("""
        SELECT r.id, r.slot_id, r.report_token, r.status, r.created_at, r.sent_at,
            r.practitioner_note, s.date, s.time
        FROM reports r LEFT JOIN slots s ON s.id=r.slot_id
        WHERE r.client_id=? AND r.biz_id=? ORDER BY r.created_at DESC
    """, (cid, g.biz_id)).fetchall()
    return jsonify([dict(r) for r in rows])

@app.route('/api/admin/reports/<int:cid>/create', methods=['POST'])
@require_admin
def a_create_report(cid):
    """Create a draft report for a client's most recent session.
    Phase 1: stores stub with practitioner note. Phase 2: generates full HTML via Claude API."""
    d = request.json or {}
    slot_id = d.get('slot_id')
    practitioner_note = (d.get('practitioner_note', '') or '').strip() or None
    session_data_json = d.get('session_data')  # optional — supply later when real data exists
    db = get_db()
    client = db.execute("SELECT * FROM clients WHERE id=? AND biz_id=?", (cid, g.biz_id)).fetchone()
    if not client: return jsonify({'error': 'Client not found'}), 404
    # If no slot_id provided, pick most recent booked session
    if not slot_id:
        r = db.execute("SELECT id FROM slots WHERE client_id=? AND biz_id=? AND status='booked' ORDER BY date DESC, time DESC LIMIT 1",
            (cid, g.biz_id)).fetchone()
        if r: slot_id = r['id']
    token = gen_token()
    # Generate HTML (Phase 2 plumbing — falls back to stub if no session_data)
    try:
        report_html = generate_report_html(client, slot_id, practitioner_note, session_data_json)
    except Exception as e:
        print(f"  Report generation error: {e}")
        report_html = _report_stub_html(client, slot_id, practitioner_note)
    db.execute("""INSERT INTO reports (biz_id,client_id,slot_id,report_html,report_token,
        practitioner_note,session_data,status) VALUES (?,?,?,?,?,?,?,'draft')""",
        (g.biz_id, cid, slot_id, report_html, token, practitioner_note,
         json.dumps(session_data_json) if session_data_json else None))
    rid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    db.commit()
    return jsonify({'status': 'created', 'report_id': rid, 'report_token': token,
        'preview_url': f"{BASE_URL}/report/{token}"})

@app.route('/api/admin/reports/<int:rid>/note', methods=['POST'])
@require_admin
def a_update_report_note(rid):
    note = ((request.json or {}).get('practitioner_note', '') or '').strip() or None
    db = get_db()
    r = db.execute("SELECT * FROM reports WHERE id=? AND biz_id=?", (rid, g.biz_id)).fetchone()
    if not r: return jsonify({'error': 'Not found'}), 404
    # Update note and regenerate HTML with new note baked in
    client = db.execute("SELECT * FROM clients WHERE id=?", (r['client_id'],)).fetchone()
    session_data = None
    if r['session_data']:
        try: session_data = json.loads(r['session_data'])
        except: pass
    try:
        report_html = generate_report_html(client, r['slot_id'], note, session_data)
    except Exception as e:
        report_html = _report_stub_html(client, r['slot_id'], note)
    db.execute("UPDATE reports SET practitioner_note=?, report_html=? WHERE id=?", (note, report_html, rid))
    db.commit()
    return jsonify({'status': 'updated'})

@app.route('/api/admin/reports/<int:rid>/send', methods=['POST'])
@require_admin
def a_send_report(rid):
    db = get_db()
    r = db.execute("SELECT * FROM reports WHERE id=? AND biz_id=?", (rid, g.biz_id)).fetchone()
    if not r: return jsonify({'error': 'Not found'}), 404
    link = f"{BASE_URL}/report/{r['report_token']}"
    client = db.execute("SELECT * FROM clients WHERE id=?", (r['client_id'],)).fetchone()
    if not client: return jsonify({'error': 'Client missing'}), 404
    # Send as a Nudge message
    db.execute("INSERT INTO messages (biz_id,client_id,sender,body) VALUES (?,?,'admin',?)",
        (g.biz_id, r['client_id'], f"Your session report is ready. View it here: {link}"))
    # Send SMS if client allows
    send_sms_to_client(r['client_id'],
        f"Hi {client['name'].split()[0]}, your session report from the Sol Standard is ready: {link}",
        biz_id=g.biz_id)
    db.execute("UPDATE reports SET status='sent', sent_at=CURRENT_TIMESTAMP WHERE id=?", (rid,))
    db.commit()
    return jsonify({'status': 'sent', 'link': link})

@app.route('/api/admin/reports/<int:rid>/delete', methods=['POST'])
@require_admin
def a_delete_report(rid):
    db = get_db()
    db.execute("DELETE FROM reports WHERE id=? AND biz_id=?", (rid, g.biz_id))
    db.commit()
    return jsonify({'status': 'deleted'})


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
        db.execute("UPDATE clients SET notes=?,status='consented' WHERE id=?", (notes, g.client_id))
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
        # Update client status to active
        db.execute("UPDATE clients SET status='active' WHERE id=? AND status!='active'", (g.client_id,))
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
            # Set client to active
            db.execute("UPDATE clients SET status='active' WHERE id=? AND status!='active'", (client_id,))
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
# DATABASE BACKUP
# ═══════════════════════════════════════════════════
BACKUP_DIR = os.path.join(DATA_DIR, 'backups')
os.makedirs(BACKUP_DIR, exist_ok=True)

def do_backup():
    """Create a timestamped backup of the database"""
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    backup_path = os.path.join(BACKUP_DIR, f'nudge_backup_{ts}.db')
    try:
        shutil.copy2(DB_PATH, backup_path)
        # Keep only last 30 backups
        backups = sorted([f for f in os.listdir(BACKUP_DIR) if f.startswith('nudge_backup_')])
        while len(backups) > 30:
            os.remove(os.path.join(BACKUP_DIR, backups.pop(0)))
        print(f"  ✓ Backup: {backup_path}")
        return backup_path
    except Exception as e:
        print(f"  ✗ Backup failed: {e}")
        return None

@app.route('/api/admin/backup')
@require_admin
def a_backup():
    path = do_backup()
    if not path: return jsonify({'error': 'Backup failed'}), 500
    return jsonify({'status': 'backed up', 'file': os.path.basename(path)})

@app.route('/api/admin/backup/download')
@require_admin
def a_backup_download():
    path = do_backup()
    if not path: return "Backup failed", 500
    return send_from_directory(BACKUP_DIR, os.path.basename(path), as_attachment=True)

@app.route('/api/admin/backup/list')
@require_admin
def a_backup_list():
    try:
        backups = sorted([f for f in os.listdir(BACKUP_DIR) if f.startswith('nudge_backup_')], reverse=True)
        return jsonify([{'file': f, 'size': os.path.getsize(os.path.join(BACKUP_DIR, f))} for f in backups[:10]])
    except: return jsonify([])

# Auto-backup on startup
_last_backup = 0
@app.before_request
def auto_backup():
    global _last_backup
    now = time.time()
    if now - _last_backup > 86400:  # Once per day
        _last_backup = now
        do_backup()


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
