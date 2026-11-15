#!/usr/bin/env python3
"""Archive scanner — run on a host on the same network as the archive, never over a slow remote mount.

  python3 nas_scan.py --root /path/to/archive --out ~/nas-scan [--clients ClientA ClientB …]
                      [--max-mb 600] [--since 2018] [--dry-run]

Pure Python 3 stdlib: no Ghostscript, poppler or pip packages needed (macOS: Xcode command-line
tools provide python3). Resumable: a SQLite cache skips files whose size and mtime are unchanged.

For every client folder under --root it reads the DESIGN files and ignores photographs:
  .indd / .ai / .pdf  XMP packet: fonts (family + style), swatches (name, process/spot, CMYK/RGB/LAB),
                      plate names, title, page count
  .ai / .pdf          vector colour operators (fill/stroke CMYK, RGB, grey) in content streams —
                      raster images are XObjects and are never read, so photos don't pollute colours;
                      Separation (spot) names; embedded font names
  .idml               swatches (Graphic.xml) and fonts (Fonts.xml)
  .ase .svg           swatches; logo fills and font families
Photos, video, audio, PSD/TIFF are skipped by extension.

Per client it writes <out>/clients/<folder>/<folder>.nas-scan.json with:
  guideline_books  likely brand manuals (name, title, pages, spots, Pantone mentions), best first
  colours          clustered, counted by DISTINCT FILES, recency-weighted, with CMYK/RGB originals
                   and swatch names
  spots            spot colour names and how many files use them
  fonts            families with styles (Regular/Bold/…) and file counts
With --collect it also copies, into <out>/clients/<folder>/:
  assets/  vector masters of logo candidates and recurring placed graphics (ranked by how many
           documents place them — from the XMP link manifests — plus logo-like names and
           client-supplied/definitive folders), deduplicated by hash
  fonts/   every distinct font file found in the client folders (by sha256), as a CJ-internal
           safeguard copy, split by embedded licence: fonts/open/ (SIL OFL, Apache…) and
           fonts/commercial/ (2026-11-08). Commercial files are a backup only: never copy them
           into jobs or hand them out; activate them on a licensed machine.
CJ imports the output into its private client register (not part of this package).
"""
import argparse, fnmatch, json, math, os, re, shutil, signal, sqlite3, struct, sys, time, zipfile, zlib
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree as ET

DESIGN_EXT = {".indd", ".ai", ".pdf", ".idml", ".ase", ".svg", ".docx", ".pptx"}   # docx/pptx: identity text only
IGNORE_FILE = "_creator-scan-ignore.txt"   # optional, in a client folder: what the scan skips there (.gitignore-like)


def load_ignore(folder):
    """Patterns from <client folder>/_creator-scan-ignore.txt: one per line, # comments, glob (* ? [..]),
    case-insensitive. A name pattern matches at any depth; a leading / anchors it to the client folder
    (a pattern with a / inside is anchored too); a trailing / matches folders only."""
    try:
        lines = (Path(folder) / IGNORE_FILE).read_text(errors="ignore").splitlines()
    except OSError:
        return []
    pats = []
    for l in lines:
        l = l.strip()
        if not l or l.startswith("#"):
            continue
        dir_only = l.endswith("/")
        l = l.rstrip("/")
        anchored = l.startswith("/") or "/" in l
        pats.append((l.lstrip("/").lower(), anchored, dir_only))
    return pats


def ignored(rel, is_dir, pats):
    """rel: path from the client folder, '/'-separated."""
    rel = rel.replace(os.sep, "/").lower()
    name = rel.rsplit("/", 1)[-1]
    return any((is_dir or not d) and fnmatch.fnmatchcase(rel if anc else name, p) for p, anc, d in pats)


SKIP_DIRS = re.compile(r"^(\.|__|node_modules$|\.Trash|#recycle|@eaDir$|Cache|Previews?$|_creator-)", re.I)
SENSITIVE = re.compile(r"cred|password|passwd|codice.?sicurezza|cvv|iban|secret|amministrazione|contratt|fattur|preventiv|bilancio", re.I)
GUIDE = re.compile(r"guideline|linee.?guida|manuale|brand.?book|brandbook|brand.?manual|identity|identit[aà]|"
                   r"corporate|style.?guide|styleguide|immagine.?coordinata|visual.?identity|logo.?(book|manual)", re.I)
RECENCY_HALF_LIFE_Y = 3.0
# Adobe default swatch libraries (every new document carries them): never evidence of a brand
DEFAULT_SWATCH = re.compile(
    r"^(\[?(none|registration|registrazione|paper|carta|black|white|nero|bianco)\]?|"
    r"(cmyk|rgb) (red|yellow|green|cyan|blue|magenta)|(rosso|giallo|verde|cyan|ciano|blu|magenta) (cmyk|rgb)|"
    r"c=\d+ m=\d+ y=\d+ k=\d+|r=\d+ g=\d+ b=\d+|all|tutti)$", re.I)
STYLE_WORDS = r"(regular|roman|book|medium|bold|semibold|demibold|demi|light|thin|extralight|ultralight|black|heavy|" \
              r"extrabold|ultrabold|italic|oblique|obl|it|condensed|cond|cn|narrow|wide|extended|ext|semi|extra|ultra)"


def norm_font(fam, style):
    fam = (fam or "").strip().strip("'\"")
    style = (style or "Regular").strip()
    m = re.match(rf"^(.+?)[-_ ]((?:{STYLE_WORDS})+)$", fam, re.I)
    if m:
        fam, style = m.group(1), m.group(2) if style in ("", "Regular") else style
    key = re.sub(r"(lt|std|pro|mt|ps|ef|com|ot)$", "", re.sub(r"[^a-z0-9]", "", fam.lower()))
    return key, fam, style


# ---------------- XMP ----------------

# The document's own XMP packet. Layout files also carry the packets of placed images, often first in the file:
# reading the first packet gave an .indd a placed photo's dates, tool, fonts and swatches.
OWN_PACKET = {".indd": re.compile(r'CreatorTool(?:>|=")[^<"]*InDesign', re.I),
              ".ai": re.compile(r'CreatorTool(?:>|=")[^<"]*Illustrator', re.I),
              ".pdf": re.compile(r"pdf:Producer", re.I)}


def read_xmp(path, max_bytes, own=None):
    """Stream the file for XMP packets. Returns (xml, is_own): the first packet matching `own` (a regex for the
    document's own packet), else the first packet with is_own False. Without `own`, the first packet."""
    start, end = b"<x:xmpmeta", b"</x:xmpmeta>"
    buf, pos, first = b"", 0, None
    with open(path, "rb") as f:
        while pos < max_bytes:
            chunk = f.read(8 << 20)
            if not chunk:
                break
            pos += len(chunk)
            buf += chunk
            while True:
                i = buf.find(start)
                if i < 0:
                    buf = buf[-64:]
                    break
                j = buf.find(end, i)
                if j < 0:
                    buf = buf[i:] if len(buf) - i <= 32 << 20 else b""   # runaway packet: drop it
                    break
                xml = buf[i:j + len(end)].decode("utf-8", "ignore")
                buf = buf[j + len(end):]
                if own is None or own.search(xml):
                    return xml, True
                first = first or xml
    return first, False


_LEAD_NUM = re.compile(r"\s*(-?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?)")


def num(s):
    """Leading number of s, or 0. InDesign XMP sometimes pads swatch values with NULs/garbage."""
    m = _LEAD_NUM.match(s or "")
    return float(m.group(1)) if m else 0.0


def parse_xmp(xml):
    out = {"fonts": [], "swatches": [], "plates": [], "title": None, "pages": None, "links": []}
    links = re.findall(r'stRef:filePath="([^"]+)"', xml) + re.findall(r"<stRef:filePath>([^<]+)</stRef:filePath>", xml)
    from urllib.parse import unquote
    out["links"] = sorted({re.split(r"[/\\:]", unquote(l))[-1] for l in links if l.strip()})
    fonts = re.findall(r"<stFnt:fontFamily>([^<]+)</stFnt:fontFamily>\s*(?:<stFnt:fontFace>([^<]+)</stFnt:fontFace>)?", xml)
    fonts += re.findall(r'stFnt:fontFamily="([^"]+)"\s+stFnt:fontFace="([^"]*)"', xml)
    out["fonts"] = [(f.strip(), (s or "Regular").strip()) for f, s in fonts]
    for block in re.findall(r"<rdf:li[^>]*>\s*(?:<rdf:Description[^>]*>)?\s*(<xmpG:swatchName>[\s\S]*?)</rdf:li>", xml) + \
                 re.findall(r'(xmpG:swatchName="[^"]*"[^>]*)', xml):
        g = lambda k: (re.search(rf"<xmpG:{k}>([^<]*)</xmpG:{k}>", block) or re.search(rf'xmpG:{k}="([^"]*)"', block) or [None, None])[1]
        sw = {"name": g("swatchName"), "mode": g("mode"), "type": g("type")}
        if sw["mode"] == "CMYK":
            sw["values"] = [num(g(k)) for k in ("cyan", "magenta", "yellow", "black")]
        elif sw["mode"] == "RGB":
            sw["values"] = [num(g(k)) for k in ("red", "green", "blue")]
        elif sw["mode"] == "LAB":
            sw["values"] = [num(g(k)) for k in ("L", "A", "B")]
        if sw["name"]:
            out["swatches"].append(sw)
    plates = re.search(r"<xmpTPg:PlateNames>([\s\S]*?)</xmpTPg:PlateNames>", xml)
    if plates:
        out["plates"] = re.findall(r"<rdf:li>([^<]+)</rdf:li>", plates.group(1))
    t = re.search(r"<dc:title>[\s\S]*?<rdf:li[^>]*>([^<]*)</rdf:li>", xml)
    out["title"] = t.group(1).strip() if t else None
    n = re.search(r"<xmpTPg:NPages>(\d+)</xmpTPg:NPages>", xml) or re.search(r'xmpTPg:NPages="(\d+)"', xml)
    out["pages"] = int(n.group(1)) if n else None
    return out


# ---------------- PDF / AI content ----------------

# One way to match each number, bounded, and it must end at a token boundary. The old
# (-?\d*\.?\d+) could split a digit run many ways; four of them in a row backtracked
# exponentially on hex image data made of long all-digit lines (hung on 0.3 MB PDFs).
NUM = rb"(-?(?:\d{1,12}(?:\.\d{0,12})?|\.\d{1,12}))(?![\d.])"
OPS = {
    "cmyk": re.compile(rb"(?<![\w.])" + NUM + rb"\s+" + NUM + rb"\s+" + NUM + rb"\s+" + NUM + rb"\s+(k|K)\b"),
    "rgb": re.compile(rb"(?<![\w.])" + NUM + rb"\s+" + NUM + rb"\s+" + NUM + rb"\s+(rg|RG)\b"),
    "gray": re.compile(rb"(?<![\w.])" + NUM + rb"\s+(g|G)\b"),
}
STREAM = re.compile(rb"/FlateDecode[^>]*>>\s*stream\r?\n", re.S)


RAW_OPS_BYTES = 8 << 20       # colour-operator regexes run on at most this much of the raw file
STREAM_BUDGET = 64 << 20      # total decompressed content-stream bytes scanned per file
TEXTY = bytes(range(32, 127)) + b"\t\n\r\f"


def looks_binary(raw, sample=4096, max_ratio=0.10):
    """Images, fonts and ICC profiles: too many non-text bytes to be a content stream."""
    s = raw[:sample]
    return bool(s) and len(s.translate(None, TEXTY)) / len(s) > max_ratio


def pdf_content(path, max_bytes, max_streams=400):
    data = open(path, "rb").read(max_bytes)
    colours, fonts, spots = defaultdict(int), set(), set()
    pages = len(re.findall(rb"/Type\s*/Page[^s]", data))
    for m in re.finditer(rb"/Separation\s*/([^\s/\[\]()<>]+)", data):
        spots.add(re.sub(r"#([0-9a-fA-F]{2})", lambda x: chr(int(x.group(1), 16)), m.group(1).decode("latin-1")))
    # The raw file (often hundreds of MB of embedded raster) only gets the cheap literal scans
    # below; the colour-operator regexes see its head plus the decompressed text streams.
    blobs, budget = [data[:RAW_OPS_BYTES]], STREAM_BUDGET
    for i, m in enumerate(STREAM.finditer(data)):
        if i >= max_streams or budget <= 0:
            break
        s = m.end()
        e = data.find(b"endstream", s)
        if e < 0:
            continue
        try:
            raw = zlib.decompressobj().decompress(data[s:e], min(8 << 20, budget))
        except zlib.error:
            continue
        # skip binary payloads (images, fonts): content streams are mostly ASCII operators
        if raw[:200].count(b"\x00") > 5 or looks_binary(raw):
            continue
        budget -= len(raw)
        blobs.append(raw)
    pantone = len(re.findall(rb"PANTONE|Pantone", data))
    for m in re.finditer(rb"/(?:BaseFont|FontName)\s*/([A-Za-z0-9+\-_,.]+)", data):
        fonts.add(re.sub(r"^[A-Z]{6}\+", "", m.group(1).decode("latin-1")))
    for j, b in enumerate(blobs):
        if j:  # blobs[0] is a slice of data, already covered by the literal scans above
            pantone += len(re.findall(rb"PANTONE|Pantone", b))
            for m in re.finditer(rb"/(?:BaseFont|FontName)\s*/([A-Za-z0-9+\-_,.]+)", b):
                fonts.add(re.sub(r"^[A-Z]{6}\+", "", m.group(1).decode("latin-1")))
            for m in re.finditer(rb"/Separation\s*/([^\s/\[\]()<>]+)", b):
                spots.add(re.sub(r"#([0-9a-fA-F]{2})", lambda x: chr(int(x.group(1), 16)), m.group(1).decode("latin-1")))
        for kind, rx in OPS.items():
            for m in rx.finditer(b):
                try:
                    vals = tuple(round(float(x), 2) for x in m.groups()[:-1])
                except ValueError:
                    continue
                if all(0 <= v <= 1 for v in vals):
                    colours[(kind, vals)] += 1
    xobjects, page_pt = [], None
    try:   # embedded artwork fingerprints (logo candidates placed or pasted inside layouts)
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import pdf_objects
        page_pt = pdf_objects.page_size(data)
        xobjects = pdf_objects.fingerprints(pdf_objects.Objects(data), page=page_pt)
    except Exception:
        pass
    try:
        import brand_meta
        text = brand_meta.pdf_text(data, blobs[1:])
    except Exception:
        text = ""
    return {"colours": colours, "fonts": fonts, "spots": spots, "pages": pages, "pantone_mentions": pantone,
            "xobjects": xobjects, "page_pt": page_pt, "text": text}


# ---------------- IDML / ASE / SVG ----------------

def idml_content(path):
    out = {"swatches": [], "fonts": []}
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        if "Resources/Graphic.xml" in names:
            root = ET.fromstring(z.read("Resources/Graphic.xml"))
            for c in root.iter("Color"):
                vals = [num(x) for x in (c.get("ColorValue") or "").split()]
                out["swatches"].append({"name": c.get("Name"), "mode": c.get("Space"), "type": c.get("Model"),
                                        "values": [v / 100 if c.get("Space") == "CMYK" else v for v in vals]})
        if "Resources/Fonts.xml" in names:
            root = ET.fromstring(z.read("Resources/Fonts.xml"))
            for f in root.iter("Font"):
                out["fonts"].append((f.get("FontFamily"), f.get("FontStyleName") or "Regular"))
    return out


def ase_content(path):
    sw, b = [], open(path, "rb").read()
    if b[:4] != b"ASEF":
        return sw
    n, i = struct.unpack(">I", b[8:12])[0], 12
    for _ in range(n):
        bt, bl = struct.unpack(">HI", b[i:i + 6]); i += 6
        blk = b[i:i + bl]; i += bl
        if bt != 1:
            continue
        nl = struct.unpack(">H", blk[:2])[0]
        name = blk[2:2 + nl * 2].decode("utf-16-be").rstrip("\x00"); j = 2 + nl * 2
        model = blk[j:j + 4].decode(); j += 4
        cnt = {"RGB ": 3, "CMYK": 4, "Gray": 1, "LAB ": 3}.get(model)
        if cnt:
            vals = list(struct.unpack(f">{cnt}f", blk[j:j + 4 * cnt]))
            sw.append({"name": name, "mode": model.strip().upper(), "values": vals})
    return sw


def svg_content(path):
    t = open(path, "r", errors="ignore").read(4 << 20)
    cols = re.findall(r"(?:fill|stroke|stop-color)\s*[:=]\s*['\"]?#([0-9a-fA-F]{6}|[0-9a-fA-F]{3})\b", t)
    fonts = re.findall(r"font-family\s*[:=]\s*['\"]?([^;'\",]+)", t)
    return cols, fonts


# ---------------- font files ----------------

FONT_EXT = {".otf", ".ttf", ".ttc", ".otc", ".dfont", ".pfb", ".pfm", ".woff", ".woff2"}
OPEN_LICENCE = re.compile(r"SIL Open Font|OFL|scripts\.sil\.org|openfontlicense|Apache License|apache\.org/licenses|"
                          r"Ubuntu Font Licence|Bitstream Vera|public domain", re.I)


def font_names(path):
    """Read the OpenType 'name' table: family, style, maker, licence text, licence URL."""
    b = open(path, "rb").read(4 << 20)
    off = 12 * 0
    if b[:4] == b"ttcf":
        off = struct.unpack(">I", b[12:16])[0]
    num = struct.unpack(">H", b[off + 4:off + 6])[0]
    tbl = None
    for i in range(num):
        tag, _, o, ln = struct.unpack(">4sIII", b[off + 12 + 16 * i: off + 28 + 16 * i])
        if tag == b"name":
            tbl = (o, ln)
    if not tbl:
        return {}
    o = tbl[0]
    _, count, soff = struct.unpack(">HHH", b[o:o + 6])
    out = {}
    for i in range(count):
        pid, eid, lid, nid, ln, so = struct.unpack(">HHHHHH", b[o + 6 + 12 * i: o + 18 + 12 * i])
        if nid not in (1, 2, 8, 13, 14, 16, 17) or nid in out:
            continue
        raw = b[o + soff + so: o + soff + so + ln]
        try:
            txt = raw.decode("utf-16-be") if pid in (0, 3) else raw.decode("latin-1")
        except UnicodeDecodeError:
            continue
        if pid == 3 and lid not in (0x409, 0x410) and nid in out:
            continue
        out[nid] = txt.strip()
    fam = out.get(16) or out.get(1)
    sty = out.get(17) or out.get(2)
    lic = " ".join(filter(None, [out.get(13), out.get(14)]))
    return {"family": fam, "style": sty, "maker": out.get(8), "licence": lic[:300],
            "open_licence": bool(OPEN_LICENCE.search(lic))}


# ---------------- colour helpers ----------------

def cmyk_to_rgb(c, m, y, k):
    return tuple(round(255 * (1 - v) * (1 - k)) for v in (c, m, y))


def to_hex(rgb):
    return "#%02x%02x%02x" % tuple(max(0, min(255, int(round(v)))) for v in rgb)


def lab_to_rgb(L, a, b):
    fy = (L + 16) / 116; fx = fy + a / 500; fz = fy - b / 200
    f = lambda t: t ** 3 if t ** 3 > 216 / 24389 else (116 * t - 16) / (24389 / 27)
    x, y, z = 0.95047 * f(fx), (fy ** 3 if L > 8 else L / (24389 / 27)), 1.08883 * f(fz)
    r, g, bl = 3.2404542 * x - 1.5371385 * y - 0.4985314 * z, -0.9692660 * x + 1.8760108 * y + 0.0415560 * z, 0.0556434 * x - 0.2040259 * y + 1.0572252 * z
    gam = lambda c: 255 * (12.92 * c if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055)
    return tuple(gam(max(0, min(1, c))) for c in (r, g, bl))


def swatch_hex(sw):
    v, m = sw.get("values") or [], (sw.get("mode") or "").upper()
    try:
        if m == "CMYK" and len(v) == 4:
            v = [x / 100 if x > 1 else x for x in v]
            return to_hex(cmyk_to_rgb(*v)), ("cmyk", [round(x * 100) for x in v])
        if m == "RGB" and len(v) == 3:
            return to_hex([x if max(v) > 1 else x * 255 for x in v]), ("rgb", v)
        if m == "LAB" and len(v) == 3:
            return to_hex(lab_to_rgb(*v)), ("lab", v)
        if m == "GRAY" and len(v) == 1:
            return to_hex([v[0] * 255] * 3), ("gray", v)
    except Exception:
        pass
    return None, None


# ---------------- per-file extraction with cache ----------------

def extract(path, ext, max_bytes):
    r = {"fonts": [], "swatches": [], "spots": [], "colours": {}, "title": None, "pages": None, "pantone": 0, "links": []}
    xml, text = None, ""
    if ext in (".indd", ".ai", ".pdf"):
        xml, xmp_own = read_xmp(path, max_bytes, OWN_PACKET.get(ext))
        if xml:
            x = parse_xmp(xml)
            r["fonts"] += x["fonts"]; r["swatches"] += x["swatches"]; r["title"] = x["title"]; r["pages"] = x["pages"]
            r["links"] = x["links"]
            r["spots"] += [p for p in x["plates"] if p not in ("Cyan", "Magenta", "Yellow", "Black")]
    if ext in (".ai", ".pdf"):
        c = pdf_content(path, max_bytes)
        r["fonts"] += [(re.split(r"[-,]", f)[0], (re.split(r"[-,]", f) + ["Regular"])[1]) for f in c["fonts"]]
        r["spots"] += sorted(c["spots"])
        r["pages"] = r["pages"] or c["pages"]; r["pantone"] = c["pantone_mentions"]
        r["xobjects"], r["page_pt"] = c["xobjects"], c["page_pt"]
        text = c.get("text", "")
        for (kind, vals), n in c["colours"].items():
            if kind == "cmyk":
                hx = to_hex(cmyk_to_rgb(*vals)); orig = ["cmyk", [round(v * 100) for v in vals]]
            elif kind == "rgb":
                hx = to_hex([v * 255 for v in vals]); orig = ["rgb", [round(v * 255) for v in vals]]
            else:
                hx = to_hex([vals[0] * 255] * 3); orig = ["gray", [round(vals[0] * 100)]]
            e = r["colours"].setdefault(hx, {"n": 0, "orig": orig}); e["n"] += n
    elif ext == ".idml":
        x = idml_content(path); r["fonts"] += x["fonts"]; r["swatches"] += x["swatches"]
    elif ext == ".ase":
        r["swatches"] += ase_content(path)
    elif ext == ".svg":
        cols, fonts = svg_content(path)
        for h in cols:
            h = "#" + (h if len(h) == 6 else "".join(ch * 2 for ch in h)).lower()
            e = r["colours"].setdefault(h, {"n": 0, "orig": ["rgb-hex", h]}); e["n"] += 1
        r["fonts"] += [(f.strip(), "Regular") for f in fonts]
    r["spots"] = sorted(set(r["spots"]))
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import brand_meta
        r["meta"] = brand_meta.file_meta(path, ext, xml, text)
        if ext in OWN_PACKET:
            r["meta"]["xmp_own"] = bool(xml) and xmp_own
        if r.get("page_pt") and not r["meta"].get("page_mm"):
            r["meta"]["page_mm"] = [round(v * 25.4 / 72) for v in r["page_pt"][:2]]
    except Exception as e:
        r["meta"] = {"error": str(e)[:120]}
    r["v"] = RESULT_VERSION
    return r


class FileTimeout(Exception):
    pass


def _alarm(signum, frame):
    raise FileTimeout()


def cache_db(path):
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE IF NOT EXISTS f (path TEXT PRIMARY KEY, size INT, mtime REAL, result TEXT)")
    return db


GONE_KEEP_DAYS = 30   # cache rows of a vanished folder stay this long, so a move or rename reuses them


def prune_vanished(out, db, live, log_to=None):
    """A top-level folder that is no longer under the root (renamed, archived, deleted) loses its previous scan
    output, so it is not published again under its old name. Its cache rows stay for GONE_KEEP_DAYS: a folder
    moved to an archive share or renamed is then recognised file by file instead of
    re-read (see MovedIndex). Guard: if the root lists no folders or fewer than half of those scanned before, the
    share is probably not mounted properly; prune nothing."""
    live = {safe_folder(n) for n in live}      # output folders carry the sanitised name
    db.execute("CREATE TABLE IF NOT EXISTS gone (folder TEXT PRIMARY KEY, since REAL)")
    for (name,) in db.execute("SELECT folder FROM gone").fetchall():
        if name in live:
            db.execute("DELETE FROM gone WHERE folder = ?", (name,))
    old = {n for (n,) in db.execute("SELECT folder FROM gone WHERE since < ?", (time.time() - GONE_KEEP_DAYS * 86400,))}
    if old:
        stale = [(p,) for (p,) in db.execute("SELECT path FROM f") if safe_folder(p.split("/", 1)[0]) in old]
        db.executemany("DELETE FROM f WHERE path = ?", stale)
        db.executemany("DELETE FROM gone WHERE folder = ?", [(n,) for n in old])
    db.commit()
    prev = {d.name for d in (out / "clients").iterdir() if d.is_dir()} if (out / "clients").is_dir() else set()
    gone = sorted(prev - live)
    if not gone:
        return []
    if not live or len(prev & live) < len(prev) / 2:
        print(f"prune: skipped, {len(gone)} of {len(prev)} folders missing under the root (mount problem?)", file=log_to)
        return []
    for name in gone:
        shutil.rmtree(out / "clients" / name, ignore_errors=True)
        db.execute("INSERT OR IGNORE INTO gone VALUES (?, ?)", (name, time.time()))
    db.commit()
    print(f"prune: removed scan output for folder(s) no longer under the root: {', '.join(gone)}", file=log_to)
    return gone


RULES_FILES = ("cj-regole.txt", "cj-rules.txt")   # matched case-insensitively at the top of a client folder
RULE_TAGS = {"IGNORE", "RISERVATO", "NDA", "PROPOSTA"}       # plus DISPLAYNAME=<name>


def read_rules(folder):
    """Rule tags a person dropped into the folder (2026-11-12): CJ-REGOLE.txt, one per line, '#' comments.
    Returns e.g. ["NDA", "DISPLAYNAME=Client Name"]; unknown lines are kept as "?<line>" so the export can report them."""
    try:
        f = next((p for p in folder.iterdir() if p.is_file() and p.name.lower() in RULES_FILES), None)
        lines = f.read_text(encoding="utf-8", errors="replace").splitlines() if f else []
    except OSError:
        return []
    out = []
    for raw in lines:
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        key, eq, val = line.partition("=")
        key = key.strip().upper()
        if key == "DISPLAYNAME" and eq and val.strip():
            out.append(f"DISPLAYNAME={val.strip()[:120]}")
        elif key in RULE_TAGS and not eq:
            out.append(key)
        else:
            out.append(f"?{line[:120]}")
    return out


def safe_folder(name):
    """The output folder name for a top-level folder (clients/<safe>/)."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name)


class MovedIndex:
    """Cached results found by where a file sits inside its client folder, its size and its whole-second mtime,
    across this cache and peer caches (the other share's). A folder moved between Lavori and Archivio (rsync -a,
    times kept) or renamed is recognised file by file and not re-read. Peer caches are opened read-only."""

    def __init__(self, db, peers=()):
        self.dbs = [db] + [sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=10) for p in peers if Path(p).is_file()]
        self.idx = {}
        for d in self.dbs:
            try:
                for path, size, mtime in d.execute("SELECT path, size, mtime FROM f"):
                    rest = path.split("/", 1)[1] if "/" in path else None
                    if rest:
                        self.idx.setdefault((rest, size, int(mtime)), (d, path))
            except sqlite3.Error:
                continue
        self.hits = 0

    def get(self, rel, size, mtime):
        rest = rel.split("/", 1)[1] if "/" in rel else None
        hit = self.idx.get((rest, size, int(mtime))) if rest else None
        if not hit:
            return None
        d, path = hit
        if path == rel and d is self.dbs[0]:
            return None   # same row: already judged stale by the caller
        try:
            row = d.execute("SELECT result FROM f WHERE path=?", (path,)).fetchone()
        except sqlite3.Error:   # peer busy (the other pass writing): just read the file
            return None
        r = json.loads(row[0]) if row and row[0] else None
        if not r or r.get("v", 1) < RESULT_VERSION:
            return None
        self.hits += 1
        return r


# ---------------- aggregation ----------------

LOGO_NAME = re.compile(r"(?<![a-z])logo|marchio|lockup|logotip|pittogramm|simbolo|symbol|emblem|(?<![a-z])brand|monogram|sigillo|\\bseal\\b", re.I)
VECTOR_EXT = {".ai", ".eps", ".svg", ".pdf"}
EMBEDDED_IMAGES = False   # needs a visual check to tell the client's mark from a parent brand's; fingerprints stay cached
RESULT_VERSION = 8    # bump when extract() output changes: cached results older than this are re-read (all types)
RASTER_EXT = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".psd", ".webp", ".gif", ".bmp", ".heic"}
NOISE_NAME = re.compile(r"schermata|screenshot|screen.?shot|bildschirmfoto|captura|whatsapp|^img[_-]?\d|^dsc|^_?mg_|^dji_|^pxl_|"
                        r"^p\d{6,}|^\d+\.|^[a-z0-9]{6}\.(tif|jpe?g|png)$|adobestock|shutterstock|istock|getty|unsplash|pexels|"
                        r"freepik|depositphotos|mock.?up|fustell|die.?line|stanz|cutter|troquel|qr.?code|barcode|ean\d*|"
                        r"placeholder|segnaposto|^untitled|^senza.?titolo|^copia|^copy of", re.I)
PARTNER_NAME = re.compile(r"partner|sponsor|patrocin|in collaborazione|co.?brand|regione|comune|provincia|ministero|"
                          r"governo|unione.?europea|\beu\b|\bue\b|fesr|\bfse\b|pnrr|por.?fesr|camera.?di.?commercio|cciaa|"
                          r"\bfsc\b|pefc|\bce\b|iso.?\d{3,5}|certific|ecolabel|facebook|instagram|linkedin|youtube|tiktok|"
                          r"whatsapp|twitter|pinterest|app.?store|google.?play|play.?store|visa|mastercard|paypal|amex|"
                          r"tripadvisor|booking\.?com|airbnb|expedia|michelin|gambero", re.I)
GUIDE_DOC = re.compile(r"brand.?book|brand.?positioning|brand.?manual|guideline|linee.?guida|manuale|presentazion|"
                       r"proposta|propos|catalog|brochure|listino|company.?profile", re.I)
SMALL_FORMAT_PRINT = re.compile(r"bigliett|business.?card|visit|etichett|label|badge|cartellin|segnaposto|coupon|"
                                r"ticket|bigliett|invit|flyer|volantin|cartolin|timbro|adesiv|sticker|tag\b|pass\b", re.I)
CLIENT_STOP = {"srl", "spa", "snc", "sas", "the", "and", "group", "gruppo", "italia", "italy", "hotel", "studio", "srls",
               "societa", "azienda", "agricola", "cantina", "cantine", "di", "del", "della", "dei", "per", "con", "academy",
               "project", "progetto", "sito", "web", "cj", "lavori", "new", "nuovo", "old"}


def client_tokens(folder):
    """Distinctive words of a client folder name (to tell own marks from other clients' and partners')."""
    words = re.sub(r"[^a-z0-9]+", " ", folder.lower()).split()
    return {w for w in words if len(w) >= 3 and w not in CLIENT_STOP and not w.isdigit()}


PRIORITY_DIR = re.compile(r"originali|dal.?cliente|logo_def|definitiv|master|brand|identity|marchio", re.I)


def aggregate(folder, files, inventory=None, font_files=None, all_folders=()):
    now = time.time()
    inventory = inventory or {}
    placed = defaultdict(set)
    for rel, mtime, r in files:
        for l in r.get("links", []):
            placed[l.lower()].add(rel)
    colours = defaultdict(lambda: {"files": 0, "weight": 0.0, "ops": 0, "orig": None, "names": set()})
    spots, fonts, books = defaultdict(set), defaultdict(lambda: {"files": set(), "styles": defaultdict(int)}), []
    for rel, mtime, r in files:
        rec = 0.5 ** (((now - mtime) / 31557600) / RECENCY_HALF_LIFE_Y)
        for hx, e in r["colours"].items():
            c = colours[hx]; c["files"] += 1; c["weight"] += rec; c["ops"] += e["n"]; c["orig"] = c["orig"] or e["orig"]
        used = set(r["colours"])
        for sw in r["swatches"]:
            nm = (sw.get("name") or "").strip()
            hx, orig = swatch_hex(sw)
            if DEFAULT_SWATCH.match(nm) and hx not in used:
                continue
            if hx:
                c = colours[hx]; c["files"] += 1; c["weight"] += 2 * rec; c["orig"] = c["orig"] or orig
                if nm and not DEFAULT_SWATCH.match(nm):
                    c["names"].add(nm)
            if (sw.get("type") or "").upper() == "SPOT" and nm and not DEFAULT_SWATCH.match(nm):
                spots[nm].add(rel)
        for sp in r["spots"]:
            if not DEFAULT_SWATCH.match(sp):
                spots[sp].add(rel)
        for fam, style in r["fonts"]:
            if not fam:
                continue
            key, disp, sty = norm_font(fam, style)
            if not key:
                continue
            f = fonts[key]; f["files"].add(rel); f["styles"][sty] += 1
            f.setdefault("names", defaultdict(int))[disp] += 1
        score = (3 if GUIDE.search(rel) else 0) + (2 if r["title"] and GUIDE.search(r["title"]) else 0) + \
                (1 if (r["pages"] or 0) >= 8 else 0) + (1 if r["spots"] else 0) + (1 if r["pantone"] else 0) + \
                (0.5 if len({f for f, _ in r["fonts"]}) >= 2 else 0)
        if score >= 3:
            books.append({"path": rel, "score": score, "title": r["title"], "pages": r["pages"],
                          "spots": r["spots"][:6], "pantone_mentions": r["pantone"]})
    # tangible assets: logo candidates and recurring graphics -- placed files, embedded artwork, small artboards
    own, others = client_tokens(folder), set()
    for f_ in all_folders:
        if f_ != folder:
            others |= client_tokens(f_)
    others -= own
    job = lambda rel: rel.split("/")[1] if rel.count("/") >= 2 else ""
    client_jobs = len({job(rel) for rel, _, _ in files})
    many_jobs = client_jobs >= 4      # only then is "seen in one job only" a sign of a third party's mark
    stem_of = lambda rel: re.sub(r"(\s*(copia|copy|backup).*|[\s_\-]*(v|rev|def|ok|final[e]?|stampa|print|web|hd|low)?[\s_\-]*\(?\d*\)?)$", "",
                                 os.path.splitext(os.path.basename(rel).lower())[0], flags=re.I)
    assets, rejected = [], []

    def role_of(name_l, ext):
        stem = re.sub(r"[^a-z0-9]+", " ", os.path.splitext(name_l)[0])
        words = set(stem.split())
        if NOISE_NAME.search(name_l) or (ext in RASTER_EXT and not LOGO_NAME.search(name_l)):
            return "noise"       # photos and screenshots, even when a client word (e.g. "bianco") is in the name
        if own & words or any(t in stem.replace(" ", "") for t in own if len(t) >= 5):
            return "own"
        if PARTNER_NAME.search(name_l) or (others & words) or any(t in stem.replace(" ", "") for t in others if len(t) >= 6):
            return "partner"
        if ext in RASTER_EXT and not LOGO_NAME.search(name_l):
            return "noise"
        return "neutral"

    def add(a_):
        (rejected if a_["role"] in ("noise", "partner") else assets).append(a_)

    for name_l, docs in placed.items():
        ext = os.path.splitext(name_l)[1]
        jobs = {job(d) for d in docs}
        add({"name": name_l, "kind": "placed-file", "placed_in": len(docs), "jobs": len(jobs),
             "found_at": inventory.get(name_l, [])[:3], "vector": ext in VECTOR_EXT,
             "logo_name": bool(LOGO_NAME.search(name_l)), "role": role_of(name_l, ext)})
    for name_l, locs in inventory.items():
        ext = os.path.splitext(name_l)[1]
        if LOGO_NAME.search(name_l) and ext in VECTOR_EXT and name_l not in placed and not GUIDE_DOC.search(name_l):
            add({"name": name_l, "kind": "logo-file", "placed_in": 0, "jobs": len({job(l) for l in locs}),
                 "found_at": locs[:3], "vector": True, "logo_name": True, "role": role_of(name_l, ext)})
    # small one-page vector artboards: logo masters often have no "logo" in the name
    names_seen = {a_["name"] for a_ in assets + rejected}
    for rel, mtime, r in files:
        pp, nm = r.get("page_pt"), os.path.basename(rel).lower()
        if pp and max(pp) <= 300 and (r.get("pages") or 1) <= 2 and nm not in names_seen \
                and os.path.splitext(nm)[1] in (".ai", ".pdf") and not SMALL_FORMAT_PRINT.search(rel) \
                and (LOGO_NAME.search(nm) or role_of(nm, ".ai") == "own" or placed.get(nm)):
            names_seen.add(nm)
            add({"name": nm, "kind": "small-artboard", "placed_in": len(placed.get(nm, ())), "jobs": 1,
                 "found_at": [rel], "vector": True, "logo_name": bool(LOGO_NAME.search(nm)), "role": role_of(nm, ".ai")})
    # embedded artwork recurring across documents and jobs (logos pasted or embedded in layouts)
    emb = {}
    for rel, mtime, r in files:
        for x in r.get("xobjects", []):
            if x["k"] == "image" and not EMBEDDED_IMAGES:
                continue      # recurring embedded rasters are mostly customers'/partners' logos and photos (tested 2026-11-08)
            if x["k"] == "image" and not x["sm"] and (x.get("jpeg", True) or max(x["w"], x["ht"]) > 1200):
                continue      # opaque JPEG or large raster: photography (mood images recur across decks too)
            e = emb.setdefault(x["h"], {"x": x, "src": rel, "docs": set(), "jobs": defaultdict(int)})
            if stem_of(rel) not in e["docs"]:    # the same document as .ai + .pdf, or a backup copy, counts once
                e["docs"].add(stem_of(rel)); e["jobs"][job(rel)] += 1
    n_docs_x = max(1, sum(1 for _, _, r in files if r.get("xobjects")))
    for h_, e in emb.items():
        nd, nj = len(e["docs"]), len(e["jobs"])
        if nd < 3 or (nj < 2 and many_jobs):
            continue
        x = e["x"]
        conc = max(e["jobs"].values()) / nd      # share of its documents inside one job
        a_ = {"name": f"embedded-{x['k']}-{h_[:12]}.pdf", "kind": f"embedded-{x['k']}", "placed_in": nd, "jobs": nj,
              "found_at": [e["src"]], "vector": x["k"] == "form", "logo_name": False, "hash": h_,
              "src_obj": x["obj"], "w": x["w"], "ht": x["ht"], "coverage": round(nd / n_docs_x, 2),
              "role": "partner" if (many_jobs and conc >= 0.7) else "neutral"}   # one campaign's or customer's marks
        add(a_)
    for a_ in assets:
        a_["score"] = (2 * min(a_["jobs"], 10) + min(a_["placed_in"], 20) + (6 if a_["logo_name"] else 0) +
                       (8 if a_["role"] == "own" else 0) + (2 if a_["vector"] else 0) +
                       (4 if a_["kind"] == "embedded-form" else 0) + (3 if a_["kind"] == "small-artboard" else 0) +
                       (3 if any(PRIORITY_DIR.search(x) for x in a_["found_at"]) else 0))
        if a_["role"] == "neutral" and a_["jobs"] < 2 and a_["kind"] in ("placed-file", "logo-file") and many_jobs \
                and not any(PRIORITY_DIR.search(x) for x in a_["found_at"]):
            a_["score"] = min(a_["score"], 5)    # a mark from one job of a many-job client: likely a third party's
    assets.sort(key=lambda a_: -a_["score"])
    rejected.sort(key=lambda a_: -a_["placed_in"])
    fonts_found = []
    seen_f = set()
    for rel, meta in (font_files or []):
        k = ((meta.get("family") or "").lower(), (meta.get("style") or "").lower())
        if not meta.get("family") or k in seen_f:
            continue
        seen_f.add(k)
        fonts_found.append(dict(meta, path=rel))
    n_files = max(1, len(files))
    min_files = 2 if n_files >= 10 else 1
    col_out = [{"hex": h, "files": c["files"], "weight": round(c["weight"], 2), "ops": c["ops"],
                "original": c["orig"], "swatch_names": sorted(c["names"])[:5]}
               for h, c in colours.items() if c["files"] >= min_files or c["names"]]
    col_out.sort(key=lambda c: (-c["weight"], -c["ops"]))
    return {
        "folder": folder, "scanned": datetime.now().isoformat(timespec="seconds"), "design_files": len(files),
        "timed_out": [rel for rel, _, r in files if r.get("timeout")],
        "guideline_books": sorted(books, key=lambda b: -b["score"])[:15],
        "colours": col_out[:60],
        "spots": sorted(({"name": k, "files": len(v)} for k, v in spots.items()), key=lambda s: -s["files"])[:30],
        "assets": [a_ for a_ in assets if a_["score"] >= 6][:40],
        "assets_rejected": {"count": len(rejected), "top": [{"name": a_["name"], "role": a_["role"], "kind": a_["kind"],
                                                             "placed_in": a_["placed_in"]} for a_ in rejected[:25]]},
        "font_files": fonts_found[:80],
        "font_files_all": [dict(meta, path=rel) for rel, meta in (font_files or [])],
        "fonts": sorted(({"family": max(v["names"], key=v["names"].get), "files": len(v["files"]), "styles": dict(v["styles"])}
                         for k, v in fonts.items()),
                        key=lambda f: -f["files"])[:30],
    }


def collect(root, out, name, res, a):
    """Copy vector logo/asset masters and every distinct font file, tagged open/commercial by licence."""
    import hashlib, shutil
    dst = out / "clients" / name
    dst.mkdir(parents=True, exist_ok=True)               # a folder with nothing to copy still gets its scan JSON
    shutil.rmtree(dst / "assets", ignore_errors=True)    # rankings change: keep only tonight's picks
    seen, n_assets, n_fonts = set(), 0, 0
    for asset in res["assets"]:        # every kept asset gets a hash (cross-client check); the top ones are copied
        kind = asset.get("kind", "")
        copy = n_assets < a.max_assets
        if not (asset["vector"] or kind.startswith("embedded") or asset.get("role") == "own" or asset.get("logo_name")):
            continue
        src = root / asset["found_at"][0] if asset.get("found_at") else None
        if src is None:
            continue
        try:
            if kind.startswith("embedded") and not copy:
                asset["sha12"] = asset["hash"][:12]
                continue
            if kind.startswith("embedded"):
                sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
                import pdf_objects
                blob = pdf_objects.extract_pdf(src.read_bytes()[: int(a.max_mb * 1e6)], asset["src_obj"],
                                               kind.split("-", 1)[1], asset["w"], asset["ht"])
                if not blob:
                    continue
                h, out_name = asset["hash"][:12], asset["name"]
            else:
                if src.stat().st_size > a.max_asset_mb * 1e6:
                    continue
                blob = None
                h, out_name = hashlib.sha256(src.read_bytes()).hexdigest()[:12], src.name
        except Exception:
            continue
        asset["sha12"] = h
        if h in seen or not copy:
            continue
        seen.add(h)
        (dst / "assets").mkdir(parents=True, exist_ok=True)
        target = dst / "assets" / f"{h}-{out_name}"
        if blob is not None:
            target.write_bytes(blob)
        else:
            shutil.copy2(src, target)
        asset["collected"], asset["sha12"] = f"assets/{h}-{out_name}", h
        n_assets += 1
    by_path, n_open = {}, 0
    for ff in res.get("font_files_all", []):     # every distinct font file, licence-tagged (safeguard copy)
        src = root / ff["path"]
        try:
            data = src.read_bytes()
        except OSError:
            continue
        ff["sha256"] = h = hashlib.sha256(data).hexdigest()
        sub = "open" if ff.get("open_licence") else "commercial"
        rel_c = f"fonts/{sub}/{h[:12]}-{src.name}"
        ff["collected"] = rel_c
        by_path[ff["path"]] = (rel_c, h)
        if h in seen:
            continue
        seen.add(h)
        (dst / "fonts" / sub).mkdir(parents=True, exist_ok=True)
        if not (dst / rel_c).exists():
            (dst / rel_c).write_bytes(data)
            shutil.copystat(src, dst / rel_c)
        n_fonts += 1; n_open += sub == "open"
    for ff in res["font_files"]:
        ff["collected"], ff["sha256"] = by_path.get(ff["path"], (None, None))
    (dst / f"{name}.nas-scan.json").write_text(json.dumps(res, indent=1, ensure_ascii=False))
    print(f"   collected {n_assets} vector asset(s), {n_fonts} font file(s) ({n_open} open-licence)")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", required=True, help="e.g. /path/to/archive (mounted on the scanning host)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--clients", nargs="*", help="only these top-level folders")
    ap.add_argument("--max-mb", type=float, default=600, help="read at most this many MB per file")
    ap.add_argument("--since", type=int, help="skip files last modified before this year")
    ap.add_argument("--dry-run", action="store_true", help="count design files per client, read nothing")
    ap.add_argument("--peer-cache", nargs="*", default=[],
                    help="other scans' cache.sqlite (read-only): reuse results for folders moved or renamed")
    ap.add_argument("--collect", action="store_true", help="copy vector logo/asset masters and open-licence fonts")
    ap.add_argument("--max-assets", type=int, default=25); ap.add_argument("--max-asset-mb", type=float, default=25)
    ap.add_argument("--file-timeout", type=int, default=120,
                    help="seconds per file; a file that takes longer is logged as TIMEOUT, cached and skipped")
    a = ap.parse_args()
    root, out = Path(a.root).expanduser(), Path(a.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    db = cache_db(out / "cache.sqlite")
    folders = all_folders = sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith((".", "_", "#")))
    if a.clients:
        want = {c.lower() for c in a.clients}
        folders = [p for p in folders if p.name.lower() in want]
    elif not a.dry_run:
        prune_vanished(out, db, {p.name for p in all_folders}, log_to=sys.stdout)
    moved = MovedIndex(db, a.peer_cache) if not a.dry_run else None
    since = datetime(a.since, 1, 1).timestamp() if a.since else 0
    max_bytes = int(a.max_mb * 1e6)
    log = open(out / "scan.log", "a")
    signal.signal(signal.SIGALRM, _alarm)
    for folder in folders:
        t0, files, seen = time.time(), [], 0
        hits0 = moved.hits if moved else 0
        inventory, font_files = defaultdict(list), []
        pats = load_ignore(folder)
        if pats:
            log.write(f"{datetime.now():%F %T} {folder.name}: {len(pats)} ignore pattern(s) from {IGNORE_FILE}\n")
        for dp, dns, fns in os.walk(folder):
            here = os.path.relpath(dp, folder)
            here = "" if here == "." else here + "/"
            dns[:] = [d for d in dns if not SKIP_DIRS.search(d) and not SENSITIVE.search(d)
                      and not (pats and ignored(here + d, True, pats))]
            for fn in fns:
                ext = os.path.splitext(fn)[1].lower()
                if fn.startswith("._") or (pats and ignored(here + fn, False, pats)):
                    continue
                relf = os.path.relpath(os.path.join(dp, fn), root)
                if ext in VECTOR_EXT or ext in (".png", ".tif", ".tiff", ".psd", ".jpg"):
                    if len(inventory[fn.lower()]) < 5:
                        inventory[fn.lower()].append(relf)
                if ext in FONT_EXT and not a.dry_run:
                    try:
                        meta = font_names(os.path.join(dp, fn))
                    except Exception:  # Type 1, dfont, woff: no sfnt name table we can read
                        meta = {}
                    font_files.append((relf, dict(meta, format=ext[1:])))
                if ext not in DESIGN_EXT or fn.startswith("._") or SENSITIVE.search(fn):
                    continue
                p = os.path.join(dp, fn)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                if st.st_mtime < since:
                    continue
                seen += 1
                if a.dry_run:
                    continue
                rel = os.path.relpath(p, root)
                row = db.execute("SELECT size, mtime, result FROM f WHERE path=?", (rel,)).fetchone()
                if row and row[0] == st.st_size and row[1] == st.st_mtime and \
                        json.loads(row[2]).get("v", 1) >= RESULT_VERSION:
                    r = json.loads(row[2])
                elif (r := moved.get(rel, st.st_size, st.st_mtime)) is not None:
                    db.execute("REPLACE INTO f VALUES (?,?,?,?)", (rel, st.st_size, st.st_mtime, json.dumps(r)))
                else:
                    t1 = time.time()
                    signal.alarm(a.file_timeout)
                    try:
                        r = extract(p, ext, max_bytes)
                    except FileTimeout:  # cached as empty so a resumed scan doesn't retry it
                        log.write(f"{datetime.now():%F %T} TIMEOUT {rel} ({st.st_size / 1e6:.0f} MB, {a.file_timeout}s)\n"); log.flush()
                        r = {"fonts": [], "swatches": [], "spots": [], "colours": {}, "title": None, "pages": None,
                             "pantone": 0, "links": [], "timeout": True, "v": RESULT_VERSION}
                    except Exception as e:  # one bad file never stops the scan
                        log.write(f"{datetime.now():%F %T} ERR {rel}: {e}\n"); continue
                    finally:
                        signal.alarm(0)
                    if time.time() - t1 > 30:
                        log.write(f"{datetime.now():%F %T} SLOW {rel} {time.time() - t1:.0f}s\n"); log.flush()
                    db.execute("REPLACE INTO f VALUES (?,?,?,?)", (rel, st.st_size, st.st_mtime, json.dumps(r)))
                files.append((rel, st.st_mtime, r))
            if files and len(files) % 200 == 0:
                db.commit()
        db.commit()
        if a.dry_run:
            print(f"{folder.name:40} {seen:6} design files")
            continue
        res = aggregate(folder.name, files, inventory, font_files, [p.name for p in all_folders])
        res["rules"] = read_rules(folder)
        try:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            import brand_meta
            from kinds import kinds_of
            projects, series, summary = brand_meta.build_projects(folder.name, files, kinds_of, root=folder)
            pidx = {rel: brand_meta.project_key(rel) for rel, _, _ in files}
            res["projects"], res["series"], res["projects_summary"] = projects, series, summary
            res["identity_signals"] = brand_meta.identity_signals(folder.name, files, pidx)
        except Exception as e:  # metadata never blocks the colour/font/asset scan
            log.write(f"{datetime.now():%F %T} {folder.name}: brand metadata failed: {e}\n")
        name = safe_folder(folder.name)
        if a.collect:
            collect(root, out, name, res, a)
        (out / "clients" / name).mkdir(parents=True, exist_ok=True)
        (out / "clients" / name / f"{name}.nas-scan.json").write_text(json.dumps(res, indent=1, ensure_ascii=False))
        msg = (f"{folder.name}: {len(files)} design files in {time.time() - t0:.0f}s · {len(res['colours'])} colours · "
               f"{len(res['fonts'])} fonts · {len(res['spots'])} spots · {len(res['guideline_books'])} guideline candidates")
        if moved and moved.hits > hits0:
            msg += f" · {moved.hits - hits0} reused from a moved/renamed folder"
        print(msg); log.write(f"{datetime.now():%F %T} {msg}\n"); log.flush()


if __name__ == "__main__":
    main()
