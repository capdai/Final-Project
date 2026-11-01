_ECC_PER_BLOCK = [None, 10, 16, 26, 18, 24, 16, 18, 22, 22, 26]
_NUM_BLOCKS = [None, 1, 1, 1, 2, 2, 4, 4, 4, 5, 5]
MAX_VERSION = 10


def _raw_modules(ver):
    r = (16 * ver + 128) * ver + 64
    if ver >= 2:
        na = ver // 7 + 2
        r -= (25 * na - 10) * na - 55
        if ver >= 7:
            r -= 36
    return r


def _data_capacity_bits(ver):
    return (_raw_modules(ver) // 8 - _ECC_PER_BLOCK[ver] * _NUM_BLOCKS[ver]) * 8


def _align_positions(ver):
    if ver == 1:
        return []
    na = ver // 7 + 2
    size = ver * 4 + 17
    step = (ver * 4 + na * 2 + 1) // (na * 2 - 2) * 2
    res = [size - 7 - i * step for i in range(na - 1)] + [6]
    return res[::-1]


def _gf_mul(x, y):
    z = 0
    for i in range(7, -1, -1):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z


def _rs_divisor(degree):
    result = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(degree):
            result[j] = _gf_mul(result[j], root)
            if j + 1 < degree:
                result[j] ^= result[j + 1]
        root = _gf_mul(root, 2)
    return result


def _rs_remainder(data, divisor):
    result = [0] * len(divisor)
    for b in data:
        factor = b ^ result.pop(0)
        result.append(0)
        for i, c in enumerate(divisor):
            result[i] ^= _gf_mul(c, factor)
    return result


def _encode_data(text):
    data = text.encode("utf-8")
    for ver in range(1, MAX_VERSION + 1):
        cc_bits = 8 if ver <= 9 else 16
        need = 4 + cc_bits + 8 * len(data)
        if need <= _data_capacity_bits(ver):
            break
    else:
        raise ValueError("ข้อความยาวเกินไปสำหรับ QR")
    cap = _data_capacity_bits(ver)
    bits = []

    def put(val, n):
        for i in range(n - 1, -1, -1):
            bits.append((val >> i) & 1)

    put(0b0100, 4)
    put(len(data), cc_bits)
    for b in data:
        put(b, 8)
    put(0, min(4, cap - len(bits)))
    while len(bits) % 8:
        bits.append(0)
    pad = 0xEC
    while len(bits) < cap:
        put(pad, 8)
        pad ^= 0xEC ^ 0x11
    cw = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    return ver, cw


def _add_ecc_interleave(data, ver):
    nb, ecc_len = _NUM_BLOCKS[ver], _ECC_PER_BLOCK[ver]
    raw = _raw_modules(ver) // 8
    nshort, shortlen = nb - raw % nb, raw // nb
    div = _rs_divisor(ecc_len)
    blocks, k = [], 0
    for i in range(nb):
        dat = data[k:k + shortlen - ecc_len + (0 if i < nshort else 1)]
        k += len(dat)
        ecc = _rs_remainder(dat, div)
        if i < nshort:
            dat = dat + [0]
        blocks.append(dat + ecc)
    out = []
    for i in range(len(blocks[0])):
        for j, blk in enumerate(blocks):
            if i != shortlen - ecc_len or j >= nshort:
                out.append(blk[i])
    return out


class _Matrix:
    def __init__(self, ver):
        self.ver = ver
        self.size = ver * 4 + 17
        self.m = [[False] * self.size for _ in range(self.size)]
        self.f = [[False] * self.size for _ in range(self.size)]
        self._draw_function_patterns()

    def _set(self, x, y, dark):
        self.m[y][x] = dark
        self.f[y][x] = True

    def _finder(self, cx, cy):
        for dy in range(-4, 5):
            for dx in range(-4, 5):
                x, y = cx + dx, cy + dy
                if 0 <= x < self.size and 0 <= y < self.size:
                    self._set(x, y, max(abs(dx), abs(dy)) not in (2, 4))

    def _align(self, cx, cy):
        for dy in range(-2, 3):
            for dx in range(-2, 3):
                self._set(cx + dx, cy + dy, max(abs(dx), abs(dy)) != 1)

    def _draw_function_patterns(self):
        n = self.size
        for i in range(n):
            self._set(6, i, i % 2 == 0)
            self._set(i, 6, i % 2 == 0)
        self._finder(3, 3)
        self._finder(n - 4, 3)
        self._finder(3, n - 4)
        pos = _align_positions(self.ver)
        k = len(pos)
        for i in range(k):
            for j in range(k):
                if not ((i == 0 and j == 0) or (i == 0 and j == k - 1) or (i == k - 1 and j == 0)):
                    self._align(pos[i], pos[j])
        self._draw_format(self.m, 0)          # จองที่ไว้ก่อน
        self._draw_version()

    def _draw_format(self, m, mask):
        n = self.size
        data = mask            # ECC ระดับ M = 0b00
        rem = data
        for _ in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        bits = ((data << 10) | rem) ^ 0x5412

        def bit(i):
            return ((bits >> i) & 1) != 0
        for i in range(0, 6):
            self._put(m, 8, i, bit(i))
        self._put(m, 8, 7, bit(6))
        self._put(m, 8, 8, bit(7))
        self._put(m, 7, 8, bit(8))
        for i in range(9, 15):
            self._put(m, 14 - i, 8, bit(i))
        for i in range(0, 8):
            self._put(m, n - 1 - i, 8, bit(i))
        for i in range(8, 15):
            self._put(m, 8, n - 15 + i, bit(i))
        self._put(m, 8, n - 8, True)

    def _put(self, m, x, y, dark):
        m[y][x] = dark
        self.f[y][x] = True

    def _draw_version(self):
        if self.ver < 7:
            return
        rem = self.ver
        for _ in range(12):
            rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
        bits = (self.ver << 12) | rem
        n = self.size
        for i in range(18):
            b = ((bits >> i) & 1) != 0
            a, c = n - 11 + i % 3, i // 3
            self._set(a, c, b)
            self._set(c, a, b)

    def draw_codewords(self, data):
        n = self.size
        i = 0
        right = n - 1
        while right >= 1:
            if right <= 6:
                right -= 1
            for vert in range(n):
                for j in range(2):
                    x = right - j
                    upward = ((right + 1) & 2) == 0
                    y = (n - 1 - vert) if upward else vert
                    if not self.f[y][x] and i < len(data) * 8:
                        self.m[y][x] = ((data[i >> 3] >> (7 - (i & 7))) & 1) != 0
                        i += 1
            right -= 2


_MASKS = [
    lambda x, y: (x + y) % 2 == 0,
    lambda x, y: y % 2 == 0,
    lambda x, y: x % 3 == 0,
    lambda x, y: (x + y) % 3 == 0,
    lambda x, y: (x // 3 + y // 2) % 2 == 0,
    lambda x, y: x * y % 2 + x * y % 3 == 0,
    lambda x, y: (x * y % 2 + x * y % 3) % 2 == 0,
    lambda x, y: ((x + y) % 2 + x * y % 3) % 2 == 0,
]


def _penalty(m):
    n = len(m)
    score = 0
    for grid in (m, [list(c) for c in zip(*m)]):
        for row in grid:
            run, prev = 1, row[0]
            for v in row[1:]:
                if v == prev:
                    run += 1
                else:
                    if run >= 5:
                        score += 3 + (run - 5)
                    run, prev = 1, v
            if run >= 5:
                score += 3 + (run - 5)
            s = "".join("1" if v else "0" for v in row)
            score += 40 * (s.count("00001011101") + s.count("10111010000"))
    for y in range(n - 1):
        for x in range(n - 1):
            if m[y][x] == m[y][x + 1] == m[y + 1][x] == m[y + 1][x + 1]:
                score += 3
    dark = sum(v for row in m for v in row)
    total = n * n
    k = (abs(dark * 20 - total * 10) + total - 1) // total - 1
    return score + max(k, 0) * 10


def make_qr(text):
    """คืนเมทริกซ์ list[list[bool]] (True = โมดูลสีเข้ม) ไม่รวมขอบว่าง"""
    ver, data = _encode_data(text)
    mx = _Matrix(ver)
    mx.draw_codewords(_add_ecc_interleave(data, ver))
    best, best_score = None, None
    for mask in range(8):
        m = [row[:] for row in mx.m]
        for y in range(mx.size):
            for x in range(mx.size):
                if not mx.f[y][x] and _MASKS[mask](x, y):
                    m[y][x] = not m[y][x]
        mx._draw_format(m, mask)
        sc = _penalty(m)
        if best_score is None or sc < best_score:
            best, best_score = m, sc
    return best


def qr_svg(text, border=4, dark="#000", light="#fff"):
    m = make_qr(text)
    n = len(m) + 2 * border
    path = "".join("M%d,%dh1v1h-1z" % (x + border, y + border)
                   for y, row in enumerate(m) for x, v in enumerate(row) if v)
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" shape-rendering="crispEdges" '
            'role="img" aria-label="QR Code"><rect width="100%%" height="100%%" fill="%s"/>'
            '<path d="%s" fill="%s"/></svg>' % (n, n, light, path, dark))


def qr_png(text, scale=8, border=4):
    """คืนไบต์ PNG (ใช้ Pillow) สำหรับทดสอบ/ดาวน์โหลด"""
    import io
    from PIL import Image
    m = make_qr(text)
    n = len(m) + 2 * border
    img = Image.new("L", (n, n), 255)
    px = img.load()
    for y, row in enumerate(m):
        for x, v in enumerate(row):
            if v:
                px[x + border, y + border] = 0
    img = img.resize((n * scale, n * scale), Image.NEAREST)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()
