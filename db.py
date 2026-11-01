import os
import shutil
import sqlite3
from datetime import datetime

SCHEMA_VERSION = 2

STATUSES = ("awaiting_payment", "checking", "awaiting_barber", "confirmed", "in_progress",
            "done", "cancelled", "declined", "expired", "no_show")
PAY_STATUSES = ("none", "unpaid", "paid", "refund_due", "refunded", "forfeited")


def _q(vals):
    return ",".join("'%s'" % v for v in vals)


USERS_SQL = """CREATE TABLE {name} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    full_name TEXT NOT NULL,
    phone TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL DEFAULT 'user' CHECK (role IN ('user','barber','admin','owner')),
    active INTEGER NOT NULL DEFAULT 1,
    must_change_pw INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
)"""

BOOKINGS_SQL = """CREATE TABLE {name} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    barber_id INTEGER NOT NULL REFERENCES barbers(id),
    service_id INTEGER NOT NULL REFERENCES services(id),
    price INTEGER NOT NULL,
    date TEXT NOT NULL,
    time TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    photo TEXT,
    status TEXT NOT NULL DEFAULT 'awaiting_barber' CHECK (status IN (%s)),
    payment_status TEXT NOT NULL DEFAULT 'none' CHECK (payment_status IN (%s)),
    duration_min INTEGER NOT NULL DEFAULT 30,
    deposit_amount INTEGER NOT NULL DEFAULT 0,
    pay_expires_at TEXT,
    slip_file TEXT,
    slip_ref TEXT,
    slip_note TEXT NOT NULL DEFAULT '',
    slip_reject_reason TEXT NOT NULL DEFAULT '',
    queued_at TEXT,
    decline_reason TEXT NOT NULL DEFAULT '',
    responded_at TEXT,
    refunded_at TEXT,
    refund_note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
)""" % (_q(STATUSES), _q(PAY_STATUSES))

BARBERS_SQL = """CREATE TABLE IF NOT EXISTS barbers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    specialty TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1,
    user_id INTEGER REFERENCES users(id),
    bio TEXT NOT NULL DEFAULT '',
    experience_years INTEGER NOT NULL DEFAULT 0,
    photo TEXT,
    accepting INTEGER NOT NULL DEFAULT 1,
    commission_pct INTEGER NOT NULL DEFAULT 0
)"""

SERVICES_SQL = """CREATE TABLE IF NOT EXISTS services (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    price INTEGER NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    duration_min INTEGER NOT NULL DEFAULT 30,
    description TEXT NOT NULL DEFAULT '',
    sort_order INTEGER NOT NULL DEFAULT 0
)"""

OTHER_SQL = [
    """CREATE TABLE IF NOT EXISTS booking_slots (
        booking_id INTEGER NOT NULL REFERENCES bookings(id) ON DELETE CASCADE,
        barber_id INTEGER NOT NULL,
        date TEXT NOT NULL,
        time TEXT NOT NULL,
        UNIQUE (barber_id, date, time)
    )""",
    """CREATE TABLE IF NOT EXISTS barber_dayoff (
        barber_id INTEGER NOT NULL REFERENCES barbers(id) ON DELETE CASCADE,
        date TEXT NOT NULL,
        PRIMARY KEY (barber_id, date)
    )""",
    """CREATE TABLE IF NOT EXISTS barber_gallery (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        barber_id INTEGER NOT NULL REFERENCES barbers(id) ON DELETE CASCADE,
        filename TEXT NOT NULL,
        caption TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
    )""",
    """CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL DEFAULT ''
    )""",
]

INDEX_SQL = [
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_barbers_user ON barbers(user_id) WHERE user_id IS NOT NULL",
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_bookings_slip_ref ON bookings(slip_ref) WHERE slip_ref IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS ix_bookings_date ON bookings(date)",
    "CREATE INDEX IF NOT EXISTS ix_bookings_status ON bookings(status)",
    "CREATE INDEX IF NOT EXISTS ix_bookings_barber ON bookings(barber_id, date)",
    "CREATE INDEX IF NOT EXISTS ix_bookings_user ON bookings(user_id)",
]

SETTING_DEFAULTS = {
    "deposit_amount": "50",      # ค่ามัดจำ (บาท) 0 = ไม่เก็บมัดจำ
    "hold_minutes": "15",        # กันคิวไว้กี่นาทีระหว่างรอชำระ
    "refund_hours": "3",         # ยกเลิกก่อนนัดอย่างน้อยกี่ชั่วโมงถึงได้คืนมัดจำ
    "respond_minutes": "60",     # ช่างต้องตอบรับภายในกี่นาที (เกินแล้วเตือนเจ้าของ)
    "promptpay_id": "",          # เบอร์/เลขพร้อมเพย์ของร้าน
    "account_name": "",          # ชื่อบัญชีที่แสดงให้ลูกค้าตรวจสอบ
    "qr_image": "",              # ไฟล์รูป QR ที่เจ้าของอัปโหลด (ไว้ใช้เมื่ออ่านเลขพร้อมเพย์ไม่ได้)
}

DEFAULT_SERVICES = [
    ("ตัดผมชาย", 250, 30), ("ตัดกันข้าง (Fade)", 300, 60), ("โกนหนวด-เคราสุดคม", 150, 30),
    ("สระ + ไดร์จัดทรง", 120, 30), ("ทำสีผม", 900, 120)]
DEFAULT_BARBERS = [
    ("เร็น (Joker)", "ทรงคลาสสิก / ตัดกรรไกร"),
    ("ริว (Skull)", "Fade / Undercut"),
    ("แอน (Panther)", "ทำสี / ทรงสมัยใหม่")]
# ระยะเวลาเริ่มต้นสำหรับบริการเดิมที่ชื่อตรงกัน (ใช้ตอนย้ายข้อมูลเท่านั้น เจ้าของแก้ได้ในหน้าบริการ)
LEGACY_DURATIONS = {"ตัดกันข้าง (Fade)": 60, "ทำสีผม": 120}
ACTIVE_SLOT_STATUSES = ("awaiting_payment", "checking", "awaiting_barber", "confirmed", "in_progress", "done")


def _columns(con, table):
    return {r[1] for r in con.execute("PRAGMA table_info(%s)" % table)}


def _add_columns(con, table, defs):
    have = _columns(con, table)
    for name, ddl in defs:
        if name not in have:
            con.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, name, ddl))


def _create_rest(con):
    con.execute(BARBERS_SQL)
    con.execute(SERVICES_SQL)
    for s in OTHER_SQL:
        con.execute(s)
    for s in INDEX_SQL:
        con.execute(s)


def _seed(con, fresh):
    if not con.execute("SELECT 1 FROM services").fetchone():
        con.executemany("INSERT INTO services (name,price,duration_min,sort_order) VALUES (?,?,?,?)",
                        [(n, p, d, i) for i, (n, p, d) in enumerate(DEFAULT_SERVICES)])
    if not con.execute("SELECT 1 FROM barbers").fetchone():
        con.executemany("INSERT INTO barbers (name,specialty) VALUES (?,?)", DEFAULT_BARBERS)
    for k, v in SETTING_DEFAULTS.items():
        con.execute("INSERT OR IGNORE INTO settings (key,value) VALUES (?,?)", (k, v))


def migrate_legacy(con, path, log):
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    bdir = os.path.join(os.path.dirname(os.path.abspath(path)), "backups")
    os.makedirs(bdir, exist_ok=True)
    bpath = os.path.join(bdir, "barber-before-v2-%s.db" % ts)
    shutil.copy2(path, bpath)
    log("[ย้ายข้อมูล] สำรองฐานข้อมูลเดิมไว้ที่ %s" % bpath)
    con.isolation_level = None
    con.execute("PRAGMA foreign_keys=OFF")
    con.execute("BEGIN")
    try:
        bcols = _columns(con, "bookings")
        con.execute(USERS_SQL.format(name="users_new"))
        con.execute("""INSERT INTO users_new (id,username,password_hash,full_name,phone,role,active,created_at)
                       SELECT id,username,password_hash,full_name,phone,role,
                              CASE WHEN role='admin' THEN 0 ELSE 1 END, created_at FROM users""")
        con.execute("DROP TABLE users")
        con.execute("ALTER TABLE users_new RENAME TO users")

        con.execute(BOOKINGS_SQL.format(name="bookings_new"))
        photo = "photo" if "photo" in bcols else "NULL"
        con.execute("""INSERT INTO bookings_new
              (id,user_id,barber_id,service_id,price,date,time,note,photo,status,payment_status,
               duration_min,deposit_amount,queued_at,created_at)
            SELECT id,user_id,barber_id,service_id,price,date,time,note,%s,
              CASE status WHEN 'pending' THEN
                   CASE WHEN date >= date('now','localtime') THEN 'awaiting_barber' ELSE 'cancelled' END
                   ELSE status END,
              'none',30,0,created_at,created_at FROM bookings""" % photo)
        con.execute("DROP TABLE bookings")
        con.execute("ALTER TABLE bookings_new RENAME TO bookings")

        _add_columns(con, "barbers", [
            ("user_id", "INTEGER REFERENCES users(id)"), ("bio", "TEXT NOT NULL DEFAULT ''"),
            ("experience_years", "INTEGER NOT NULL DEFAULT 0"), ("photo", "TEXT"),
            ("accepting", "INTEGER NOT NULL DEFAULT 1"), ("commission_pct", "INTEGER NOT NULL DEFAULT 0")])
        _add_columns(con, "services", [
            ("duration_min", "INTEGER NOT NULL DEFAULT 30"), ("description", "TEXT NOT NULL DEFAULT ''"),
            ("sort_order", "INTEGER NOT NULL DEFAULT 0")])
        _create_rest(con)
        for name, mins in LEGACY_DURATIONS.items():
            con.execute("UPDATE services SET duration_min=? WHERE name=? AND duration_min=30", (mins, name))
        con.execute("""INSERT OR IGNORE INTO booking_slots (booking_id,barber_id,date,time)
                       SELECT id,barber_id,date,time FROM bookings WHERE status IN (%s)""" % _q(ACTIVE_SLOT_STATUSES))
        _seed(con, False)
        con.execute("PRAGMA user_version=%d" % SCHEMA_VERSION)
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    finally:
        con.execute("PRAGMA foreign_keys=ON")
    bad = con.execute("PRAGMA foreign_key_check").fetchall()
    if bad:
        log("[ย้ายข้อมูล] คำเตือน: พบข้อมูลอ้างอิงไม่ตรงกัน %d รายการ" % len(bad))
    log("[ย้ายข้อมูล] เสร็จแล้ว (โครงสร้างเวอร์ชัน %d)" % SCHEMA_VERSION)


def ensure_owner(con, username):
    """คืน 'exists' | 'promoted' | 'missing'"""
    if con.execute("SELECT 1 FROM users WHERE role='owner' AND active=1").fetchone():
        return "exists"
    u = con.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
    if not u:
        return "missing"
    con.execute("UPDATE users SET role='owner', active=1, must_change_pw=0 WHERE id=?", (u[0],))
    con.commit()
    return "promoted"


def init_db(path, owner_username="kondanai", log=print):
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    con = sqlite3.connect(path)
    try:
        ver = con.execute("PRAGMA user_version").fetchone()[0]
        has_users = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='users'").fetchone()
        if has_users and ver < 2:
            migrate_legacy(con, path, log)
            con.isolation_level = ""
        else:
            con.execute("PRAGMA foreign_keys=ON")
            if not has_users:
                con.execute(USERS_SQL.format(name="users"))
                con.execute(BOOKINGS_SQL.format(name="bookings"))
            _create_rest(con)
            _seed(con, not has_users)
            con.execute("PRAGMA user_version=%d" % SCHEMA_VERSION)
            con.commit()
        status = ensure_owner(con, owner_username)
        if status == "promoted":
            log("[เจ้าของ] ตั้งบัญชี '%s' เป็นเจ้าของระบบแล้ว" % owner_username)
        elif status == "missing":
            log("[เจ้าของ] ยังไม่มีบัญชีเจ้าของ: สมัครสมาชิกชื่อ '%s' ที่หน้าเว็บ แล้วรัน  python app.py make-owner %s"
                % (owner_username, owner_username))
        con.commit()
    finally:
        con.close()
