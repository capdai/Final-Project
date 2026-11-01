import re
import hashlib

import qrgen

PROMPTPAY_AID = "A000000677010111"   # โอนเงินพร้อมเพย์ (credit transfer)


# ---------------------------------------------------------------- EMV helpers
def crc16(text):
    """CRC-16/CCITT-FALSE (poly 0x1021, init 0xFFFF) ตามมาตรฐาน EMVCo / Thai QR"""
    crc = 0xFFFF
    for ch in text.encode("ascii"):
        crc ^= ch << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else (crc << 1)
            crc &= 0xFFFF
    return "%04X" % crc


def _tlv(tag, val):
    return "%s%02d%s" % (tag, len(val), val)


def parse_tlv(s):
    """แยก TLV ระดับเดียว -> [(tag, value)] ถ้ารูปแบบผิดจะหยุดและคืนเท่าที่แยกได้"""
    out, i = [], 0
    while i + 4 <= len(s):
        tag, ln = s[i:i + 2], s[i + 2:i + 4]
        if not ln.isdigit():
            break
        n = int(ln)
        val = s[i + 4:i + 4 + n]
        if len(val) != n:
            break
        out.append((tag, val))
        i += 4 + n
    return out


# ---------------------------------------------------------------- PromptPay
def normalize_proxy(raw):
    """รับเบอร์โทร/เลขบัตร ปชช./เลขผู้เสียภาษี/e-wallet -> (kind, ค่าใน QR, ค่าที่แสดง) หรือ None"""
    d = re.sub(r"\D", "", raw or "")
    if len(d) == 13 and d.startswith("0066"):
        d = "0" + d[4:]
    elif len(d) == 11 and d.startswith("66"):
        d = "0" + d[2:]
    if len(d) == 10 and d.startswith("0"):
        return "phone", "0066" + d[1:], d
    if len(d) == 13:
        return "id", d, d
    if len(d) == 15:
        return "ewallet", d, d
    return None


def promptpay_payload(proxy_raw, amount=None):
    p = normalize_proxy(proxy_raw)
    if p is None:
        raise ValueError("เลขพร้อมเพย์ไม่ถูกต้อง (ใช้เบอร์โทร 10 หลัก, เลข 13 หลัก หรือ e-wallet 15 หลัก)")
    kind, wire, _ = p
    sub = _tlv("00", PROMPTPAY_AID) + _tlv({"phone": "01", "id": "02", "ewallet": "03"}[kind], wire)
    body = _tlv("00", "01") + _tlv("01", "12" if amount else "11") + _tlv("29", sub) + _tlv("53", "764")
    if amount:
        body += _tlv("54", "%.2f" % float(amount))
    body += _tlv("58", "TH") + "6304"
    return body + crc16(body)


def extract_proxy(payload):
    """อ่านเลขพร้อมเพย์จากข้อความใน QR รับเงิน -> (kind, ค่า) หรือ None (เช่น QR บิล/ร้านค้าประเภทอื่น)"""
    try:
        top = parse_tlv(payload)
        if not top or top[0][0] != "00":
            return None
        for tag, val in top:
            if tag in ("29", "30", "31") or tag.isdigit() and 26 <= int(tag) <= 51:
                inner = dict(parse_tlv(val))
                if inner.get("00") != PROMPTPAY_AID:
                    continue
                for t, kind in (("01", "phone"), ("02", "id"), ("03", "ewallet")):
                    if t in inner:
                        v = inner[t]
                        if kind == "phone" and v.startswith("0066"):
                            v = "0" + v[4:]
                        return kind, v
    except Exception:
        pass
    return None


def qr_svg_for(payload):
    return qrgen.qr_svg(payload)


# ---------------------------------------------------------------- อ่าน QR จากรูป
def scanner_available():
    try:
        import cv2  # noqa: F401
        import numpy  # noqa: F401
        return True
    except Exception:
        return False


def decode_qr(data):
    """คืน None = ไม่มี OpenCV, [] = ไม่พบ QR, [ข้อความ] = พบ"""
    try:
        import cv2
        import numpy as np
    except Exception:
        return None
    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return []
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    det = cv2.QRCodeDetector()
    for sc in (1.0, 0.6, 1.6, 0.35, 2.2):
        g = gray if sc == 1.0 else cv2.resize(
            gray, None, fx=sc, fy=sc, interpolation=cv2.INTER_AREA if sc < 1 else cv2.INTER_CUBIC)
        if max(g.shape) > 4200 or max(g.shape) < 120:
            continue
        thr = cv2.adaptiveThreshold(g, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 10)
        for variant in (g, thr):
            try:
                val, _, _ = det.detectAndDecode(variant)
                if val:
                    return [val]
                res = det.detectAndDecodeMulti(variant)
                if res and res[0] and res[1]:
                    found = [v for v in res[1] if v]
                    if found:
                        return found[:1]
            except cv2.error:
                continue
    return []


def analyze_slip(data):
    """วิเคราะห์รูปสลิป -> dict
    scanner: มีตัวอ่าน QR ไหม | payload | ref (ใช้กันซ้ำ) | bank | trans_ref | is_shop_qr | note"""
    info = {"scanner": scanner_available(), "payload": None, "trans_ref": None, "bank": None,
            "is_shop_qr": False, "note": ""}
    img_hash = hashlib.sha256(data).hexdigest()
    info["ref"] = "img:" + img_hash
    codes = decode_qr(data)
    if codes is None:
        info["note"] = "ตรวจ QR อัตโนมัติไม่ได้ (เครื่องยังไม่ได้ติดตั้ง OpenCV) — ต้องตรวจด้วยตา"
        return info
    if not codes:
        info["note"] = "ไม่พบ QR บนรูปนี้ — ระวัง อาจเป็นสลิปที่ครอป/ตัดต่อ ต้องตรวจด้วยตา"
        return info
    p = codes[0]
    info["payload"] = p
    if p.startswith("000201"):
        info["is_shop_qr"] = True
        info["note"] = "รูปนี้เป็น QR รับเงิน ไม่ใช่สลิปโอนเงิน"
        return info
    info["ref"] = "qr:" + hashlib.sha256(p.encode("utf-8")).hexdigest()
    top = dict(parse_tlv(p))
    inner = dict(parse_tlv(top["00"])) if "00" in top else {}
    info["bank"] = inner.get("01")
    info["trans_ref"] = inner.get("02")
    info["note"] = "พบ QR บนสลิป" + (" (รหัสอ้างอิง %s)" % info["trans_ref"] if info["trans_ref"] else "")
    return info
