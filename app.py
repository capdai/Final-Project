import os
import sys
import secrets
import sqlite3
import getpass

from flask import Flask, render_template
from werkzeug.exceptions import HTTPException
from werkzeug.security import generate_password_hash

import core
import db as dbmod
import media


def _secret_key():
    env = os.environ.get("SECRET_KEY")
    if env:
        return env
    path = os.path.join(core.BASE_DIR, ".secret_key")
    if not os.path.exists(path):
        with open(path, "w") as f:
            f.write(secrets.token_hex(32))
    with open(path) as f:
        return f.read().strip()


def _asset_version():
    latest = 0
    for root, _, files in os.walk(os.path.join(core.BASE_DIR, "static")):
        for fn in files:
            latest = max(latest, int(os.path.getmtime(os.path.join(root, fn))))
    return str(latest)


def create_app():
    app = Flask(__name__)
    app.config.update(SECRET_KEY=_secret_key(), SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                      MAX_CONTENT_LENGTH=media.MAX_BYTES + 512 * 1024, ASSET_V=_asset_version())
    core.init_app(app)
    from views_main import bp as main_bp
    from views_barber import bp as barber_bp
    from views_admin import bp as admin_bp
    for bp in (main_bp, barber_bp, admin_bp):
        app.register_blueprint(bp)

    @app.errorhandler(HTTPException)
    def http_error(e):
        msgs = {404: "ไม่พบหน้านี้", 403: "คุณไม่มีสิทธิ์เข้าหน้านี้",
                413: "ไฟล์ที่ส่งมาใหญ่เกินกำหนด (สูงสุด 5 MB ต่อรูป)"}
        return render_template("error.html", e=e, message=msgs.get(e.code, e.description)), e.code

    return app


def _cli(argv):
    cmd = argv[0]
    if len(argv) < 2:
        print("ใช้งาน: python app.py %s USERNAME" % cmd)
        return 1
    dbmod.init_db(core.DB_PATH, core.OWNER_USERNAME)
    con = sqlite3.connect(core.DB_PATH)
    u = con.execute("SELECT id FROM users WHERE username=?", (argv[1],)).fetchone()
    if not u:
        print("ไม่พบบัญชี '%s'" % argv[1])
        return 1
    if cmd == "make-owner":
        con.execute("UPDATE users SET role='owner', active=1, must_change_pw=0 WHERE id=?", (u[0],))
        print("ตั้ง '%s' เป็นเจ้าของระบบแล้ว" % argv[1])
    else:
        pw = getpass.getpass("รหัสผ่านใหม่ (6 ตัวขึ้นไป): ")
        if len(pw) < 6:
            print("รหัสผ่านสั้นเกินไป")
            return 1
        con.execute("UPDATE users SET password_hash=?, active=1, must_change_pw=0 WHERE id=?",
                    (generate_password_hash(pw), u[0]))
        print("ตั้งรหัสผ่านใหม่ให้ '%s' แล้ว" % argv[1])
    con.commit()
    con.close()
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in ("make-owner", "set-password"):
        sys.exit(_cli(sys.argv[1:]))
    dbmod.init_db(core.DB_PATH, core.OWNER_USERNAME)
    create_app().run(host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", 5000)), debug=False)
