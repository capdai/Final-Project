import os
import hmac
import time
import math
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, date, timedelta
from functools import wraps

from flask import g, session, request, abort, redirect, url_for, flash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("BARBER_DB", os.path.join(BASE_DIR, "barber.db"))
UPLOAD_DIR = os.environ.get("BARBER_UPLOADS", os.path.join(BASE_DIR, "uploads"))
ADMIN_CODE = os.environ.get("ADMIN_CODE", "UUDDLRLR")      # U=ขึ้น D=ลง L=ซ้าย R=ขวา
OWNER_USERNAME = os.environ.get("OWNER_USER", "kondanai")

OPEN_HOUR, CLOSE_HOUR, SLOT_MIN = 10, 20, 30
BOOK_AHEAD_DAYS = 30
ADMIN_IDLE_SEC = 3600          # โหมดแอดมินของเจ้าของหมดอายุเมื่อไม่ใช้งาน 1 ชั่วโมง
MAX_UNPAID = 2                 # ลูกค้าค้างรอชำระได้พร้อมกันไม่เกิน (กันกดจองกวน)
MAX_OPEN = 6                   # คิวที่ยังไม่เสร็จพร้อมกันไม่เกิน

STATUS_TH = {
    "awaiting_payment": "รอชำระมัดจำ", "checking": "รอตรวจสลิป", "awaiting_barber": "รอช่างตอบรับ",
    "confirmed": "ช่างรับงานแล้ว", "in_progress": "กำลังทำ", "done": "เสร็จสิ้น",
    "cancelled": "ยกเลิก", "declined": "ช่างปฏิเสธ", "expired": "หมดเวลาชำระ", "no_show": "ไม่มาตามนัด"}
PAY_TH = {"none": "ไม่มีมัดจำ", "unpaid": "ยังไม่ชำระ", "paid": "ชำระแล้ว",
          "refund_due": "รอคืนเงิน", "refunded": "คืนเงินแล้ว", "forfeited": "ริบมัดจำ"}
ACTIVE = ("awaiting_payment", "checking", "awaiting_barber", "confirmed", "in_progress", "done")
ROLE_TH = {"user": "ลูกค้า", "barber": "ช่าง", "admin": "แอดมินผู้ช่วย", "owner": "เจ้าของ"}


# ------------------------------------------------------------------ DB
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH, timeout=10)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


def close_db(_=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


@contextmanager
def tx(db):
    """ธุรกรรมแบบล็อกเขียน: สำเร็จ = commit, error = rollback แล้วโยน error ต่อ"""
    if db.in_transaction:
        db.commit()
    db.execute("BEGIN IMMEDIATE")
    try:
        yield
        db.commit()
    except BaseException:
        db.rollback()
        raise


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ------------------------------------------------------------------ ตั้งค่าร้าน
def get_settings(db):
    import db as dbmod
    s = dict(dbmod.SETTING_DEFAULTS)
    for r in db.execute("SELECT key,value FROM settings"):
        s[r["key"]] = r["value"]
    return s


def set_setting(db, key, value):
    db.execute("INSERT INTO settings (key,value) VALUES (?,?) "
               "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))


def int_setting(s, key, default):
    try:
        return max(0, int(s.get(key, default)))
    except (TypeError, ValueError):
        return default


def deposit_info(db):
    """คืน (เปิดใช้มัดจำไหม, จำนวนเงิน) — จะเปิดก็ต่อเมื่อตั้งยอด > 0 และตั้งค่า QR/พร้อมเพย์แล้ว"""
    s = get_settings(db)
    amt = int_setting(s, "deposit_amount", 50)
    return (amt > 0 and bool(s.get("promptpay_id") or s.get("qr_image"))), amt


# ------------------------------------------------------------------ ช่องเวลา
def all_slots():
    t = datetime(2000, 1, 1, OPEN_HOUR, 0)
    end = datetime(2000, 1, 1, CLOSE_HOUR, 0)
    out = []
    while t < end:
        out.append(t.strftime("%H:%M"))
        t += timedelta(minutes=SLOT_MIN)
    return out


def slots_needed(duration_min):
    return max(1, math.ceil(int(duration_min or SLOT_MIN) / SLOT_MIN))


def parse_date_in_range(s):
    try:
        d = datetime.strptime(s, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None
    today = date.today()
    if d < today or d > today + timedelta(days=BOOK_AHEAD_DAYS):
        return None
    return d


def slot_status(db, barber_id, d, need=1, exclude_id=None, admin=False):
    """รายการช่องเวลาของช่างในวันนั้น [{time, available}]
    available = ช่องว่างต่อเนื่องครบตามระยะเวลาบริการ, ไม่ผ่านเวลาไปแล้ว, ช่างไม่ลา และเปิดรับคิว"""
    slots = all_slots()
    if not admin:
        b = db.execute("SELECT active, accepting FROM barbers WHERE id=?", (barber_id,)).fetchone()
        off = db.execute("SELECT 1 FROM barber_dayoff WHERE barber_id=? AND date=?",
                         (barber_id, d.isoformat())).fetchone()
        if not b or not b["active"] or not b["accepting"] or off:
            return [{"time": s, "available": False} for s in slots]
    taken = {r[0] for r in db.execute(
        "SELECT time FROM booking_slots WHERE barber_id=? AND date=? AND booking_id!=?",
        (barber_id, d.isoformat(), exclude_id or 0))}
    now = datetime.now()
    out = []
    for i, s in enumerate(slots):
        ok = i + need <= len(slots) and all(slots[i + k] not in taken for k in range(need))
        if not admin and d == now.date() and s <= now.strftime("%H:%M"):
            ok = False
        out.append({"time": s, "available": ok})
    return out


def occupy(db, bid, barber_id, d, t, need):
    slots = all_slots()
    i = slots.index(t)
    if i + need > len(slots):
        raise sqlite3.IntegrityError("เกินเวลาปิดร้าน")
    for k in range(need):
        db.execute("INSERT INTO booking_slots (booking_id,barber_id,date,time) VALUES (?,?,?,?)",
                   (bid, barber_id, d, slots[i + k]))


def release(db, bid):
    db.execute("DELETE FROM booking_slots WHERE booking_id=?", (bid,))


def expire_stale(db):
    """คิวที่รอชำระเกินเวลา -> หมดเวลา และคืนช่องเวลา (เรียกอัตโนมัติทุกคำขอ ไม่ต้องมีตัวตั้งเวลา)"""
    ids = [r[0] for r in db.execute(
        "SELECT id FROM bookings WHERE status='awaiting_payment' AND pay_expires_at IS NOT NULL "
        "AND pay_expires_at < ?", (now_str(),))]
    if not ids:
        return
    try:
        with tx(db):
            for i in ids:
                db.execute("UPDATE bookings SET status='expired' WHERE id=? AND status='awaiting_payment'", (i,))
                release(db, i)
    except sqlite3.OperationalError:
        db.rollback()


def change_status(db, b, new, reason="", pay=None):
    """เปลี่ยนสถานะคิวพร้อมผลข้างเคียง (ช่องเวลา/มัดจำ) — ต้องเรียกภายใน tx(db)
    ถ้าฟื้นคิวแล้วเวลาชนกับคนอื่นจะเกิด sqlite3.IntegrityError"""
    old, ps = b["status"], b["payment_status"]
    if new in ACTIVE and old not in ACTIVE:
        occupy(db, b["id"], b["barber_id"], b["date"], b["time"], slots_needed(b["duration_min"]))
    elif new not in ACTIVE and old in ACTIVE:
        release(db, b["id"])
    if new in ("cancelled", "declined") and ps == "paid":
        ps = "refund_due"
    elif new == "no_show" and ps == "paid":
        ps = "forfeited"
    elif new == "awaiting_barber" and ps == "unpaid":
        ps = "paid"
    if pay:
        ps = pay
    sets, args = ["status=?", "payment_status=?"], [new, ps]
    if new == "awaiting_barber":
        sets.append("queued_at=?")
        args.append(now_str())
    if new in ("confirmed", "declined"):
        sets.append("responded_at=?")
        args.append(now_str())
    if new == "declined":
        sets.append("decline_reason=?")
        args.append(reason[:200])
    args.append(b["id"])
    db.execute("UPDATE bookings SET %s WHERE id=?" % ",".join(sets), args)
    return ps


def hours_until(b):
    try:
        appt = datetime.strptime("%s %s" % (b["date"], b["time"]), "%Y-%m-%d %H:%M")
    except ValueError:
        return 0
    return (appt - datetime.now()).total_seconds() / 3600


def barber_cards(db, only_active=True):
    """ช่างพร้อมสถิติที่เปิดเผยได้ ใช้ในหน้าแรก/หน้าเลือกช่าง"""
    return db.execute("""
        SELECT br.*, (SELECT COUNT(*) FROM bookings b WHERE b.barber_id=br.id AND b.status='done') AS done_cnt
        FROM barbers br %s ORDER BY br.id""" % ("WHERE br.active=1" if only_active else "")).fetchall()


# ------------------------------------------------------------------ ความปลอดภัย
def safe_eq(a, b):
    return hmac.compare_digest(str(a).encode("utf-8"), str(b).encode("utf-8"))


def csrf_token():
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_hex(16)
    return session["_csrf"]


_hits = {}


def too_many(bucket, ip, limit, window=60):
    now = time.time()
    k = (bucket, ip)
    _hits[k] = [t for t in _hits.get(k, []) if now - t < window]
    return len(_hits[k]) >= limit


def hit(bucket, ip):
    _hits.setdefault((bucket, ip), []).append(time.time())


def random_password(n=10):
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"
    return "".join(secrets.choice(alphabet) for _ in range(n))


# ------------------------------------------------------------------ ผู้ใช้/สิทธิ์
def load_user():
    g.user = None
    g.is_owner_mode = g.is_assistant = g.is_admin = False
    uid = session.get("uid")
    if uid:
        u = get_db().execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        if u is None or not u["active"]:
            session.clear()
        else:
            g.user = u
            if u["role"] == "owner" and session.get("admin"):
                if time.time() - session.get("admin_at", 0) > ADMIN_IDLE_SEC:
                    session.pop("admin", None)
                    session.pop("admin_at", None)
                else:
                    g.is_owner_mode = True
                    session["admin_at"] = int(time.time())
            elif u["role"] != "owner":
                session.pop("admin", None)
            g.is_assistant = u["role"] == "admin"
            g.is_admin = g.is_owner_mode or g.is_assistant


def login_required(f):
    @wraps(f)
    def wrapper(*a, **kw):
        if g.user is None:
            flash("กรุณาเข้าสู่ระบบก่อน", "err")
            return redirect(url_for("main.login"))
        return f(*a, **kw)
    return wrapper


def staff_required(f):
    @wraps(f)
    @login_required
    def wrapper(*a, **kw):
        if not g.is_admin:
            abort(404)
        return f(*a, **kw)
    return wrapper


def owner_required(f):
    @wraps(f)
    @login_required
    def wrapper(*a, **kw):
        if not g.is_owner_mode:
            abort(404)
        return f(*a, **kw)
    return wrapper


def barber_required(f):
    @wraps(f)
    @login_required
    def wrapper(*a, **kw):
        if g.user["role"] != "barber":
            abort(404)
        return f(*a, **kw)
    return wrapper


def my_barber(db):
    b = db.execute("SELECT * FROM barbers WHERE user_id=?", (g.user["id"],)).fetchone()
    if b is None:
        abort(404)
    return b


def nav_counts(db):
    """ตัวเลขแจ้งเตือนบนเมนู"""
    c = {}
    if g.get("is_admin"):
        mins = int_setting(get_settings(db), "respond_minutes", 60)
        limit = (datetime.now() - timedelta(minutes=mins)).strftime("%Y-%m-%d %H:%M:%S")
        c["slips"] = db.execute("SELECT COUNT(*) FROM bookings WHERE status='checking'").fetchone()[0]
        c["refunds"] = db.execute("SELECT COUNT(*) FROM bookings WHERE payment_status='refund_due'").fetchone()[0]
        c["overdue"] = db.execute("SELECT COUNT(*) FROM bookings WHERE status='awaiting_barber' AND queued_at < ?",
                                  (limit,)).fetchone()[0]
    if g.get("user") is not None and g.user["role"] == "barber":
        br = db.execute("SELECT id FROM barbers WHERE user_id=?", (g.user["id"],)).fetchone()
        c["jobs"] = db.execute("SELECT COUNT(*) FROM bookings WHERE barber_id=? AND status='awaiting_barber'",
                               (br["id"],)).fetchone()[0] if br else 0
    return c


def init_app(app):
    from werkzeug.exceptions import HTTPException  # noqa: F401

    app.teardown_appcontext(close_db)

    @app.before_request
    def _hook():
        if request.endpoint == "static":
            return
        db = get_db()
        expire_stale(db)
        load_user()
        if request.method == "POST":
            if not safe_eq(request.form.get("_csrf", ""), session.get("_csrf", "")):
                abort(400, "CSRF token ไม่ถูกต้อง กรุณารีเฟรชหน้าแล้วลองใหม่")
        if (g.user is not None and g.user["must_change_pw"]
                and request.endpoint not in ("main.account_password", "main.logout")):
            return redirect(url_for("main.account_password"))

    @app.context_processor
    def _ctx():
        d = {"csrf_token": csrf_token, "STATUS_TH": STATUS_TH, "PAY_TH": PAY_TH, "ROLE_TH": ROLE_TH,
             "ASSET_V": app.config.get("ASSET_V", "1"), "ADMIN_LEN": len(ADMIN_CODE)}
        if g.get("user") is not None:
            d["counts"] = nav_counts(get_db())
        return d

    @app.template_filter("dmy")
    def dmy(value):
        try:
            return datetime.strptime(value, "%Y-%m-%d").strftime("%d/%m/%Y")
        except (TypeError, ValueError):
            return value or ""

    @app.template_filter("baht")
    def baht(v):
        try:
            return "{:,}".format(int(round(float(v))))
        except (TypeError, ValueError):
            return "0"

    @app.template_filter("dt")
    def dt(value):
        try:
            return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").strftime("%d/%m %H:%M")
        except (TypeError, ValueError):
            return value or ""

    @app.template_filter("endtime")
    def endtime(b):
        try:
            t = datetime.strptime(b["time"], "%H:%M") + timedelta(minutes=int(b["duration_min"]))
            return t.strftime("%H:%M")
        except Exception:
            return ""
