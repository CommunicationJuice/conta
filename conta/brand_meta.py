"""Brand metadata for the Creator brand scan: who the client is, and what work CJ does for it.

Per file (called from nas_scan.extract, cached with the scan):
  file_meta()      XMP dates, rights, PDF/X, and identity signals from text: website URLs and domains,
                   email DOMAINS (never addresses), legal company names, copyright holders.
Per client folder (called from nas_scan after the walk):
  build_projects() the project register: one row per project folder — kind, started (CJ's own first working
                   day: inherited file history and received clean files left out, see RELATION_GAP),
                   last design edit, last delivery, PDF/X, pages, status, received — plus series/editions
                   (e.g. AGENDA2020 → 2024 → 2027) and a summary (work mix, cadence, seasonality).
  identity_signals() each name/domain/company candidate with the set of projects it appears in.
Across clients (called from the nightly after all folders are scanned):
  build_identity() the client identity record. It chooses by BREADTH, not frequency: the client's own
                   brand appears across many of its projects; a partner, sponsor or event co-brand appears
                   in one or two, and names seen in many clients' folders are generic or CJ's own.
Privacy: no personal names, no email addresses, no fiscal identifiers are kept.
"""
import json, re, statistics, zipfile
from datetime import timedelta
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

# ---------------- per file ----------------

GENERIC_DOMAINS = re.compile(
    r"(^|\.)(adobe|w3|purl|iptc|apple|microsoft|google|gstatic|googleapis|typekit|fonts|pantone|openxmlformats|"
    r"schemas|xmlsoap|ns|example|localhost|gmail|googlemail|libero|hotmail|outlook|live|msn|yahoo|icloud|me|"
    r"pec|legalmail|aruba|tiscali|virgilio|fastwebnet|alice|tin|email|mail|wix|wordpress|squarespace|"
    r"facebook|instagram|linkedin|youtube|twitter|x|tiktok|pinterest|whatsapp|bit|goo|tinyurl|"
    r"cjuice|communicationjuice|shutterstock|adobestock|istockphoto|gettyimages|unsplash|freepik|"
    r"creativecommons|wikipedia|amazon|apple|paypal|stripe|cloudflare|mozilla|github)\.[a-z.]+$", re.I)
URL = re.compile(r"(?:https?://|www\.)[A-Za-z0-9.\-]+\.[a-z]{2,10}(?:/[^\s\"'<>()]*)?", re.I)
EMAIL_DOMAIN = re.compile(r"[A-Za-z0-9._%+\-]+@([A-Za-z0-9.\-]+\.[a-z]{2,10})\b")
LEGAL = re.compile(
    r"\b([A-Z][A-Za-z0-9&'’.\-]*(?:\s+[A-Z0-9&][A-Za-z0-9&'’.\-]*){0,5})\s*,?\s+"
    r"(S\.?\s?p\.?\s?A\.?|S\.?\s?r\.?\s?l\.?\s?s?\.?|S\.?\s?c\.?\s?a\.?\s?r\.?\s?l\.?|S\.?\s?a\.?\s?s\.?|S\.?\s?n\.?\s?c\.?|"
    r"Soc(?:ietà)?\.?\s+Coop(?:erativa)?\.?|GmbH|AG|Ltd\.?|Limited|Inc\.?|LLC|S\.A\.|B\.V\.|SE)(?![A-Za-z])")
COPYRIGHT = re.compile(r"(?:©|\(c\)|copyright)\s*(?:\d{4}\s*[-–]?\s*(?:\d{4})?\s*)?([A-Z][^\n\r©|;]{2,60})", re.I)


TRADEMARK = re.compile(r"([A-Z][\w\-’']{1,30}(?:\s+[A-Z0-9][\w\-’']{1,30}){0,2})\s?[®™]")
LOCATION = re.compile(r"\b\d{5}\s+([A-Z][a-zàèéìòù’']+(?:[\s-][A-Z][a-zàèéìòù’']+){0,2})\s*\(?([A-Z]{2})\)?\b")
SOCIAL = re.compile(r"(instagram\.com|facebook\.com|linkedin\.com/company|youtube\.com/(?:@|c/|channel/)?|tiktok\.com/@?)"
                    r"/?([A-Za-z0-9_.\-]{2,60})", re.I)
PHRASE = re.compile(r"(?:^|[.!?\n\r]\s*)([A-ZÀ-Ý][^.!?\n\r]{12,80}[.!?]?)")
STOP = {"it": {"il", "la", "di", "che", "per", "con", "una", "della", "nel", "sono", "alla", "dei", "gli"},
        "en": {"the", "and", "of", "to", "with", "for", "your", "our", "is", "are", "from", "this"},
        "de": {"der", "die", "und", "das", "mit", "für", "ist", "von", "den", "zu", "auf", "nicht"},
        "fr": {"le", "les", "des", "et", "pour", "avec", "une", "est", "dans", "du", "sur", "qui"},
        "es": {"el", "los", "las", "y", "para", "con", "una", "es", "del", "por", "que", "su"}}


def languages(text):
    words = re.findall(r"[a-zàèéìòùäöüß]+", text.lower()[:300_000])
    counts = {lang: sum(1 for w in words if w in st) for lang, st in STOP.items()}
    tot = sum(counts.values())
    return {k: round(v / tot, 2) for k, v in counts.items() if tot >= 20 and v / tot >= 0.1}


def norm_domain(d):
    d = d.lower().strip(".").removeprefix("www.")
    return d if "." in d and not GENERIC_DOMAINS.search(d) and not re.fullmatch(r"[\d.]+", d) else None


PERSONAL_DOC = re.compile(r"bigliett|business.?card|\bbv[_\s-]|cards?\b|carta.?intestata|letterhead|firma|signature|"
                          r"badge|cartellin|curriculum|\bcv\b|tesserin", re.I)


def text_signals(text, cap=40, phrases_ok=True):
    """Identity signals from free text. Returns small, de-duplicated lists."""
    text = text[:2_000_000]
    domains, sites = {}, {}
    for u in URL.findall(text):
        host = re.sub(r"^(https?://)?", "", u, flags=re.I).split("/")[0]
        d = norm_domain(host)
        if d:
            sites.setdefault(d, ("https://" + host.lower().removeprefix("http://").removeprefix("https://")).rstrip("/") + "/")
    for d in EMAIL_DOMAIN.findall(text):
        d = norm_domain(d)
        if d:
            domains[d] = domains.get(d, 0) + 1
    legal = set()
    for name, form in LEGAL.findall(text):
        name = re.sub(r"\s+", " ", name).strip(" .,-")
        if 2 <= len(name) <= 60 and not re.search(r"\d{5,}", name):
            form_n = re.sub(r"[\s.]", "", form).upper().replace("SCARL", "S.c.a.r.l.")
            legal.add(name + " " + form_n)
    rights = {re.sub(r"\s+", " ", m).strip(" .,-") for m in COPYRIGHT.findall(text)}
    marks = {re.sub(r"\s+", " ", m).strip() for m in TRADEMARK.findall(text)}
    places = {f"{c} ({p})" for c, p in LOCATION.findall(text)}
    social = {}
    for net, handle in SOCIAL.findall(text):
        h = handle.strip("/.").lower()
        if h and h not in ("p", "share", "sharer", "watch", "pages", "groups", "in", "company", "intent"):
            social.setdefault(re.sub(r"\.com.*", "", net.lower()), set()).add(h)
    phrases = set()
    for ph in (PHRASE.findall(text) if phrases_ok else []):
        ph = re.sub(r"\s+", " ", ph).strip()
        nw = len(ph.split())
        if 3 <= nw <= 10 and not re.search(r"\d{3,}|@|www|http|[{}<>\\]", ph):
            phrases.add(ph[:90])
    return {"sites": dict(list(sites.items())[:cap]), "email_domains": sorted(domains)[:cap],
            "legal_names": sorted(legal)[:cap], "rights": sorted(r for r in rights if len(r) > 2)[:cap],
            "trademarks": sorted(marks)[:cap], "locations": sorted(places)[:cap],
            "social": {k: sorted(v)[:5] for k, v in social.items()},
            "phrases": sorted(phrases)[:60], "languages": languages(text)}


def xmp_meta(xml):
    g = lambda pat: (re.search(pat, xml) or [None, None])[1]
    m = {"created": g(r"<xmp:CreateDate>([^<]+)</xmp:CreateDate>") or g(r'xmp:CreateDate="([^"]+)"'),
         "modified": g(r"<xmp:ModifyDate>([^<]+)</xmp:ModifyDate>") or g(r'xmp:ModifyDate="([^"]+)"'),
         "pdfx": bool(re.search(r"GTS_PDFXVersion", xml)),
         "tool": (g(r"<xmp:CreatorTool>([^<]+)</xmp:CreatorTool>") or g(r'xmp:CreatorTool="([^"]+)"') or "")[:60] or None}
    rights = re.search(r"<dc:rights>[\s\S]*?<rdf:li[^>]*>([^<]*)</rdf:li>", xml)
    m["rights"] = rights.group(1).strip()[:120] if rights else None
    # save/edit history: the days someone worked on the file (no names kept)
    when = re.findall(r"stEvt:when(?:=\"|>)([0-9T:\-+.Z]{10,})", xml)
    m["work_days"] = sorted({w[:10] for w in when})[-400:]
    w = re.search(r"<stDim:w>([\d.]+)</stDim:w>\s*<stDim:h>([\d.]+)</stDim:h>\s*<stDim:unit>(\w+)</stDim:unit>", xml)
    if w:
        k = {"Millimeters": 1, "Points": 25.4 / 72, "Pixels": 25.4 / 72, "Inches": 25.4, "Centimeters": 10}.get(w.group(3), None)
        if k:
            m["page_mm"] = [round(float(w.group(1)) * k), round(float(w.group(2)) * k)]
    return m


PDF_LITERAL = re.compile(rb"\(((?:[^()\\]|\\.){4,300})\)")
PDF_URI = re.compile(rb"/URI\s*\(([^)]{6,300})\)")


def pdf_text(data, blobs):
    """Literal strings from content streams + link annotations (URLs are plain text there)."""
    parts = [m.group(1).decode("latin-1", "ignore") for m in PDF_URI.finditer(data[:64 << 20])]
    n = 0
    for b in blobs:
        for m in PDF_LITERAL.finditer(b):
            s = m.group(1).decode("latin-1", "ignore")
            parts.append(s); n += len(s)
            if n > 400_000:
                return " ".join(parts)
    return " ".join(parts)


def zip_text(path, members_re, max_bytes=4 << 20):
    """Text of XML members in a zip container (IDML stories, docx/pptx), tags stripped."""
    out, n, extra = [], 0, {}
    with zipfile.ZipFile(path) as z:
        for name in z.namelist():
            if name == "docProps/app.xml":
                c = re.search(rb"<Company>([^<]*)</Company>", z.read(name))
                extra["company_field"] = c.group(1).decode("utf-8", "ignore").strip() if c else None
            if name == "docProps/core.xml":
                t = re.search(rb"<dc:title>([^<]*)</dc:title>", z.read(name))
                extra["title"] = t.group(1).decode("utf-8", "ignore").strip() if t else None
            if re.search(members_re, name) and n < max_bytes:
                raw = z.read(name)[:max_bytes - n]
                n += len(raw)
                out.append(re.sub(r"<[^>]+>", " ", raw.decode("utf-8", "ignore")))
    return " ".join(out), extra


def file_meta(path, ext, xml=None, text=""):
    m = xmp_meta(xml) if xml else {}
    if ext == ".idml":
        t, _ = zip_text(path, r"^Stories/.*\.xml$")
        text += " " + t
    elif ext in (".docx", ".pptx"):
        t, extra = zip_text(path, r"^(word/document|ppt/slides/slide\d+)\.xml$")
        text += " " + t
        m.update({k: v for k, v in extra.items() if v})
    if m.get("rights"):
        text += " © " + m["rights"]
    clean = ext in (".idml", ".docx", ".pptx") and not PERSONAL_DOC.search(str(path))
    m["signals"] = text_signals(text, phrases_ok=clean) if text.strip() else {}
    return m


# ---------------- per client folder: projects ----------------

GENERIC_DIR = re.compile(r"^(links?|fonts?|document fonts|immagini|images?|img|foto|photos?|export|exports|pdf|pdfs|"
                         r"stampa|print|old|vecchi|buttare|scarti|archivio|archive|def|definitivi?|finali?|final|"
                         r"versioni|bozze|drafts?|varie|misc|materiali|material[ei]|risorse|resources|assets|grafiche?|"
                         r"cliente|dal cliente|originali|preview|anteprime|jpg|png|tiff?|ai|indd|psd|svg|eps|"
                         r"web|social|output|consegna|consegne|delivery|_+.*)$", re.I)
# containers whose subfolders are the projects: year folders, job lists, and old/archive folders inside a client
# (e.g. <client>/vecchio holding a 2015 gala and 2018 jobs as separate folders; 2026-11-12)
GROUP_DIR = re.compile(r"^((19|20)\d{2}|lavori|progetti|projects|jobs|commesse|clienti|anni|in corso|attivi|chiusi|"
                       r"vecchio|vecchi|old|archivio|archive)$", re.I)
MASTER = {".indd", ".ai", ".idml"}
DELIVERY = {".pdf"}
EDITION_STRIP = re.compile(r"[\s_\-]*((19|20)\d{2}|v\d+|rev\d*|ed\d*|def|definitivo|final[e]?|ok|copia|copy|\(\d+\))", re.I)
# --- relationship start (who made what, when) ---------------------------------------------------------------
# File dates carry other people's history: a client's 2005 InDesign file reworked by CJ in 2026 still holds its
# 2005 creation date and edit days. A file's dates are therefore walked back from its last save only while the
# gaps stay under RELATION_GAP; older dates are inherited. Clean files (a lone PDF, a logo with no versions, a
# packaged folder) that predate the project's own working files by more than RELATION_GAP were received.
RELATION_GAP = timedelta(days=180)
OWN_TOOL = {".indd": "indesign", ".idml": "indesign", ".ai": "illustrator"}
LOGO_NAME = re.compile(r"logo|marchio|logotip|pittogramma|brand ?mark", re.I)
SIBLING_STRIP = re.compile(r"[\s_\-.()]*(v\d{1,2}|rev\.?\s?\d*|bozza\s?\d*|draft\s?\d*|def(initivo)?|ok\s?stampa|"
                           r"approvat[oa]|final[e]?|correz\w*|modific\w*|copia|copy|\d{1,2})(?=$|[\s_\-.()])", re.I)
# folders of material received or collected, not CJ work (client archives, references, samples, templates)
RECEIVED = re.compile(r"material[ei]?[ _-]*(del[ _-]*)?client[ei]|da[ _-]*client[ei]|ricevut[oie]|"
                      r"client[ _-]*(material|files|assets)|from[ _-]*client|riferiment[io]|\breferences?\b|"
                      r"\besempi\b|campion(i|ario)|\btemplates?\b|"
                      r"\bricerca\b(?![ _-]*(di[ _-]*)?(personale|staff|figur|collaborat|addett))", re.I)


def _sibling_stem(name):
    s = Path(name).stem.lower()
    for _ in range(4):
        s2 = SIBLING_STRIP.sub("", s).strip(" _-.")
        if s2 == s:
            break
        s = s2
    return s


def _packaged(root, rel_dir, cache={}):
    """An InDesign package folder (Links + fonts next to the layout): a hand-off, not a working folder."""
    if root is None:
        return False
    d = Path(root).parent / rel_dir
    if d not in cache:
        try:
            names = {n.name.lower() for n in d.iterdir() if n.is_dir()}
        except OSError:
            names = set()
        cache[d] = "links" in names and bool(names & {"document fonts", "fonts", "font"})
    return cache[d]


def _own_dates(ext, mtime, meta):
    """(first, days): the file's own working dates, newest first, cut at the first gap > RELATION_GAP."""
    last = datetime.fromtimestamp(mtime).replace(hour=0, minute=0, second=0, microsecond=0)
    own = meta.get("xmp_own")
    if own is None:   # results cached before xmp_own existed: trust a layout's metadata only if its tool matches
        own = ext not in OWN_TOOL or OWN_TOOL[ext] in (meta.get("tool") or "").lower()
    ev = [last]
    if own:
        for s in [meta.get("created")] + list(meta.get("work_days") or []):
            # plain dates (work_days) are calendar days already: no timezone shift
            d = datetime.strptime(s, "%Y-%m-%d") if isinstance(s, str) and len(s) == 10 else _dt(s)
            if d:
                ev.append(d.replace(hour=0, minute=0, second=0, microsecond=0))
    ev = sorted({d for d in ev if d.year >= 1995}, reverse=True) or [last]
    chain = [ev[0]]
    for d in ev[1:]:
        if chain[-1] - d > RELATION_GAP:
            break
        chain.append(d)
    return chain[-1], {d.strftime("%Y-%m-%d") for d in chain}, own, len(chain) < len(ev)


FORMATS = {(210, 297): "A4", (148, 210): "A5", (297, 420): "A3", (420, 594): "A2", (594, 841): "A1", (105, 148): "A6",
           (85, 55): "business card", (55, 85): "business card", (210, 210): "21x21", (200, 200): "20x20",
           (700, 1000): "70x100 poster", (1000, 700): "100x70 poster", (500, 700): "50x70 poster",
           (99, 210): "DL", (210, 99): "DL", (1920, 1080): "16:9 screen", (338, 190): "16:9 slide"}
VERSION_MARK = re.compile(r"(?:^|[\s_\-(])(v\d{1,2}|rev\.?\s?\d*|bozza\s?\d*|draft\s?\d*|def(initivo)?|ok\s?stampa|"
                          r"approvat[oa]|final[e]?|correz\w*|modific\w*)(?=$|[\s_\-).])", re.I)
FINISHING = {"die-cut": r"fustell|die.?line|cutcontour|stanz|troquel|artios_cut|taglio|cut\b",
             "crease/fold": r"cordon|crease|rillung|piega|falz",
             "spot UV / varnish": r"\buv\b|vernic|varnish|lucid|lack",
             "foil": r"foil|oro a caldo|lamina|hot.?stamp|praeg|präg",
             "white ink": r"white|bianco coprente|deckweiss|weiss",
             "emboss": r"emboss|rilievo|sbalz|blind"}


def fmt_of(mm):
    if not mm:
        return None
    w, h = mm
    for (fw, fh), name in FORMATS.items():
        if abs(w - fw) <= 3 and abs(h - fh) <= 3:
            return name
    return f"{w}x{h} mm"


def _dt(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")[:25]).astimezone(timezone.utc).replace(tzinfo=None)
    except ValueError:
        try:
            return datetime.strptime(s[:10], "%Y-%m-%d")
        except ValueError:
            return None


def project_key(rel):
    """Client-relative project path for a design file: the shallowest meaningful folder."""
    parts = Path(rel).parts[1:]          # drop the client folder itself
    dirs = list(parts[:-1])
    while dirs and GENERIC_DIR.match(dirs[-1]):
        dirs.pop()
    key = []
    for d in dirs:
        key.append(d)
        if not GROUP_DIR.match(d):
            break
    if not key or all(GROUP_DIR.match(k) for k in key):
        stem = EDITION_STRIP.sub("", Path(parts[-1]).stem).strip(" _-") or Path(parts[-1]).stem
        key = key + [f"({stem})"]
    return "/".join(key)


def _relation(items, root):
    """Per project: (started, CJ working-file start, own work days, received files, inherited files, raw start)."""
    fam = defaultdict(int)
    for i in items:
        fam[(str(Path(i[0]).parent), _sibling_stem(i[0]))] += 1
    rows = []
    for rel, mtime, r, ext in items:
        meta = r.get("meta") or {}
        first, days, own, inherited = _own_dates(ext, mtime, meta)
        raw = [datetime.fromtimestamp(mtime)] + ([_dt(meta.get("created"))] if _dt(meta.get("created")) else [])
        pkg = _packaged(root, str(Path(rel).parent))
        sibs = fam[(str(Path(rel).parent), _sibling_stem(rel))]
        messy = ext in MASTER and (sibs >= 2 or len(days) >= 2) and not pkg
        rows.append((first, days if ext in MASTER else set(), messy, inherited, min(raw)))
    mess = [f for f, _, m, _, _ in rows if m]
    ms = min(mess) if mess else None
    keep = [x for x in rows if x[2] or ms is None or x[0] >= ms - RELATION_GAP]
    days = sorted(set().union(*[x[1] for x in keep])) if keep else []
    return (min(x[0] for x in keep), ms, days, len(rows) - len(keep), sum(x[3] for x in rows),
            min(x[4] for x in rows))


FINAL_NAME = re.compile(r"(?:^|[\s_\-.(])(def(initiv[oa])?|esecutiv[oi]|stampa|tipografi[ac]|hr|hi-?res|print|"
                        r"final[e]?|ok[\s_-]?stampa)(?=$|[\s_\-.)\d])", re.I)
PROOF_NAME = re.compile(r"bozz|draft|proof|prova|anteprima|preview|\blr\b|low|_web\b|\bweb\b|mail|small", re.I)


def _bleed(mm):
    """Page larger than a standard format by 5–12 mm per dimension: a print file with bleed (3–5 mm per side)."""
    if not mm or len(mm) < 2:
        return False
    w, h = mm[:2]
    return any(5 <= w - fw <= 12 and 5 <= h - fh <= 12 for fw, fh in FORMATS)


def _deliveries(pdfs, started):
    """first_delivery: the earliest own PDF (often a proof sent for review, so low confidence).
    final_delivery: the earliest PDF that looks print-final, scored on cached signals: PDF/X (2), page with
    bleed (2), final-sounding name (1); proof-sounding names are skipped. Score ≥ 3 high, 2 medium.
    TrimBox/BleedBox and crop marks are not in the cache yet (next deliberate re-read)."""
    own = [(datetime.fromtimestamp(i[1]), i) for i in pdfs if not started or datetime.fromtimestamp(i[1]) >= started]
    if not own:
        return None, None, None
    first = min(d for d, _ in own)
    finals = []
    for d, (rel, _, r, _) in own:
        name, meta = Path(rel).stem, r.get("meta") or {}
        if PROOF_NAME.search(name):
            continue
        score = 2 * bool(meta.get("pdfx")) + 2 * _bleed(meta.get("page_mm")) + bool(FINAL_NAME.search(name))
        if score >= 2:
            finals.append((d, score))
    if not finals:
        return first, None, None
    d, _ = min(finals)
    best = max(s for x, s in finals if x == d)
    return first, d, "high" if best >= 3 else "medium"


def build_projects(client_folder, files, kinds_of, now=None, root=None):
    """root: the client folder on disk (optional), used to recognise packaged folders."""
    now = now or datetime.now()
    groups = defaultdict(list)
    for rel, mtime, r in files:
        ext = Path(rel).suffix.lower()
        if ext in MASTER | DELIVERY | {".docx", ".pptx"}:
            groups[project_key(rel)].append((rel, mtime, r, ext))
    projects = []
    for key, items in groups.items():
        masters = [i for i in items if i[3] in MASTER]
        pdfs = [i for i in items if i[3] in DELIVERY]
        mt = lambda xs: [datetime.fromtimestamp(i[1]) for i in xs]
        started, cj_from, days, n_received, n_inherited, raw_start = _relation(items, root)
        last_edit = max(mt(masters)) if masters else None
        last_delivery = max(mt(pdfs)) if pdfs else None
        first_delivery, final_delivery, final_conf = _deliveries(pdfs, started)
        last = max(x for x in (last_edit, last_delivery) if x) if (last_edit or last_delivery) else max(mt(items))
        age = (now - last).days
        names = key + " " + " ".join(Path(i[0]).name for i in items[:40])
        fmts = defaultdict(int)
        for i in masters + pdfs:
            f = fmt_of((i[2].get("meta") or {}).get("page_mm"))
            if f:
                fmts[f] += 1
        versions = {m.group(1).lower() for i in items for m in VERSION_MARK.finditer(Path(i[0]).stem)}
        spots = " ".join(sp for i in items for sp in i[2].get("spots", []))
        finishing = sorted(k for k, rx in FINISHING.items() if re.search(rx, spots, re.I))
        years = sorted({int(y) for y in re.findall(r"(?<!\d)((?:19|20)\d{2})(?!\d)", names) if 1990 <= int(y) <= now.year + 2})
        projects.append({
            "project": key, "kind": kinds_of(names), "years_named": years,
            "started": started.strftime("%Y-%m-%d") if started else None,
            "last_design_edit": last_edit.strftime("%Y-%m-%d") if last_edit else None,
            "last_delivery": last_delivery.strftime("%Y-%m-%d") if last_delivery else None,
            "first_delivery": first_delivery.strftime("%Y-%m-%d") if first_delivery else None,
            "final_delivery": final_delivery.strftime("%Y-%m-%d") if final_delivery else None,
            "dates": {
                "started": {"source": "xmp_history" if cj_from else "file_mtime",
                            "confidence": "low" if n_inherited else "high" if cj_from else "medium"},
                "first_delivery": {"source": "delivery_pdf", "confidence": "low"} if first_delivery else None,
                "final_delivery": {"source": "delivery_pdf", "confidence": final_conf} if final_delivery else None,
            },
            "status": "active" if age <= 30 else "recent" if age <= 180 else "dormant",
            "masters": len(masters), "pdfs": len(pdfs),
            "print_ready": any((i[2].get("meta") or {}).get("pdfx") for i in pdfs),
            "pages_max": max([i[2].get("pages") or 0 for i in items] or [0]) or None,
            "formats": [f for f, _ in sorted(fmts.items(), key=lambda kv: -kv[1])][:3],
            "work_days": len(days), "work_span": [days[0], days[-1]] if days else None,
            "version_marks": len(versions), "finishing": finishing,
            "main_files": [Path(i[0]).name for i in sorted(masters, key=lambda i: -i[1])[:3]],
            "cj_working_files_from": cj_from.strftime("%Y-%m-%d") if cj_from else None,
            "started_raw": raw_start.strftime("%Y-%m-%d") if raw_start else None,
            "received_files": n_received, "inherited_history_files": n_inherited,
            "_last": last,
        })
    # Received projects: named like client/reference material, or only clean files that ended before CJ's first
    # working file for this client. Kept in the register (flagged) but left out of the summary and series.
    era = min((datetime.strptime(p["cj_working_files_from"], "%Y-%m-%d") for p in projects
               if p["cj_working_files_from"]), default=None)
    for p in projects:
        last = p.pop("_last")
        p["received"] = bool(RECEIVED.search(p["project"]) or
                             (era and not p["cj_working_files_from"] and last < era - RELATION_GAP))
    projects.sort(key=lambda p: p["last_delivery"] or p["last_design_edit"] or p["started"] or "", reverse=True)
    own = [p for p in projects if not p["received"]]
    return projects, series_of(own, now), summary_of(own, now)


def series_of(projects, now):
    """Projects whose names differ only by year/version: editions of one recurring piece of work."""
    by = defaultdict(list)
    for p in projects:
        stem = EDITION_STRIP.sub("", p["project"].split("/")[-1]).strip(" _-()").lower()
        if stem:
            by[stem].append(p)
    out = []
    for stem, ps in by.items():
        dated = sorted((p for p in ps if p["started"]), key=lambda p: p["started"])
        if len(dated) < 2:
            continue
        starts = [datetime.strptime(p["started"], "%Y-%m-%d") for p in dated]
        gaps = [(b - a).days for a, b in zip(starts, starts[1:]) if (b - a).days > 45]
        cadence = statistics.median(gaps) if gaps else None
        nxt = (starts[-1] + timedelta(days=cadence)).strftime("%Y-%m") if cadence else None
        out.append({"series": stem, "editions": [{"project": p["project"], "started": p["started"]} for p in dated],
                    "cadence_days": int(cadence) if cadence else None, "next_expected": nxt})
    return sorted(out, key=lambda s: -len(s["editions"]))


def summary_of(projects, now):
    mix = defaultdict(int); per_year = defaultdict(int); months = defaultdict(int)
    for p in projects:
        for k in p["kind"] or ["other"]:
            mix[k] += 1
        if p["started"]:
            per_year[p["started"][:4]] += 1
            months[int(p["started"][5:7])] += 1
    dates = sorted(d for p in projects for d in (p["started"], p["last_design_edit"], p["last_delivery"]) if d)
    fm = defaultdict(int); fin = defaultdict(int)
    for p in projects:
        for f in p.get("formats", []):
            fm[f] += 1
        for f in p.get("finishing", []):
            fin[f] += 1
    wd = [p["work_days"] for p in projects if p.get("work_days")]
    return {"projects": len(projects), "work_mix": dict(sorted(mix.items(), key=lambda kv: -kv[1])),
            "formats": dict(sorted(fm.items(), key=lambda kv: -kv[1])[:8]),
            "finishing": dict(sorted(fin.items(), key=lambda kv: -kv[1])),
            "median_work_days_per_project": statistics.median(wd) if wd else None,
            "projects_per_year": dict(sorted(per_year.items())),
            "start_months": {m: months[m] for m in sorted(months)},
            "first_activity": dates[0] if dates else None, "last_activity": dates[-1] if dates else None,
            "active_now": sum(p["status"] == "active" for p in projects)}


# ---------------- per client folder: identity signals ----------------

LOGO_WORDS = re.compile(r"\b(logo(tipo|type)?|marchio|lockup|simbolo|symbol|pittogramma|def(initivo)?|bianco|nero|neg(ativo)?|"
                        r"pos(itivo)?|colou?ri?|colore|cmyk|rgb|pantone|vett(oriale)?|vector|orizzontale|verticale|oriz|"
                        r"vert|small|big|copia|copy|final[e]?|ok|white|black|positive|negative|horizontal|vertical|"
                        r"payoff|claim|ext|esteso|\d+(px|mm)?|v\d+|new|nuovo|old)\b", re.I)


def logo_name(filename):
    stem = re.sub(r"[_\-.]+", " ", Path(filename).stem)
    stem = re.sub(r"logo(tipo|type)?|marchio|vecchi[oa]|old|nuov[oa]|new", " ", stem, flags=re.I)
    stem = LOGO_WORDS.sub(" ", stem)
    stem = re.sub(r"\s+", " ", stem).strip()
    return stem if len(stem) >= 2 and not stem.isdigit() else None


def identity_signals(client_folder, files, projects_index):
    """{kind: {candidate: set(project)}} — breadth over the client's projects."""
    sig = {k: defaultdict(set) for k in ("site", "email_domain", "legal_name", "rights", "logo_name", "swatch_prefix",
                                         "trademark", "phrase", "location", "social")}
    langs = defaultdict(float); nl = 0
    for rel, mtime, r in files:
        proj = projects_index.get(rel) or project_key(rel)
        meta = r.get("meta") or {}
        s = meta.get("signals") or {}
        for d in s.get("sites", {}):
            sig["site"][d].add(proj)
        for d in s.get("email_domains", []):
            sig["email_domain"][d].add(proj)
        for n in s.get("legal_names", []):
            sig["legal_name"][n].add(proj)
        for n in s.get("rights", []):
            sig["rights"][n].add(proj)
        for n in s.get("trademarks", []):
            sig["trademark"][n].add(proj)
        for n in s.get("phrases", []):
            sig["phrase"][n].add(proj)
        for n in s.get("locations", []):
            sig["location"][n].add(proj)
        for net, hs in (s.get("social") or {}).items():
            for h in hs:
                sig["social"][f"{net}:{h}"].add(proj)
        if s.get("languages"):
            nl += 1
            for k, v in s["languages"].items():
                langs[k] += v
        if re.search(r"logo|marchio|lockup|simbolo", Path(rel).name, re.I):
            n = logo_name(Path(rel).name)
            if n:
                sig["logo_name"][n.lower()].add(proj)
        for sw in r.get("swatches", []):
            n = (sw.get("name") or "")
            m = re.match(r"([A-Za-z][A-Za-z]{2,})[_\s]+(red|blue|blu|green|verde|rosso|grey|gray|grigio|black|nero|"
                         r"gold|oro|primary|secondary|brand|corporate|pantone|cmyk|rgb)", n, re.I)
            if m and not re.fullmatch(r"cmyk|cmjn|rgb|lab|pantone|process|spot|rosso|giallo|verde|blu|cyan|ciano|magenta|"
                                      r"yellow|black|nero|bianco|white|grigio|grey|gray|red|green|blue|gold|oro|"
                                      r"pure|basic|global|new|nuovo|colore|color|colour|text|testo", m.group(1), re.I):
                sig["swatch_prefix"][m.group(1).lower()].add(proj)
    out = {k: {c: sorted(ps) for c, ps in v.items() if k != "phrase" or len(ps) >= 2} for k, v in sig.items()}
    out["languages"] = {k: round(v / nl, 2) for k, v in sorted(langs.items(), key=lambda kv: -kv[1])} if nl else {}
    return out


# ---------------- across clients: the identity record ----------------

AGENCY = re.compile(r"cjuice|communication\s*juice|\bcj\b", re.I)


def build_identity(folder, signals, projects, summary, cross_count, n_clients, shared_names=(), now=None):
    """cross_count[kind][candidate] = number of client folders where the candidate appears."""
    now = now or datetime.now()
    n_proj = max(1, len(projects))
    words = {w for w in re.sub(r"[^a-z0-9]+", " ", folder.lower()).split() if len(w) >= 3}
    generic_at = max(4, n_clients // 6)
    shared = {s.lower() for s in shared_names}

    def ranked(kind, also_partners=True):
        rows, partners = [], []
        for cand, projs in signals.get(kind, {}).items():
            if AGENCY.search(cand):
                continue
            mine = bool(words & set(re.sub(r"[^a-z0-9]+", " ", cand.lower()).split())) or \
                any(w in cand.replace(".", "").lower() for w in words)
            elsewhere = cross_count.get(kind, {}).get(cand, 1) - 1
            if elsewhere >= generic_at and not mine:
                continue                                  # generic: seen in many clients' folders
            breadth = len(projs) / n_proj
            score = breadth * (2.0 if mine else 1.0) - (0.15 * elsewhere if not mine else 0)
            row = {"value": cand, "projects": len(projs), "breadth": round(breadth, 3), "matches_folder": mine,
                   "other_clients": elsewhere}
            if (len(projs) <= 2 and not mine and n_proj >= 4) or cand.lower() in shared or elsewhere >= 2 and not mine:
                partners.append(row)
            else:
                rows.append((score, row))
        rows.sort(key=lambda x: -x[0])
        return [r for _, r in rows], partners

    sites, p_sites = ranked("site")
    mail, p_mail = ranked("email_domain")
    legal, p_legal = ranked("legal_name")
    logos, p_logos = ranked("logo_name")
    swatch, _ = ranked("swatch_prefix")
    rights, _ = ranked("rights")
    marks, _ = ranked("trademark")
    places, _ = ranked("location")
    social, _ = ranked("social")
    phrases = [{"text": c, "projects": len(ps)} for c, ps in signals.get("phrase", {}).items() if len(ps) >= 3]
    phrases.sort(key=lambda r: -r["projects"])

    # brand: logo names and swatch prefixes that match the folder lead; else the folder name itself
    brand_cands = [r for r in logos + swatch if r["matches_folder"]] or logos[:1]
    brand = brand_cands[0]["value"].title() if brand_cands else folder
    # websites: domains from URLs, plus email domains not already present (a company's mail domain is often its site)
    site_rows = sites + [dict(r, value=r["value"], via="email") for r in mail if r["value"] not in {s["value"] for s in sites}]
    websites = [{"url": f"https://www.{r['value']}/" if r["value"].count(".") == 1 else f"https://{r['value']}/",
                 "domain": r["value"], "projects": r["projects"], "via": r.get("via", "url"),
                 "matches_folder": r["matches_folder"], "status": "unverified"}
                for r in sorted(site_rows, key=lambda r: (-r["matches_folder"], -r["projects"]))[:5]]
    company = next((r["value"] for r in legal if r["matches_folder"]), legal[0]["value"] if legal else None)
    recent = [p for p in projects if p["started"] and (now - datetime.strptime(p["started"], "%Y-%m-%d")).days < 540]
    young = projects and len(recent) == len(projects)
    partners = sorted({r["value"] for r in p_logos + p_legal + p_sites}, key=str.lower)[:40]
    conf = "high" if (brand_cands and websites and websites[0]["matches_folder"]) else "medium" if (brand_cands or websites) else "low"
    return {
        "folder": folder, "brand": brand, "company": company,
        "websites": websites,
        "other_names": [r["value"] for r in (logos + swatch)[:8] if r["value"].title() != brand],
        "copyright_holders": [r["value"] for r in rights[:5]],
        "products_and_trademarks": [r["value"] for r in marks if r["projects"] >= 2 or r["matches_folder"]][:20],
        "recurring_lines": phrases[:15],
        "locations": [r["value"] for r in places[:5]],
        "social": [r["value"] for r in social[:8]],
        "languages": signals.get("languages", {}),
        "partners_seen": partners,
        "unpublished_hint": bool(young and not any(w["matches_folder"] for w in websites)),
        "first_activity": summary.get("first_activity"), "last_activity": summary.get("last_activity"),
        "confidence": conf, "status": "provisional",
        "notes": "First pass from file metadata; websites unverified. The brand-context step verifies them and "
                 "must tolerate offline or not-yet-published sites (new brands CJ is still building).",
    }
