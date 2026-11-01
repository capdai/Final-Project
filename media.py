import io
import os
import secrets

from PIL import Image, ImageOps

import core

Image.MAX_IMAGE_PIXELS = 40_000_000
MAX_BYTES = 5 * 1024 * 1024
MIME = {"jpg": "image/jpeg", "png": "image/png", "gif": "image/gif", "webp": "image/webp"}


def sniff_image(data):
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def read_upload(fs, max_bytes=MAX_BYTES):
    """คืน (data, ext, error) ; ไม่ได้แนบไฟล์ -> (None, None, None)"""
    if fs is None or not fs.filename:
        return None, None, None
    data = fs.read(max_bytes + 1)
    if not data:
        return None, None, None
    if len(data) > max_bytes:
        return None, None, "ไฟล์ใหญ่เกิน %d MB กรุณาเลือกไฟล์ที่เล็กกว่านี้" % (max_bytes // 1024 // 1024)
    ext = sniff_image(data)
    if ext is None:
        return None, None, "รองรับเฉพาะไฟล์รูป JPG, PNG, WEBP หรือ GIF"
    return data, ext, None


def optimize(data, max_side=1600, quality=85):
    """ย่อและแปลงเป็น WebP (ตัด EXIF) คืนไบต์ ; ถ้าเปิดรูปไม่ได้ -> ValueError"""
    try:
        with Image.open(io.BytesIO(data)) as im:
            im.load()
            im = ImageOps.exif_transpose(im)
            has_alpha = im.mode in ("RGBA", "LA", "P") and ("A" in im.getbands() or "transparency" in im.info)
            im = im.convert("RGBA" if has_alpha else "RGB")
            im.thumbnail((max_side, max_side))
            out = io.BytesIO()
            im.save(out, "WEBP", quality=quality, method=4)
            return out.getvalue()
    except Exception as e:                      # รูปเสีย/ใหญ่เกินกำหนด
        raise ValueError("เปิดไฟล์รูปไม่ได้ กรุณาลองรูปอื่น") from e


def _dir(subdir):
    return os.path.join(core.UPLOAD_DIR, subdir) if subdir else core.UPLOAD_DIR


def save(subdir, data, ext):
    os.makedirs(_dir(subdir), exist_ok=True)
    name = secrets.token_hex(16) + "." + ext
    with open(os.path.join(_dir(subdir), name), "wb") as fh:
        fh.write(data)
    return name


def remove(subdir, name):
    if name:
        try:
            os.remove(os.path.join(_dir(subdir), os.path.basename(name)))
        except OSError:
            pass


def path(subdir, name):
    p = os.path.join(_dir(subdir), os.path.basename(name or ""))
    return p if name and os.path.isfile(p) else None
