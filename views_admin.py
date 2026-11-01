import csv
import io
import math
import re
import sqlite3
from datetime import datetime, date, timedelta

from flask import (Blueprint, render_template, request, redirect, url_for, flash, abort, jsonify,
                   session, g, Response)
from werkzeug.security import generate_password_hash

import core
import media
import pay as paylib
import views_barber as vb
from core import (get_db, tx, staff_required, owner_required, change_status, occupy, release, all_slots,
                  slots_needed, slot_status, get_settings, set_setting, int_setting, now_str, random_password,
                  deposit_info)

bp = Blueprint("admin", __name__, url_prefix="/admin")
MANUAL_STATUSES = ("awaiting_barber", "confirmed", "in_progress", "done", "cancelled", "declined", "no_show")


def _back(default="admin.dashboard"):
    return redirect(request.referrer or url_for(default))


# ------------------------------------------------------------ แดชบอร์ดคิว
@bp.route("/")
@staff_required
def dashboard():
    db = get_db()
    today = date.today().isoformat()
    fdate = request.args.get("date", "").strip()
    fstatus = request.args.get("status", "").strip()
    fbarber = request.args.get("barber", type=int)
    sql = """SELECT b.*, u.full_name, u.phone, s.name AS service, br.name AS barber
             FROM bookings b JOIN users u ON u.id=b.user_id
             JOIN services s ON s.id=b.service_id JOIN barbers br ON br.id=b.barber_id WHERE 1=1"""
    args = []
    if fdate:
        sql += " AND b.date=?"
        args.append(fdate)
    if fstatus in core.STATUS_TH:
        sql += " AND b.status=?"
        args.append(fstatus)
    if fbarber:
        sql += " AND b.barber_id=?"
        args.append(fbarber)
    sql += " ORDER BY b.date DESC, b.time ASC LIMIT 300"
    stats = {
        "today": db.execute("SELECT COUNT(*) FROM bookings WHERE date=? AND status IN (%s)" %
                            ",".join("'%s'" % s for s in core.ACTIVE), (today,)).fetchone()[0],
        "waiting": db.execute("SELECT COUNT(*) FROM bookings WHERE status='awaiting_barber'").fetchone()[0],
        "users": db.execute("SELECT COUNT(*) FROM users WHERE role='user'").fetchone()[0],
        "revenue": db.execute("SELECT COALESCE(SUM(price),0) FROM bookings WHERE status='done' AND date=?",
                              (today,)).fetchone()[0],
    }
    limit = (datetime.now() - timedelta(minutes=int_setting(get_settings(db), "respond_minutes", 60))
             ).strftime("%Y-%m-%d %H:%M:%S")
    dep_active, dep_amt = deposit_info(db)
    return render_template(
        "admin_dashboard.html", rows=db.execute(sql, args).fetchall(), stats=stats, fdate=fdate,
        fstatus=fstatus, fbarber=fbarber, overdue_limit=limit, dep_active=dep_active, dep_amt=dep_amt,
        barbers=db.execute("SELECT id,name FROM barbers ORDER BY id").fetchall(),
        manual=[(k, core.STATUS_TH[k]) for k in MANUAL_STATUSES])


@bp.route("/booking/<int:bid>/status", methods=["POST"])
@staff_required
def booking_status(bid):
    db = get_db()
    new = request.form.get("status", "")
    if new not in MANUAL_STATUSES:
        abort(400)
    b = db.execute("SELECT * FROM bookings WHERE id=?", (bid,)).fetchone()
    if b is None:
        abort(404)
    try:
        with tx(db):
            change_status(db, b, new, reason=request.form.get("reason", "").strip() or "ปฏิเสธโดยร้าน")
        flash("อัปเดตคิว #%d เป็น \"%s\" แล้ว" % (bid, core.STATUS_TH[new]), "ok")
    except sqlite3.IntegrityError:
        flash("ฟื้นคิวนี้ไม่ได้ เพราะช่วงเวลานั้นถูกจองโดยคนอื่นแล้ว", "err")
    return _back()


@bp.route("/booking/<int:bid>/edit", methods=["GET", "POST"])
@staff_required
def booking_edit(bid):
    db = get_db()
    b = db.execute("""SELECT b.*, u.full_name, u.phone FROM bookings b JOIN users u ON u.id=b.user_id
                      WHERE b.id=?""", (bid,)).fetchone()
    if b is None:
        abort(404)
    if request.method == "POST":
        f = request.form
        service = db.execute("SELECT * FROM services WHERE id=?", (f.get("service_id", 0),)).fetchone()
        barber = db.execute("SELECT * FROM barbers WHERE id=?", (f.get("barber_id", 0),)).fetchone()
        try:
            d = datetime.strptime(f.get("date", ""), "%Y-%m-%d").date()
        except ValueError:
            d = None
        t = f.get("time", "")
        try:
            price = int(f.get("price", ""))
        except ValueError:
            price = -1
        if not (service and barber and d and t in all_slots() and price >= 0):
            flash("ข้อมูลไม่ครบหรือไม่ถูกต้อง", "err")
            return redirect(url_for("admin.booking_edit", bid=bid))
        duration = service["duration_min"] if service["id"] != b["service_id"] else b["duration_min"]
        try:
            with tx(db):
                if b["status"] in core.ACTIVE:
                    release(db, bid)
                db.execute("""UPDATE bookings SET service_id=?, barber_id=?, date=?, time=?, note=?, price=?,
                              duration_min=? WHERE id=?""",
                           (service["id"], barber["id"], d.isoformat(), t, f.get("note", "").strip()[:200],
                            price, duration, bid))
                if b["status"] in core.ACTIVE:
                    occupy(db, bid, barber["id"], d.isoformat(), t, slots_needed(duration))
            flash("แก้ไขคิว #%d เรียบร้อย" % bid, "ok")
            return redirect(url_for("admin.dashboard"))
        except sqlite3.IntegrityError:
            flash("ช่างคนนี้มีคิวชนในวัน/เวลานั้น (หรือเกินเวลาปิดร้าน) เลือกเวลาอื่น", "err")
        return redirect(url_for("admin.booking_edit", bid=bid))
    return render_template(
        "admin_edit.html", b=b,
        services=db.execute("SELECT * FROM services WHERE active=1 OR id=? ORDER BY sort_order,id",
                            (b["service_id"],)).fetchall(),
        barbers=db.execute("SELECT * FROM barbers WHERE active=1 OR id=? ORDER BY id", (b["barber_id"],)).fetchall())


@bp.route("/api/slots")
@staff_required
def api_slots():
    db = get_db()
    try:
        barber_id = int(request.args.get("barber_id", ""))
        sid = int(request.args.get("service_id", "0") or 0)
        exclude = int(request.args.get("exclude", "0") or 0)
        d = datetime.strptime(request.args.get("date", ""), "%Y-%m-%d").date()
    except ValueError:
        return jsonify([]), 400
    svc = db.execute("SELECT duration_min FROM services WHERE id=?", (sid,)).fetchone()
    ex = db.execute("SELECT duration_min, service_id FROM bookings WHERE id=?", (exclude,)).fetchone()
    need = slots_needed(svc["duration_min"]) if svc else 1
    return jsonify(slot_status(db, barber_id, d, need, exclude_id=exclude, admin=True))


@bp.route("/booking/<int:bid>/delete", methods=["POST"])
@owner_required
def booking_delete(bid):
    db = get_db()
    b = db.execute("SELECT photo, slip_file FROM bookings WHERE id=?", (bid,)).fetchone()
    if b:
        media.remove("", b["photo"])
        media.remove("slips", b["slip_file"])
        db.execute("DELETE FROM bookings WHERE id=?", (bid,))
        db.commit()
        flash("ลบคิว #%d แล้ว" % bid, "ok")
    return _back()


# ------------------------------------------------------------ ตรวจสลิป / คืนเงิน
@bp.route("/slips")
@staff_required
def slips():
    db = get_db()
    rows = db.execute("""SELECT b.*, u.full_name, u.phone, s.name AS service, br.name AS barber
                         FROM bookings b JOIN users u ON u.id=b.user_id JOIN services s ON s.id=b.service_id
                         JOIN barbers br ON br.id=b.barber_id WHERE b.status='checking' ORDER BY b.created_at""").fetchall()
    return render_template("admin_slips.html", rows=rows, scanner=paylib.scanner_available())


@bp.route("/slips/<int:bid>/approve", methods=["POST"])
@staff_required
def slip_approve(bid):
    db = get_db()
    b = db.execute("SELECT * FROM bookings WHERE id=? AND status='checking'", (bid,)).fetchone()
    if b is None:
        flash("ไม่พบคิวที่รอตรวจสลิป (อาจมีคนตรวจไปแล้ว)", "err")
        return _back("admin.slips")
    try:
        with tx(db):
            change_status(db, b, "awaiting_barber")
        flash("อนุมัติสลิปคิว #%d แล้ว — ส่งต่อให้ช่างตอบรับ" % bid, "ok")
    except sqlite3.IntegrityError:
        flash("อนุมัติไม่สำเร็จ ช่องเวลาถูกใช้ไปแล้ว", "err")
    return _back("admin.slips")


@bp.route("/slips/<int:bid>/reject", methods=["POST"])
@staff_required
def slip_reject(bid):
    db = get_db()
    b = db.execute("SELECT * FROM bookings WHERE id=? AND status='checking'", (bid,)).fetchone()
    if b is None:
        flash("ไม่พบคิวที่รอตรวจสลิป", "err")
        return _back("admin.slips")
    reason = request.form.get("reason", "").strip()[:150] or "สลิปไม่ถูกต้อง"
    hold = max(1, int_setting(get_settings(db), "hold_minutes", 15))
    expires = (datetime.now() + timedelta(minutes=hold)).strftime("%Y-%m-%d %H:%M:%S")
    with tx(db):
        db.execute("""UPDATE bookings SET status='awaiting_payment', pay_expires_at=?, slip_reject_reason=?,
                      slip_file=NULL, slip_ref=NULL, slip_note='' WHERE id=?""", (expires, reason, bid))
    media.remove("slips", b["slip_file"])
    flash("ปฏิเสธสลิปคิว #%d แล้ว ลูกค้าส่งสลิปใหม่ได้ภายใน %d นาที" % (bid, hold), "ok")
    return _back("admin.slips")


@bp.route("/refunds")
@staff_required
def refunds():
    db = get_db()
    q = """SELECT b.*, u.full_name, u.phone, s.name AS service, br.name AS barber
           FROM bookings b JOIN users u ON u.id=b.user_id JOIN services s ON s.id=b.service_id
           JOIN barbers br ON br.id=b.barber_id WHERE b.payment_status IN (%s) ORDER BY %s LIMIT 100"""
    due = db.execute(q % ("'refund_due'", "b.date, b.time")).fetchall()
    hist = db.execute(q % ("'refunded','forfeited'", "b.date DESC, b.time DESC")).fetchall()
    return render_template("admin_refunds.html", due=due, hist=hist)


@bp.route("/refunds/<int:bid>/done", methods=["POST"])
@owner_required
def refund_done(bid):
    db = get_db()
    cur = db.execute("""UPDATE bookings SET payment_status='refunded', refunded_at=?, refund_note=?
                        WHERE id=? AND payment_status='refund_due'""",
                     (now_str(), request.form.get("note", "").strip()[:150], bid))
    db.commit()
    flash("บันทึกว่าคืนเงินคิว #%d แล้ว" % bid if cur.rowcount else "ไม่พบรายการรอคืนเงินนี้",
          "ok" if cur.rowcount else "err")
    return _back("admin.refunds")


# ------------------------------------------------------------ บริการ
def _duration(v):
    try:
        v = int(v)
    except (TypeError, ValueError):
        return None
    if not 15 <= v <= 480:
        return None
    return int(math.ceil(v / core.SLOT_MIN) * core.SLOT_MIN)


def _service_fields(f):
    name = f.get("name", "").strip()[:60]
    try:
        price = int(f.get("price", ""))
    except ValueError:
        price = -1
    dur = _duration(f.get("duration_min", "30"))
    if not name or not 0 <= price <= 100000 or dur is None:
        return None
    try:
        order = int(f.get("sort_order", "0") or 0)
    except ValueError:
        order = 0
    return name, price, dur, f.get("description", "").strip()[:160], order


@bp.route("/services")
@owner_required
def services():
    rows = get_db().execute("SELECT * FROM services ORDER BY active DESC, sort_order, id").fetchall()
    return render_template("admin_services.html", rows=rows)


@bp.route("/services/add", methods=["POST"])
@owner_required
def service_add():
    v = _service_fields(request.form)
    if v is None:
        flash("กรอกชื่อ ราคา (0-100000) และระยะเวลา (15-480 นาที) ให้ถูกต้อง", "err")
    else:
        db = get_db()
        db.execute("INSERT INTO services (name,price,duration_min,description,sort_order) VALUES (?,?,?,?,?)", v)
        db.commit()
        flash("เพิ่มบริการแล้ว", "ok")
    return redirect(url_for("admin.services"))


@bp.route("/services/<int:sid>/save", methods=["POST"])
@owner_required
def service_save(sid):
    v = _service_fields(request.form)
    if v is None:
        flash("ข้อมูลบริการไม่ถูกต้อง (ชื่อ/ราคา/ระยะเวลา)", "err")
    else:
        db = get_db()
        db.execute("UPDATE services SET name=?, price=?, duration_min=?, description=?, sort_order=? WHERE id=?",
                   v + (sid,))
        db.commit()
        flash("บันทึกบริการแล้ว (คิวเดิมยังใช้ราคาและระยะเวลาตอนจอง)", "ok")
    return redirect(url_for("admin.services"))


@bp.route("/services/<int:sid>/toggle", methods=["POST"])
@owner_required
def service_toggle(sid):
    db = get_db()
    db.execute("UPDATE services SET active = 1 - active WHERE id=?", (sid,))
    db.commit()
    return redirect(url_for("admin.services"))


# ------------------------------------------------------------ ตั้งค่าร้าน / QR
@bp.route("/settings", methods=["GET", "POST"])
@owner_required
def settings():
    db = get_db()
    if request.method == "POST":
        f = request.form
        errs, vals = [], {}
        for key, lo, hi, label in (("deposit_amount", 0, 5000, "ค่ามัดจำ"), ("hold_minutes", 3, 240, "เวลากันคิว"),
                                   ("refund_hours", 0, 720, "ชั่วโมงยกเลิกที่คืนมัดจำ"),
                                   ("respond_minutes", 5, 1440, "เวลาช่างตอบรับ")):
            try:
                n = int(f.get(key, ""))
                if not lo <= n <= hi:
                    raise ValueError
                vals[key] = n
            except ValueError:
                errs.append("%s ต้องเป็นตัวเลข %d-%d" % (label, lo, hi))
        pp = f.get("promptpay_id", "").strip()
        if pp and paylib.normalize_proxy(pp) is None:
            errs.append("เลขพร้อมเพย์ไม่ถูกต้อง (เบอร์โทร 10 หลัก, เลข 13 หลัก หรือ e-wallet 15 หลัก)")
        if errs:
            for e in errs:
                flash(e, "err")
            return redirect(url_for("admin.settings"))
        s = get_settings(db)
        for k, v in vals.items():
            set_setting(db, k, v)
        set_setting(db, "account_name", f.get("account_name", "").strip()[:60])
        set_setting(db, "promptpay_id", paylib.normalize_proxy(pp)[2] if pp else "")
        data, ext, err = media.read_upload(request.files.get("qr"))
        if err:
            flash(err, "err")
        elif data:
            codes = paylib.decode_qr(data)
            proxy = paylib.extract_proxy(codes[0]) if codes else None
            media.remove("shop", s.get("qr_image"))
            set_setting(db, "qr_image", media.save("shop", data, ext))
            if proxy:
                set_setting(db, "promptpay_id", proxy[1])
                flash("อ่านเลขพร้อมเพย์จากรูป QR ได้แล้ว: %s — ระบบจะสร้าง QR ใส่ยอดมัดจำให้อัตโนมัติ" % proxy[1], "ok")
            elif codes is None:
                flash("เครื่องนี้ยังไม่ได้ติดตั้ง OpenCV จึงอ่านเลขจากรูปอัตโนมัติไม่ได้ — กรอกเลขพร้อมเพย์ในช่องด้านบน "
                      "หรือใช้รูปที่อัปโหลดแสดงให้ลูกค้าแทน", "err")
            else:
                flash("อ่านรูปนี้ไม่ได้ว่าเป็น QR โอนพร้อมเพย์ — ระบบจะแสดงรูปที่อัปโหลดให้ลูกค้าสแกนแทน "
                      "(ลูกค้าต้องกรอกยอดเอง)", "err")
        elif f.get("remove_qr"):
            media.remove("shop", s.get("qr_image"))
            set_setting(db, "qr_image", "")
        db.commit()
        flash("บันทึกการตั้งค่าแล้ว", "ok")
        return redirect(url_for("admin.settings"))
    s = get_settings(db)
    preview = None
    if s.get("promptpay_id"):
        try:
            preview = paylib.qr_svg_for(paylib.promptpay_payload(s["promptpay_id"], int_setting(s, "deposit_amount", 50) or None))
        except ValueError:
            preview = None
    return render_template("admin_settings.html", s=s, preview=preview, scanner=paylib.scanner_available(),
                           dep_active=deposit_info(db)[0])


# ------------------------------------------------------------ บัญชีช่าง / แอดมินผู้ช่วย
USERNAME_RE = re.compile(r"[A-Za-z0-9_]{3,20}")


@bp.route("/staff")
@owner_required
def staff():
    db = get_db()
    barbers = db.execute("""SELECT br.*, u.username, u.active AS user_active, u.id AS uid, u.must_change_pw
                            FROM barbers br LEFT JOIN users u ON u.id=br.user_id ORDER BY br.id""").fetchall()
    admins = db.execute("SELECT * FROM users WHERE role='admin' ORDER BY id").fetchall()
    owners = db.execute("SELECT * FROM users WHERE role='owner' ORDER BY id").fetchall()
    free = [b for b in barbers if b["uid"] is None]
    return render_template("admin_staff.html", barbers=barbers, admins=admins, owners=owners, free=free)


@bp.route("/staff/create", methods=["POST"])
@owner_required
def staff_create():
    db = get_db()
    f = request.form
    kind = f.get("kind", "")
    username = f.get("username", "").strip()
    full_name = f.get("full_name", "").strip()[:60]
    pw = f.get("password", "") or random_password()
    link_id = f.get("barber_id", type=int)
    errs = []
    if kind not in ("barber", "admin"):
        errs.append("ประเภทบัญชีไม่ถูกต้อง")
    if not USERNAME_RE.fullmatch(username):
        errs.append("ชื่อผู้ใช้ต้องเป็น a-z, 0-9, _ ยาว 3-20 ตัวอักษร")
    if not full_name:
        errs.append("กรุณากรอกชื่อ")
    if len(pw) < 6:
        errs.append("รหัสผ่านต้องยาวอย่างน้อย 6 ตัวอักษร")
    if not errs and db.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
        errs.append("ชื่อผู้ใช้นี้ถูกใช้แล้ว")
    barber = None
    if kind == "barber" and link_id:
        barber = db.execute("SELECT * FROM barbers WHERE id=? AND user_id IS NULL", (link_id,)).fetchone()
        if barber is None:
            errs.append("ไม่พบช่างที่เลือก หรือช่างคนนี้มีบัญชีแล้ว")
    if errs:
        for e in errs:
            flash(e, "err")
        return redirect(url_for("admin.staff"))
    phone = re.sub(r"[\s-]", "", f.get("phone", ""))[:15]
    try:
        with tx(db):
            cur = db.execute("""INSERT INTO users (username,password_hash,full_name,phone,role,must_change_pw)
                                VALUES (?,?,?,?,?,1)""", (username, generate_password_hash(pw), full_name, phone, kind))
            uid = cur.lastrowid
            if kind == "barber":
                if barber:
                    db.execute("UPDATE barbers SET user_id=?, active=1 WHERE id=?", (uid, barber["id"]))
                else:
                    try:
                        pct = max(0, min(90, int(f.get("commission_pct", "0") or 0)))
                    except ValueError:
                        pct = 0
                    db.execute("INSERT INTO barbers (name,specialty,user_id,commission_pct) VALUES (?,?,?,?)",
                               (full_name, f.get("specialty", "").strip()[:80], uid, pct))
    except sqlite3.IntegrityError:
        flash("สร้างบัญชีไม่สำเร็จ (ข้อมูลซ้ำ)", "err")
        return redirect(url_for("admin.staff"))
    return render_template("admin_staff_created.html", username=username, password=pw, full_name=full_name,
                           role=core.ROLE_TH[kind], reset=False)


@bp.route("/staff/<int:uid>/toggle", methods=["POST"])
@owner_required
def staff_toggle(uid):
    db = get_db()
    u = db.execute("SELECT * FROM users WHERE id=? AND role IN ('barber','admin')", (uid,)).fetchone()
    if u is None:
        abort(404)                      # บัญชีเจ้าของและลูกค้าแก้ที่หน้านี้ไม่ได้
    new = 0 if u["active"] else 1
    with tx(db):
        db.execute("UPDATE users SET active=? WHERE id=?", (new, uid))
        if u["role"] == "barber":
            db.execute("UPDATE barbers SET active=? WHERE user_id=?", (new, uid))
    msg = "เปิดใช้งานบัญชี %s แล้ว" % u["username"] if new else "ปิดบัญชี %s แล้ว (เข้าสู่ระบบไม่ได้)" % u["username"]
    if not new and u["role"] == "barber":
        n = db.execute("""SELECT COUNT(*) FROM bookings b JOIN barbers br ON br.id=b.barber_id WHERE br.user_id=?
                          AND b.date>=? AND b.status IN ('awaiting_barber','confirmed','awaiting_payment','checking')""",
                       (uid, date.today().isoformat())).fetchone()[0]
        if n:
            msg += " — ช่างคนนี้ยังมีคิวล่วงหน้า %d คิว กรุณาย้าย/ยกเลิกในหน้าคิว" % n
    flash(msg, "ok")
    return redirect(url_for("admin.staff"))


@bp.route("/staff/<int:uid>/reset", methods=["POST"])
@owner_required
def staff_reset(uid):
    db = get_db()
    u = db.execute("SELECT * FROM users WHERE id=? AND role IN ('barber','admin')", (uid,)).fetchone()
    if u is None:
        abort(404)
    pw = random_password()
    db.execute("UPDATE users SET password_hash=?, must_change_pw=1 WHERE id=?", (generate_password_hash(pw), uid))
    db.commit()
    return render_template("admin_staff_created.html", username=u["username"], password=pw,
                           full_name=u["full_name"], role=core.ROLE_TH[u["role"]], reset=True)


@bp.route("/staff/barber/<int:bid>/toggle", methods=["POST"])
@owner_required
def barber_toggle(bid):
    db = get_db()
    b = db.execute("SELECT * FROM barbers WHERE id=? AND user_id IS NULL", (bid,)).fetchone()
    if b is None:
        abort(404)
    db.execute("UPDATE barbers SET active = 1 - active WHERE id=?", (bid,))
    db.commit()
    return redirect(url_for("admin.staff"))


@bp.route("/staff/barber/<int:bid>/commission", methods=["POST"])
@owner_required
def barber_commission(bid):
    try:
        pct = int(request.form.get("commission_pct", ""))
        if not 0 <= pct <= 90:
            raise ValueError
    except ValueError:
        flash("ส่วนแบ่งช่างต้องเป็น 0-90%", "err")
        return redirect(url_for("admin.staff"))
    db = get_db()
    db.execute("UPDATE barbers SET commission_pct=? WHERE id=?", (pct, bid))
    db.commit()
    flash("บันทึกส่วนแบ่งช่างแล้ว", "ok")
    return redirect(url_for("admin.staff"))


# ------------------------------------------------------------ โปรไฟล์ช่าง (เจ้าของแก้แทนได้)
def _barber_or_404(db, bid):
    b = db.execute("SELECT * FROM barbers WHERE id=?", (bid,)).fetchone()
    if b is None:
        abort(404)
    return b


@bp.route("/barber/<int:bid>/profile", methods=["GET", "POST"])
@owner_required
def barber_profile(bid):
    db = get_db()
    b = _barber_or_404(db, bid)
    if request.method == "POST":
        err = vb.save_profile(db, b, request.form, request.files)
        flash(err or "บันทึกโปรไฟล์ช่างแล้ว", "err" if err else "ok")
        return redirect(url_for("admin.barber_profile", bid=bid))
    gallery = db.execute("SELECT * FROM barber_gallery WHERE barber_id=? ORDER BY id DESC", (bid,)).fetchall()
    return render_template("barber_profile_edit.html", b=b, gallery=gallery, scope="admin", gallery_max=vb.GALLERY_MAX)


@bp.route("/barber/<int:bid>/gallery/add", methods=["POST"])
@owner_required
def barber_gallery_add(bid):
    db = get_db()
    err = vb.add_gallery(db, _barber_or_404(db, bid), request.form, request.files)
    flash(err or "เพิ่มรูปผลงานแล้ว", "err" if err else "ok")
    return redirect(url_for("admin.barber_profile", bid=bid))


@bp.route("/barber/<int:bid>/gallery/<int:gid>/delete", methods=["POST"])
@owner_required
def barber_gallery_delete(bid, gid):
    db = get_db()
    vb.delete_gallery(db, _barber_or_404(db, bid), gid)
    return redirect(url_for("admin.barber_profile", bid=bid))


# ------------------------------------------------------------ สรุปผลงานช่าง
def _range(args):
    today = date.today()
    r = args.get("range", "month")
    if r == "today":
        d1 = d2 = today
    elif r == "7d":
        d1, d2 = today - timedelta(days=6), today
    elif r == "lastmonth":
        d2 = today.replace(day=1) - timedelta(days=1)
        d1 = d2.replace(day=1)
    elif r == "custom":
        try:
            d1 = datetime.strptime(args.get("from", ""), "%Y-%m-%d").date()
            d2 = datetime.strptime(args.get("to", ""), "%Y-%m-%d").date()
        except ValueError:
            r, d1, d2 = "month", today.replace(day=1), today
        if d1 > d2:
            d1, d2 = d2, d1
    else:
        r, d1, d2 = "month", today.replace(day=1), today
    return r, d1.isoformat(), d2.isoformat()


def stats_data(db, d1, d2):
    rows = [dict(r) for r in db.execute("""
        SELECT br.id, br.name, br.commission_pct, br.active,
          COALESCE(SUM(CASE WHEN b.status='done' THEN 1 END),0) AS done_cnt,
          COALESCE(SUM(CASE WHEN b.status='done' THEN b.price END),0) AS revenue,
          COALESCE(SUM(CASE WHEN b.status='done' AND b.payment_status='paid' THEN b.deposit_amount END),0) AS online_part,
          COALESCE(SUM(CASE WHEN b.status IN ('confirmed','in_progress','done','no_show') THEN 1 END),0) AS accepted_cnt,
          COALESCE(SUM(CASE WHEN b.status='declined' THEN 1 END),0) AS declined_cnt,
          COALESCE(SUM(CASE WHEN b.status='cancelled' THEN 1 END),0) AS cancelled_cnt,
          COALESCE(SUM(CASE WHEN b.status='no_show' THEN 1 END),0) AS noshow_cnt,
          COALESCE(SUM(CASE WHEN b.payment_status='forfeited' THEN b.deposit_amount END),0) AS forfeited
        FROM barbers br LEFT JOIN bookings b ON b.barber_id=br.id AND b.date BETWEEN ? AND ?
        GROUP BY br.id ORDER BY revenue DESC, br.id""", (d1, d2))]
    total_rev = sum(r["revenue"] for r in rows)
    for r in rows:
        r["avg"] = r["revenue"] / r["done_cnt"] if r["done_cnt"] else 0
        r["share"] = r["revenue"] * 100 / total_rev if total_rev else 0
        resp = r["accepted_cnt"] + r["declined_cnt"]
        r["accept_rate"] = r["accepted_cnt"] * 100 / resp if resp else None
        r["shop_net"] = r["revenue"] * (100 - r["commission_pct"]) / 100
        r["shop_collect"] = r["revenue"] - r["online_part"]
    tops = {}
    for t in db.execute("""SELECT b.barber_id, s.name, COUNT(*) AS c, SUM(b.price) AS rev FROM bookings b
                           JOIN services s ON s.id=b.service_id WHERE b.status='done' AND b.date BETWEEN ? AND ?
                           GROUP BY b.barber_id, s.id ORDER BY c DESC""", (d1, d2)):
        tops.setdefault(t["barber_id"], []).append((t["name"], t["c"], t["rev"]))
    daily = [dict(r) for r in db.execute("""SELECT date, SUM(price) AS rev, COUNT(*) AS cnt FROM bookings
                    WHERE status='done' AND date BETWEEN ? AND ? GROUP BY date ORDER BY date""", (d1, d2))]
    tot = {k: sum(r[k] for r in rows) for k in ("done_cnt", "revenue", "online_part", "shop_net", "shop_collect",
                                                "declined_cnt", "cancelled_cnt", "noshow_cnt", "forfeited")}
    return rows, tot, tops, daily


@bp.route("/stats")
@owner_required
def stats():
    r, d1, d2 = _range(request.args)
    rows, tot, tops, daily = stats_data(get_db(), d1, d2)
    return render_template("admin_stats.html", rows=rows, tot=tot, tops=tops, daily=daily, r=r, d1=d1, d2=d2,
                           maxrev=max([d["rev"] for d in daily] or [1]))


@bp.route("/stats.csv")
@owner_required
def stats_csv():
    r, d1, d2 = _range(request.args)
    rows, tot, _, _ = stats_data(get_db(), d1, d2)
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["ช่วง", d1, d2])
    w.writerow(["ช่าง", "งานเสร็จ", "รายได้ที่หาให้ร้าน", "สัดส่วน%", "เฉลี่ยต่องาน", "มัดจำออนไลน์", "เก็บหน้าร้าน",
                "ส่วนแบ่งช่าง%", "ส่วนที่ร้านได้", "ปฏิเสธ", "ยกเลิก", "ไม่มา", "มัดจำที่ริบ"])
    for x in rows:
        w.writerow([x["name"], x["done_cnt"], x["revenue"], round(x["share"], 1), round(x["avg"]), x["online_part"],
                    x["shop_collect"], x["commission_pct"], round(x["shop_net"]), x["declined_cnt"],
                    x["cancelled_cnt"], x["noshow_cnt"], x["forfeited"]])
    w.writerow(["รวม", tot["done_cnt"], tot["revenue"], "", "", tot["online_part"], tot["shop_collect"], "",
                round(tot["shop_net"]), tot["declined_cnt"], tot["cancelled_cnt"], tot["noshow_cnt"], tot["forfeited"]])
    return Response("\ufeff" + out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=barber-stats-%s_%s.csv" % (d1, d2)})


@bp.route("/exit", methods=["POST"])
@staff_required
def exit_mode():
    session.pop("admin", None)
    session.pop("admin_at", None)
    flash("ออกจากโหมดแอดมินแล้ว", "ok")
    return redirect(url_for("main.index"))
