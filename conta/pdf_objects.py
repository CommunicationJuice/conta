"""Minimal PDF object access for the brand scan: find embedded artwork, fingerprint it, extract it.

Illustrator (.ai, PDF-compatible) and PDF files keep placed or pasted artwork as XObjects: Form XObjects
(vector) and Image XObjects (raster). The same logo placed in many of a client's documents shows up as the
same XObject, so a fingerprint seen across documents and jobs marks a brand-mark candidate, and
extract_pdf() writes it out as a standalone one-page PDF (vector stays vector).

No dependencies; tolerant of messy files. Not a general PDF library: no encryption, no xref repair beyond
scanning for "N G obj", object streams (/ObjStm) are read for dictionaries only.
"""
import hashlib, re, zlib

OBJ = re.compile(rb"(\d{1,7})\s+(\d{1,5})\s+obj\b")
REF = re.compile(rb"(\d{1,7})\s+\d{1,5}\s+R\b")
NAME_NUM = re.compile(rb"/[A-Za-z_]+\d+")
PATH_OPS = re.compile(rb"\s[mlcvyh]\s")
SUBPATHS = re.compile(rb"\sm\s")
NUMS = rb"\[\s*(-?[\d.]+)\s+(-?[\d.]+)\s+(-?[\d.]+)\s+(-?[\d.]+)\s*\]"


class Objects:
    """object number -> raw body (bytes between 'obj' and 'endobj'), last definition wins."""

    def __init__(self, data):
        self.data, self.loc, self.extra = data, {}, {}
        for m in OBJ.finditer(data):
            e = data.find(b"endobj", m.end())
            if e > 0:
                self.loc[int(m.group(1))] = (m.end(), e)
        for n in list(self.loc):
            s, e = self.loc[n]
            if b"/ObjStm" in data[s:min(e, s + 400)]:
                self._objstm(n)

    def head(self, n, size=4096):
        """The dictionary part (up to 'stream'), cheap even for big image streams."""
        if n in self.extra:
            return self.extra[n]
        if n not in self.loc:
            return b""
        s, e = self.loc[n]
        h = self.data[s:min(e, s + size)]
        i = h.find(b"stream")
        return h[:i] if i >= 0 else h

    def body(self, n):
        if n in self.extra:
            return self.extra[n]
        s, e = self.loc.get(n, (0, 0))
        return self.data[s:e]

    def stream(self, n):
        """(dict, raw stream bytes) or (dict, None)."""
        b = self.body(n)
        i = b.find(b"stream")
        if i < 0 or n in self.extra:
            return b, None
        j = i + 6
        if b[j:j + 2] == b"\r\n":
            j += 2
        elif b[j:j + 1] in (b"\n", b"\r"):
            j += 1
        k = b.rfind(b"endstream")
        return b[:i], b[j:k if k > j else len(b)].rstrip(b"\r\n")

    def decoded(self, n, cap=8 << 20):
        d, raw = self.stream(n)
        if raw is None:
            return None
        if b"/FlateDecode" in d and not re.search(rb"/(DCT|JPX|JBIG2|CCITTFax|LZW|ASCII85|ASCIIHex)Decode", d):
            try:
                return zlib.decompressobj().decompress(raw, cap)
            except zlib.error:
                return None
        return raw if b"/Filter" not in d else None

    def _objstm(self, n):
        d, raw = self.stream(n)
        try:
            buf = zlib.decompressobj().decompress(raw or b"", 16 << 20)
            first = int(re.search(rb"/First\s+(\d+)", d).group(1))
            cnt = int(re.search(rb"/N\s+(\d+)", d).group(1))
        except (zlib.error, AttributeError, ValueError):
            return
        hdr = buf[:first].split()
        pairs = [(int(hdr[i]), int(hdr[i + 1])) for i in range(0, min(len(hdr) - 1, 2 * cnt), 2)]
        for i, (num, off) in enumerate(pairs):
            end = pairs[i + 1][1] if i + 1 < len(pairs) else len(buf) - first
            if num not in self.loc:
                self.extra[num] = buf[first + off:first + end].strip()


def page_size(data):
    m = re.search(rb"/MediaBox\s*" + NUMS, data[: 64 << 20])
    if not m:
        return None
    x0, y0, x1, y1 = (float(v) for v in m.groups())
    return [round(abs(x1 - x0)), round(abs(y1 - y0))]


def fingerprints(objs, limit=150, page=None):
    """Embedded artwork worth comparing across documents: [{k, h, obj, w, ht, sm, n}]."""
    out, seen = [], set()
    page_area = page[0] * page[1] if page else None
    for n in objs.loc:
        d = objs.head(n)
        if b"/XObject" not in d and b"/Subtype" not in d:
            continue
        if re.search(rb"/Subtype\s*/Form", d):
            bb = re.search(rb"/BBox\s*" + NUMS, d)
            if not bb:
                continue
            x0, y0, x1, y1 = (float(v) for v in bb.groups())
            w, h = abs(x1 - x0), abs(y1 - y0)
            if w < 8 or h < 8 or max(w, h) > 1000:          # specks, or page-sized artwork wrappers
                continue
            if page_area and w * h > 0.5 * page_area:
                continue
            c = objs.decoded(n)
            # a mark has real drawing in it: bullets, rules and single shapes don't
            if not c or len(c) < 1200 or len(c) > (2 << 20) or len(PATH_OPS.findall(c)) < 15:
                continue
            # textures, patterns and illustrations: far more drawing than a mark (logos: < ~50 KB, < 150 shapes)
            if len(c) > 48 * 1024 or len(SUBPATHS.findall(c)) > 150:
                continue
            fp = hashlib.sha256(NAME_NUM.sub(b"/R", c) + f"{round(w)}x{round(h)}".encode()).hexdigest()[:16]
            rec = {"k": "form", "h": fp, "obj": n, "w": round(w), "ht": round(h), "sm": False, "n": len(c)}
        elif re.search(rb"/Subtype\s*/Image", d):
            mw, mh = re.search(rb"/Width\s+(\d+)", d), re.search(rb"/Height\s+(\d+)", d)
            if not mw or not mh or b"/ImageMask true" in d:
                continue
            w, h = int(mw.group(1)), int(mh.group(1))
            if w * h < 48 * 48 or w * h > 6_000_000:          # icons/specks, or photographs
                continue
            _, raw = objs.stream(n)
            if not raw or len(raw) > (12 << 20):
                continue
            sm = re.search(rb"/SMask\s+(\d+)\s+\d+\s+R", d)
            if sm and soft_mask(objs, int(sm.group(1))):
                continue                                 # drop shadow / vignette / glow, not a mark
            fp = hashlib.sha256(raw).hexdigest()[:16]
            rec = {"k": "image", "h": fp, "obj": n, "w": w, "ht": h, "sm": b"/SMask" in d, "n": len(raw),
                   "jpeg": bool(re.search(rb"/(DCT|JPX)Decode", d))}
        else:
            continue
        if fp in seen:
            continue
        seen.add(fp)
        out.append(rec)
    out.sort(key=lambda r: (r["k"] != "form", not r["sm"], -r["n"]))   # vector first, then transparent images
    return out[:limit]


MID_ALPHA = bytes(range(16, 240))


def soft_mask(objs, n, max_soft=0.35):
    """True when an alpha mask is mostly semi-transparent (blurred shadow); a logo's mask is crisp 0/255."""
    a = objs.decoded(n, cap=4 << 20)
    if not a or b"/BitsPerComponent 8" not in objs.head(n):
        return False
    return 1 - len(a.translate(None, MID_ALPHA)) / len(a) > max_soft


def closure(objs, root, cap=3000):
    need, todo = set(), [root]
    while todo and len(need) < cap:
        n = todo.pop()
        if n in need or (n not in objs.loc and n not in objs.extra):
            continue
        need.add(n)
        part = objs.head(n, size=1 << 20) if n in objs.loc else objs.extra[n]
        todo += [int(r) for r in REF.findall(part)]
    return need


def extract_pdf(data, objnum, kind, w, h):
    """A standalone one-page PDF showing XObject `objnum` (form: at its BBox; image: scaled to <= 600 pt)."""
    objs = Objects(data)
    if objnum not in objs.loc:
        return None
    need = closure(objs, objnum)
    top = max(max(objs.loc), max(objs.extra or [0])) + 1
    cat, pages, page, content = top, top + 1, top + 2, top + 3
    if kind == "form":
        bb = re.search(rb"/BBox\s*" + NUMS, objs.head(objnum))
        x0, y0, x1, y1 = (float(v) for v in bb.groups())
        pw, ph = abs(x1 - x0), abs(y1 - y0)
        ops = f"q 1 0 0 1 {-min(x0, x1):.3f} {-min(y0, y1):.3f} cm /X1 Do Q".encode()
    else:
        s = min(1.0, 600.0 / max(w, h))
        pw, ph = w * s, h * s
        ops = f"q {pw:.3f} 0 0 {ph:.3f} 0 0 cm /X1 Do Q".encode()
    out = bytearray(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
    offs = {}

    def put(n, body):
        offs[n] = len(out)
        out.extend(f"{n} 0 obj\n".encode() + body.strip() + b"\nendobj\n")

    for n in sorted(need):
        put(n, objs.body(n))
    put(cat, f"<< /Type /Catalog /Pages {pages} 0 R >>".encode())
    put(pages, f"<< /Type /Pages /Kids [{page} 0 R] /Count 1 >>".encode())
    put(page, (f"<< /Type /Page /Parent {pages} 0 R /MediaBox [0 0 {pw:.3f} {ph:.3f}] "
               f"/Resources << /XObject << /X1 {objnum} 0 R >> >> /Contents {content} 0 R >>").encode())
    put(content, f"<< /Length {len(ops)} >>\nstream\n".encode() + ops + b"\nendstream")
    size = content + 1
    xref = len(out)
    out.extend(f"xref\n0 {size}\n0000000000 65535 f \n".encode())
    for n in range(1, size):
        out.extend(f"{offs[n]:010d} 00000 n \n".encode() if n in offs else b"0000000000 65535 f \n")
    out.extend(f"trailer\n<< /Size {size} /Root {cat} 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return bytes(out)
