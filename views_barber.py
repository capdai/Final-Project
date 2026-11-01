import sqlite3
from datetime import datetime, timedelta

from flask import Blueprint, render_template, request, redirect, url_for, flash, abort, g

import core
import media
from core import get_db, tx, barber_required, my_barber, change_status

bp = Blueprint("barber", __name__, url_prefix="/barber")
GALLERY_MAX = 12


# ------------------------------------------------------------ ตัวช่วยโปรไฟล์ (ใช้ร่วมกับหน้าเจ้าของ)
def save_profile(db, barber, form, files):
    """คืนข้อความ error หรือ None เมื่อสำเร็จ"""
    name = form.get("name", "").strip()[:60] or barber["name"]
    specialty = form.get("specialty", "").strip()[:80]
    bio = form.get("bio", "").strip()[:600]
    try:
        exp = max(0, min(60, int(form.get("experience_years", "0") or 0)))
    except ValueError:
        exp = 0
    photo = barber["photo"]
    data, ext, err = media.read_upload(files.get("photo"))
    if err:
        return err
    if data:
        try:
            new = media.save("barbers", media.optimize(data, 900), "webp")
        except ValueError as e:
            return str(e)
        media.remove("barbers", photo)
        photo = new
    elif form.get("remove_photo"):
        media.remove("barbers", photo)
        photo = None
    db.execute("UPDATE barbers SET name=?, specialty=?, bio=?, experience_years=?, photo=? WHERE id=?",
               (name, specialty, bio, exp, photo, barber["id"]))
    db.commit()
    return None


def add_gallery(db, barber, form, files):
    if db.execute("SELECT COUNT(*) FROM barber_gallery WHERE barber_id=?", (barber["id"],)).fetchone()[0] >= GALLERY_MAX:
        return "แกลเลอรีเต็มแล้ว (สูงสุด %d รูป) ลบรูปเก่าก่อน" % GALLERY_MAX
    data, ext, err = media.read_upload(files.get("image"))
    if err or not data:
        return err or "กรุณาเลือกรูปผลงาน"
    try:
        name = media.save("barbers", media.optimize(data, 1400), "webp")
    except ValueError as e:
        return str(e)
    db.execute("INSERT INTO barber_gallery (barber_id,filename,caption) VALUES (?,?,?)",
               (barber["id"], name, form.get("caption", "").strip()[:60]))
    db.commit()
    return None


def delete_gallery(db, barber, gid):
    row = db.execute("SELECT * FROM barber_gallery WHERE id=? AND barber_id=?", (gid, barber["id"])).fetchone()
    if row:
        media.remove("barbers", row["filename"])
        db.execute("DELETE FROM barber_gallery WHERE id=?", (gid,))
        db.commit()
    return bool(row)


# ------------------------------------------------------------ แดชบอร์ดงาน
def _jobs(db, barber_id, where, order, limit=200):
    return db.execute("""SELECT b.*, s.name AS service, u.full_name AS customer, u.phone AS customer_phone
                         FROM bookings b JOIN services s ON s.id=b.service_id JOIN users u ON u.id=b.user_id
                         WHERE b.barber_id=? AND %s ORDER BY %s LIMIT %d""" % (where, order, limit),
                      (barber_id,)).fetchall()


@bp.route("/")
@barber_required
def dashboard():
    db = get_db()
    me = my_barber(db)
    today = datetime.now().strftime("%Y-%m-%d")
    tab = request.args.get("tab", "pending")
    queries = {
        "pending": ("status='awaiting_barber'", "date, time"),
        "today": ("date='%s' AND status IN ('confirmed','in_progress','done','no_show')" % today, "time"),
        "upcoming": ("date>'%s' AND status='confirmed'" % today, "date, time"),
        "history": ("status IN ('done','declined','no_show','cancelled','expired')", "date DESC, time DESC"),
    }
    if tab not in queries:
        tab = "pending"
    rows = _jobs(db, me["id"], *queries[tab])
    counts = {k: db.execute("SELECT COUNT(*) FROM bookings WHERE barber_id=? AND %s" % w, (me["id"],)).fetchone()[0]
              for k, (w, _) in queries.items() if k != "history"}
    done = db.execute("SELECT COUNT(*) FROM bookings WHERE barber_id=? AND status='done'", (me["id"],)).fetchone()[0]
    dayoffs = [r[0] for r in db.execute("SELECT date FROM barber_dayoff WHERE barber_id=? AND date>=? ORDER BY date",
                                        (me["id"], today))]
    return render_template("barber_dashboard.html", me=me, rows=rows, tab=tab, counts=counts, done=done,
                           today=today, dayoffs=dayoffs,
                           max_off=(datetime.now() + timedelta(days=90)).strftime("%Y-%m-%d"))


@bp.route("/job/<int:bid>/<action>", methods=["POST"])
@barber_required
def job_action(bid, action):
    db = get_db()
    me = my_barber(db)
    b = db.execute("SELECT * FROM bookings WHERE id=? AND barber_id=?", (bid, me["id"])).fetchone()
    if b is None:
        abort(404)
    today = datetime.now().strftime("%Y-%m-%d")
    st, reason, msg, new = b["status"], "", "", None
    if action == "accept" and st == "awaiting_barber":
        new, msg = "confirmed", "รับงานแล้ว ลูกค้าจะเห็นว่าคุณยืนยันคิวนี้"
    elif action == "decline" and st in ("awaiting_barber", "confirmed"):
        reason = request.form.get("reason", "").strip()
        if len(reason) < 3:
            flash("กรุณาระบุเหตุผลที่ปฏิเสธงาน (อย่างน้อย 3 ตัวอักษร)", "err")
            return redirect(request.referrer or url_for("barber.dashboard"))
        new, msg = "declined", "ปฏิเสธงานแล้ว ช่องเวลานี้ว่างอีกครั้ง และร้านจะจัดการคืนมัดจำให้ลูกค้า"
    elif action in ("start", "done", "noshow"):
        if b["date"] > today:
            flash("ยังไม่ถึงวันนัด จึงเปลี่ยนสถานะนี้ไม่ได้", "err")
            return redirect(request.referrer or url_for("barber.dashboard"))
        if action == "start" and st == "confirmed":
            new, msg = "in_progress", "เริ่มทำงานแล้ว"
        elif action == "done" and st in ("confirmed", "in_progress"):
            new, msg = "done", "บันทึกว่างานเสร็จแล้ว"
        elif action == "noshow" and st == "confirmed":
            new, msg = "no_show", "บันทึกว่าลูกค้าไม่มาตามนัด"
    if new is None:
        flash("ทำรายการนี้ไม่ได้ในสถานะปัจจุบัน", "err")
        return redirect(request.referrer or url_for("barber.dashboard"))
    try:
        with tx(db):
            change_status(db, b, new, reason=reason)
    except sqlite3.Error:
        flash("เกิดข้อผิดพลาด กรุณาลองใหม่", "err")
        return redirect(request.referrer or url_for("barber.dashboard"))
    flash(msg, "ok")
    return redirect(request.referrer or url_for("barber.dashboard"))


@bp.route("/accepting", methods=["POST"])
@barber_required
def accepting():
    db = get_db()
    me = my_barber(db)
    db.execute("UPDATE barbers SET accepting = 1 - accepting WHERE id=?", (me["id"],))
    db.commit()
    flash("ปิดรับคิวใหม่ชั่วคราวแล้ว" if me["accepting"] else "เปิดรับคิวใหม่แล้ว", "ok")
    return redirect(url_for("barber.dashboard"))


@bp.route("/dayoff", methods=["POST"])
@barber_required
def dayoff_add():
    db = get_db()
    me = my_barber(db)
    d = request.form.get("date", "")
    try:
        dd = datetime.strptime(d, "%Y-%m-%d").date()
    except ValueError:
        flash("วันที่ไม่ถูกต้อง", "err")
        return redirect(url_for("barber.dashboard"))
    if dd < datetime.now().date() or dd > datetime.now().date() + timedelta(days=90):
        flash("เลือกวันหยุดได้ตั้งแต่วันนี้ถึง 90 วันข้างหน้า", "err")
        return redirect(url_for("barber.dashboard"))
    db.execute("INSERT OR IGNORE INTO barber_dayoff (barber_id,date) VALUES (?,?)", (me["id"], d))
    db.commit()
    n = db.execute("SELECT COUNT(*) FROM bookings WHERE barber_id=? AND date=? AND status IN "
                   "('awaiting_barber','confirmed','awaiting_payment','checking')", (me["id"], d)).fetchone()[0]
    flash("บันทึกวันหยุดแล้ว" + (" — แต่วันนั้นยังมีคิวอยู่ %d คิว กรุณาปฏิเสธหรือแจ้งเจ้าของ" % n if n else ""),
          "err" if n else "ok")
    return redirect(url_for("barber.dashboard"))


@bp.route("/dayoff/<d>/delete", methods=["POST"])
@barber_required
def dayoff_delete(d):
    db = get_db()
    db.execute("DELETE FROM barber_dayoff WHERE barber_id=? AND date=?", (my_barber(db)["id"], d))
    db.commit()
    return redirect(url_for("barber.dashboard"))


# ------------------------------------------------------------ โปรไฟล์ตัวเอง
@bp.route("/profile", methods=["GET", "POST"])
@barber_required
def profile():
    db = get_db()
    me = my_barber(db)
    if request.method == "POST":
        err = save_profile(db, me, request.form, request.files)
        flash(err or "บันทึกโปรไฟล์แล้ว", "err" if err else "ok")
        return redirect(url_for("barber.profile"))
    gallery = db.execute("SELECT * FROM barber_gallery WHERE barber_id=? ORDER BY id DESC", (me["id"],)).fetchall()
    return render_template("barber_profile_edit.html", b=me, gallery=gallery, scope="barber", gallery_max=GALLERY_MAX)


@bp.route("/gallery/add", methods=["POST"])
@barber_required
def gallery_add():
    db = get_db()
    err = add_gallery(db, my_barber(db), request.form, request.files)
    flash(err or "เพิ่มรูปผลงานแล้ว", "err" if err else "ok")
    return redirect(url_for("barber.profile"))


@bp.route("/gallery/<int:gid>/delete", methods=["POST"])
@barber_required
def gallery_delete(gid):
    db = get_db()
    delete_gallery(db, my_barber(db), gid)
    return redirect(url_for("barber.profile"))
