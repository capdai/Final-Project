import re
import sqlite3
from datetime import datetime, timedelta

from flask import (Blueprint, render_template, request, redirect, url_for, session, flash, abort,
                   jsonify, send_file, g)
from werkzeug.security import generate_password_hash, check_password_hash

import core
import media
import pay as paylib
from core import (get_db, tx, login_required, safe_eq, change_status, occupy, slot_status, slots_needed,
                  parse_date_in_range, all_slots, deposit_info, get_settings, int_setting, barber_cards,
                  now_str, too_many, hit)

bp = Blueprint("main", __name__)


# ------------------------------------------------------------ หน้าแรก / บัญชี
@bp.route("/")
def index():
    db = get_db()
    services = db.execute("SELECT * FROM services WHERE active=1 ORDER BY sort_order, id").fetchall()
    return render_template("index.html", services=services, barbers=barber_cards(db))


@bp.route("/register", methods=["GET", "POST"])
def register():
    if g.user:
        return redirect(url_for("main.index"))
    if request.method == "POST":
        f = request.form
        username = f.get("username", "").strip()
        full_name = f.get("full_name", "").strip()
        phone = re.sub(r"[\s-]", "", f.get("phone", ""))
        pw, pw2 = f.get("password", ""), f.get("password2", "")
        errors = []
        if not re.fullmatch(r"[A-Za-z0-9_]{3,20}", username):
            errors.append("ชื่อผู้ใช้ต้องเป็น a-z, 0-9, _ ยาว 3-20 ตัวอักษร")
        if not full_name or len(full_name) > 60:
            errors.append("กรุณากรอกชื่อ-นามสกุล (ไม่เกิน 60 ตัวอักษร)")
        if not re.fullmatch(r"0\d{8,9}", phone):
            errors.append("เบอร์โทรไม่ถูกต้อง (เช่น 0812345678)")
        if len(pw) < 6:
            errors.append("รหัสผ่านต้องยาวอย่างน้อย 6 ตัวอักษร")
        if pw != pw2:
            errors.append("รหัสผ่านทั้งสองช่องไม่ตรงกัน")
        db = get_db()
        if not errors and db.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
            errors.append("ชื่อผู้ใช้นี้ถูกใช้แล้ว")
        if errors:
            for e in errors:
                flash(e, "err")
            return render_template("register.html", form=f)
        db.execute("INSERT INTO users (username,password_hash,full_name,phone,role) VALUES (?,?,?,?,'user')",
                   (username, generate_password_hash(pw), full_name, phone))
        db.commit()
        flash("สมัครสมาชิกสำเร็จ! เข้าสู่ระบบได้เลย", "ok")
        return redirect(url_for("main.login"))
    return render_template("register.html", form={})


@bp.route("/login", methods=["GET", "POST"])
def login():
    if g.user:
        return redirect(url_for("main.index"))
    if request.method == "POST":
        ip = request.remote_addr or "?"
        if too_many("login", ip, 5):
            flash("พยายามผิดหลายครั้งเกินไป รอ 1 นาทีแล้วลองใหม่", "err")
            return render_template("login.html")
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        u = get_db().execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        if u and check_password_hash(u["password_hash"], password):
            if not u["active"]:
                flash("บัญชีนี้ถูกปิดใช้งาน กรุณาติดต่อร้าน", "err")
                return render_template("login.html")
            session.clear()
            session["uid"] = u["id"]
            flash("ยินดีต้อนรับ %s!" % u["full_name"], "ok")
            if u["must_change_pw"]:
                return redirect(url_for("main.account_password"))
            if u["role"] == "barber":
                return redirect(url_for("barber.dashboard"))
            if u["role"] == "admin":
                return redirect(url_for("admin.dashboard"))
            return redirect(url_for("main.index"))
        hit("login", ip)
        flash("ข้อมูลเข้าสู่ระบบไม่ถูกต้อง", "err")
    return render_template("login.html")


@bp.route("/logout", methods=["POST"])
def logout():
    session.clear()
    flash("ออกจากระบบแล้ว แล้วพบกันใหม่", "ok")
    return redirect(url_for("main.index"))


@bp.route("/account/password", methods=["GET", "POST"])
@login_required
def account_password():
    if request.method == "POST":
        cur, new, new2 = (request.form.get(k, "") for k in ("current", "new", "new2"))
        u = g.user
        if not check_password_hash(u["password_hash"], cur):
            flash("รหัสผ่านปัจจุบันไม่ถูกต้อง", "err")
        elif len(new) < 6:
            flash("รหัสผ่านใหม่ต้องยาวอย่างน้อย 6 ตัวอักษร", "err")
        elif new != new2:
            flash("รหัสผ่านใหม่ทั้งสองช่องไม่ตรงกัน", "err")
        elif new == cur:
            flash("รหัสผ่านใหม่ต้องไม่ซ้ำกับรหัสเดิม", "err")
        else:
            db = get_db()
            db.execute("UPDATE users SET password_hash=?, must_change_pw=0 WHERE id=?",
                       (generate_password_hash(new), u["id"]))
            db.commit()
            flash("เปลี่ยนรหัสผ่านแล้ว", "ok")
            return redirect(url_for("barber.dashboard" if u["role"] == "barber" else "main.index"))
    return render_template("password.html")


@bp.route("/unlock", methods=["POST"])
def unlock():
    """เจ้าของกดรหัสลูกศรที่หน้าแรก -> เปิดโหมดแอดมิน (บัญชีอื่นกดรหัสถูกก็ไม่มีผล)"""
    ip = request.remote_addr or "?"
    if g.user is None or g.user["role"] != "owner" or too_many("unlock", ip, 30):
        return jsonify(ok=False), 403
    if not safe_eq(request.form.get("code", ""), core.ADMIN_CODE):
        hit("unlock", ip)
        return jsonify(ok=False)
    session["admin"] = True
    session["admin_at"] = int(__import__("time").time())
    return jsonify(ok=True, url=url_for("admin.dashboard"))


# ------------------------------------------------------------ ช่าง / โปรไฟล์ / รูป
@bp.route("/barbers")
def barbers():
    return render_template("barbers.html", barbers=barber_cards(get_db()))


@bp.route("/barbers/<int:bid>")
def barber_profile(bid):
    db = get_db()
    b = db.execute("SELECT * FROM barbers WHERE id=?", (bid,)).fetchone()
    if b is None or (not b["active"] and not g.is_admin):
        abort(404)
    stats = db.execute("""SELECT COUNT(*) AS done_cnt, COUNT(DISTINCT user_id) AS customers
                          FROM bookings WHERE barber_id=? AND status='done'""", (bid,)).fetchone()
    gallery = db.execute("SELECT * FROM barber_gallery WHERE barber_id=? ORDER BY id DESC", (bid,)).fetchall()
    services = db.execute("SELECT * FROM services WHERE active=1 ORDER BY sort_order, id").fetchall()
    return render_template("barber_profile.html", b=b, stats=stats, gallery=gallery, services=services)


def _send(subdir, name, max_age=0, private=True):
    p = media.path(subdir, name)
    if not p:
        abort(404)
    ext = p.rsplit(".", 1)[-1].lower()
    resp = send_file(p, mimetype=media.MIME.get(ext, "application/octet-stream"), max_age=max_age)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    if private:
        resp.headers["Cache-Control"] = "private, max-age=%d" % max_age
    return resp


@bp.route("/media/<name>")
def media_public(name):
    """รูปโปรไฟล์/ผลงานช่าง (เปิดสาธารณะ)"""
    return _send("barbers", name, max_age=86400, private=False)


@bp.route("/photo/<int:bid>")
@login_required
def booking_photo(bid):
    db = get_db()
    b = db.execute("SELECT b.photo, b.user_id, br.user_id AS barber_user FROM bookings b "
                   "JOIN barbers br ON br.id=b.barber_id WHERE b.id=?", (bid,)).fetchone()
    if b is None or not b["photo"]:
        abort(404)
    if not (g.is_admin or b["user_id"] == g.user["id"] or b["barber_user"] == g.user["id"]):
        abort(404)
    return _send("", b["photo"])


@bp.route("/slip/<int:bid>")
@login_required
def slip_image(bid):
    b = get_db().execute("SELECT slip_file, user_id FROM bookings WHERE id=?", (bid,)).fetchone()
    if b is None or not b["slip_file"] or not (g.is_admin or b["user_id"] == g.user["id"]):
        abort(404)
    return _send("slips", b["slip_file"])


@bp.route("/shop/qr")
@login_required
def shop_qr():
    name = get_settings(get_db()).get("qr_image")
    return _send("shop", name)


@bp.route("/hero-admin.webp")
@login_required
def hero_admin():
    if not g.is_admin:
        abort(404)
    p = __import__("os").path.join(core.BASE_DIR, "assets", "hero-admin.webp")
    resp = send_file(p, mimetype="image/webp", max_age=0)
    resp.headers["Cache-Control"] = "no-store"
    return resp


# ------------------------------------------------------------ จองคิว
@bp.route("/api/slots")
@login_required
def api_slots():
    db = get_db()
    d = parse_date_in_range(request.args.get("date", ""))
    try:
        barber_id = int(request.args.get("barber_id", ""))
        sid = int(request.args.get("service_id", "0") or 0)
    except ValueError:
        return jsonify([]), 400
    if d is None:
        return jsonify([]), 400
    svc = db.execute("SELECT duration_min FROM services WHERE id=?", (sid,)).fetchone()
    need = slots_needed(svc["duration_min"]) if svc else 1
    return jsonify(slot_status(db, barber_id, d, need))


@bp.route("/book", methods=["GET", "POST"])
@login_required
def book():
    db = get_db()
    today = datetime.now().date()
    services = db.execute("SELECT * FROM services WHERE active=1 ORDER BY sort_order, id").fetchall()
    barbers = barber_cards(db)
    dep_active, dep_amt = deposit_info(db)
    if request.method == "POST":
        f = request.form
        service = db.execute("SELECT * FROM services WHERE id=? AND active=1", (f.get("service_id", 0),)).fetchone()
        barber = db.execute("SELECT * FROM barbers WHERE id=? AND active=1 AND accepting=1",
                            (f.get("barber_id", 0),)).fetchone()
        d = parse_date_in_range(f.get("date", ""))
        t = f.get("time", "")
        note = f.get("note", "").strip()[:200]
        back = redirect(url_for("main.book", barber=f.get("barber_id", "")))
        if not barber:
            flash("กรุณาเลือกช่างก่อนจอง (ต้องระบุช่างทุกครั้ง)", "err")
            return back
        if not (service and d and t in all_slots()):
            flash("ข้อมูลการจองไม่ครบหรือไม่ถูกต้อง", "err")
            return back
        need = slots_needed(service["duration_min"])
        if not any(s["time"] == t and s["available"] for s in slot_status(db, barber["id"], d, need)):
            flash("ช่วงเวลานี้เต็มแล้วหรือผ่านไปแล้ว เลือกเวลาอื่นนะ", "err")
            return back
        if g.user["role"] != "owner":
            unpaid = db.execute("SELECT COUNT(*) FROM bookings WHERE user_id=? AND status='awaiting_payment'",
                                (g.user["id"],)).fetchone()[0]
            opened = db.execute("SELECT COUNT(*) FROM bookings WHERE user_id=? AND status IN "
                                "('awaiting_payment','checking','awaiting_barber','confirmed','in_progress')",
                                (g.user["id"],)).fetchone()[0]
            if unpaid >= core.MAX_UNPAID:
                flash("คุณมีคิวรอชำระมัดจำค้างอยู่ %d คิว กรุณาชำระหรือยกเลิกก่อนจองเพิ่ม" % unpaid, "err")
                return redirect(url_for("main.my_bookings"))
            if opened >= core.MAX_OPEN:
                flash("คุณมีคิวที่ยังไม่เสร็จครบ %d คิวแล้ว" % core.MAX_OPEN, "err")
                return redirect(url_for("main.my_bookings"))
        photo_name = None
        data, ext, err = media.read_upload(request.files.get("photo"))
        if err:
            flash(err, "err")
            return back
        if data:
            try:
                photo_name = media.save("", media.optimize(data), "webp")
            except ValueError as e:
                flash(str(e), "err")
                return back
        s = get_settings(db)
        price = int(service["price"])
        status = "awaiting_payment" if dep_active else "awaiting_barber"
        deposit = min(dep_amt, price) if dep_active else 0
        expires = None
        if dep_active:
            expires = (datetime.now() + timedelta(minutes=max(1, int_setting(s, "hold_minutes", 15)))
                       ).strftime("%Y-%m-%d %H:%M:%S")
        try:
            with tx(db):
                cur = db.execute(
                    """INSERT INTO bookings (user_id,barber_id,service_id,price,date,time,note,photo,status,
                       payment_status,duration_min,deposit_amount,pay_expires_at,queued_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (g.user["id"], barber["id"], service["id"], price, d.isoformat(), t, note, photo_name, status,
                     "unpaid" if dep_active else "none", service["duration_min"], deposit, expires,
                     None if dep_active else now_str()))
                new_id = cur.lastrowid
                occupy(db, new_id, barber["id"], d.isoformat(), t, need)
        except sqlite3.IntegrityError:
            media.remove("", photo_name)
            flash("มีคนจองช่วงเวลานี้ตัดหน้าไปแล้ว เลือกเวลาอื่นนะ", "err")
            return back
        if dep_active:
            flash("จองช่องเวลาไว้ให้แล้ว กรุณาชำระมัดจำภายในเวลาที่กำหนด", "ok")
            return redirect(url_for("main.pay", bid=new_id))
        flash("ส่งคำขอจองถึงช่างแล้ว รอช่างตอบรับ", "ok")
        return redirect(url_for("main.my_bookings"))
    pre = request.args.get("barber", type=int)
    return render_template("book.html", services=services, barbers=barbers, pre=pre,
                           min_date=today.isoformat(),
                           max_date=(today + timedelta(days=core.BOOK_AHEAD_DAYS)).isoformat(),
                           dep_active=dep_active, dep_amt=dep_amt,
                           refund_hours=int_setting(get_settings(db), "refund_hours", 3))


# ------------------------------------------------------------ ชำระมัดจำ
def _load_own_booking(db, bid):
    b = db.execute("""SELECT b.*, s.name AS service, br.name AS barber FROM bookings b
                      JOIN services s ON s.id=b.service_id JOIN barbers br ON br.id=b.barber_id
                      WHERE b.id=? AND b.user_id=?""", (bid, g.user["id"])).fetchone()
    if b is None:
        abort(404)
    return b


@bp.route("/pay/<int:bid>", methods=["GET", "POST"])
@login_required
def pay(bid):
    db = get_db()
    b = _load_own_booking(db, bid)
    if b["status"] not in ("awaiting_payment", "checking"):
        flash("คิวนี้ไม่ได้อยู่ในขั้นตอนชำระมัดจำ (สถานะ: %s)" % core.STATUS_TH[b["status"]], "err")
        return redirect(url_for("main.my_bookings"))
    s = get_settings(db)
    if request.method == "POST":
        if b["status"] != "awaiting_payment":
            flash("ส่งสลิปแล้ว กำลังรอร้านตรวจสอบ", "err")
            return redirect(url_for("main.pay", bid=bid))
        data, ext, err = media.read_upload(request.files.get("slip"))
        if err or not data:
            flash(err or "กรุณาแนบรูปสลิปโอนเงิน", "err")
            return redirect(url_for("main.pay", bid=bid))
        info = paylib.analyze_slip(data)
        if info["is_shop_qr"]:
            flash("รูปที่แนบเป็น QR รับเงินของร้าน ไม่ใช่สลิปโอนเงิน กรุณาแนบสลิปจากแอปธนาคารหลังโอน", "err")
            return redirect(url_for("main.pay", bid=bid))
        if db.execute("SELECT 1 FROM bookings WHERE slip_ref=? AND id!=?", (info["ref"], bid)).fetchone():
            flash("สลิปนี้เคยถูกใช้กับคิวอื่นแล้ว หากเป็นความเข้าใจผิดกรุณาติดต่อร้าน", "err")
            return redirect(url_for("main.pay", bid=bid))
        name = media.save("slips", data, ext)
        try:
            with tx(db):
                cur = db.execute("""UPDATE bookings SET status='checking', slip_file=?, slip_ref=?, slip_note=?,
                                    slip_reject_reason='' WHERE id=? AND status='awaiting_payment'""",
                                 (name, info["ref"], info["note"], bid))
                if cur.rowcount != 1:
                    raise sqlite3.IntegrityError("สถานะเปลี่ยนไปแล้ว")
        except sqlite3.IntegrityError:
            media.remove("slips", name)
            flash("ส่งสลิปไม่สำเร็จ (สลิปซ้ำหรือหมดเวลา) กรุณาลองใหม่หรือติดต่อร้าน", "err")
            return redirect(url_for("main.my_bookings"))
        flash("ส่งสลิปแล้ว รอร้านตรวจสอบสักครู่ (ดูสถานะได้ที่ \"คิวของฉัน\")", "ok")
        return redirect(url_for("main.my_bookings"))
    qr_svg, qr_mode = None, None
    if s.get("promptpay_id"):
        try:
            qr_svg = paylib.qr_svg_for(paylib.promptpay_payload(s["promptpay_id"], b["deposit_amount"]))
            qr_mode = "dynamic"
        except ValueError:
            qr_svg = None
    if qr_svg is None and s.get("qr_image"):
        qr_mode = "image"
    remaining = 0
    if b["status"] == "awaiting_payment" and b["pay_expires_at"]:
        remaining = max(0, int((datetime.strptime(b["pay_expires_at"], "%Y-%m-%d %H:%M:%S")
                                - datetime.now()).total_seconds()))
    return render_template("pay.html", b=b, qr_svg=qr_svg, qr_mode=qr_mode, remaining=remaining,
                           account_name=s.get("account_name", ""), refund_hours=int_setting(s, "refund_hours", 3))


# ------------------------------------------------------------ คิวของฉัน
@bp.route("/my")
@login_required
def my_bookings():
    db = get_db()
    rows = db.execute("""SELECT b.*, s.name AS service, br.name AS barber FROM bookings b
                         JOIN services s ON s.id=b.service_id JOIN barbers br ON br.id=b.barber_id
                         WHERE b.user_id=? ORDER BY b.date DESC, b.time DESC""", (g.user["id"],)).fetchall()
    return render_template("my.html", rows=rows,
                           refund_hours=int_setting(get_settings(db), "refund_hours", 3))


@bp.route("/cancel/<int:bid>", methods=["POST"])
@login_required
def cancel(bid):
    db = get_db()
    b = _load_own_booking(db, bid)
    if b["status"] == "checking":
        flash("คิวนี้กำลังรอร้านตรวจสลิป กรุณารอผลหรือติดต่อร้านเพื่อยกเลิก", "err")
        return redirect(url_for("main.my_bookings"))
    if b["status"] not in ("awaiting_payment", "awaiting_barber", "confirmed"):
        flash("ไม่สามารถยกเลิกคิวนี้ได้", "err")
        return redirect(url_for("main.my_bookings"))
    refund_hours = int_setting(get_settings(db), "refund_hours", 3)
    pay_override, msg = None, "ยกเลิกคิวแล้ว"
    if b["payment_status"] == "paid":
        if core.hours_until(b) >= refund_hours:
            pay_override, msg = "refund_due", "ยกเลิกคิวแล้ว ร้านจะติดต่อกลับทางเบอร์โทรเพื่อโอนคืนมัดจำ"
        else:
            pay_override, msg = "forfeited", "ยกเลิกคิวแล้ว (ยกเลิกใกล้เวลานัด จึงไม่คืนมัดจำตามเงื่อนไข)"
    try:
        with tx(db):
            change_status(db, b, "cancelled", pay=pay_override)
    except sqlite3.Error:
        flash("เกิดข้อผิดพลาด กรุณาลองใหม่", "err")
        return redirect(url_for("main.my_bookings"))
    flash(msg, "ok")
    return redirect(url_for("main.my_bookings"))
