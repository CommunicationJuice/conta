"""The crafts: what kind of work a project is, read from its folder and file names (Italian and English words).

A project can be more than one kind (a catalogue with its price list is both). The public totals count a project once
per kind it carries, so the crafts add up to more than the projects. Same vocabulary as CJ's own indexer.
"""
import re
import unicodedata


# artifact kinds, Italian and English, used for indexing and for reading briefs
KINDS = {
    "catalogue": ["catalogo", "cataloghi", "catalogue", "catalog"],
    "brochure": ["brochure", "depliant", "pieghevole"],
    "flyer": ["volantino", "volantini", "flyer", "leaflet"],
    "price-list": ["listino", "listini", "prezzi", "price"],
    "product-sheet": ["scheda", "schede", "datasheet", "sheet"],
    "business-card": ["biglietti", "biglietto", "bv", "business", "card", "cards"],
    "agenda-calendar": ["agenda", "calendario", "calendar", "diary"],
    "packaging": ["packaging", "confezione", "scatola", "carton", "box", "etichetta", "etichette", "label"],
    "signage": ["cartello", "cartelli", "insegna", "signage", "bandiera", "rollup", "roll", "totem", "pannello"],
    "advertising": ["adv", "pubblicita", "campagna", "campaign", "annuncio", "stampa"],
    "banner-digital": ["banner", "display", "newsletter", "dem"],
    "social": ["social", "instagram", "facebook", "linkedin", "post", "stories", "reel"],
    "web": ["sito", "web", "website", "landing", "wordpress"],
    "video": ["video", "spot", "motion", "trailer", "webinar"],
    "presentation": ["presentazione", "deck", "pptx", "keynote", "slide", "slides"],
    "identity": ["logo", "marchio", "brand", "identity", "guidelines", "manuale", "linee"],
    "event": ["evento", "eventi", "fiera", "stand", "expo", "invito", "cartolina"],
    "seasonal": ["natale", "christmas", "xmas", "auguri", "pasqua"],
    "report": ["bilancio", "report", "annual"],
}


def norm(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def words(s):
    return set(norm(s).split())


def kinds_of(text):
    w = words(text)
    return sorted(k for k, kws in KINDS.items() if w & set(kws))


def years_of(text):
    return sorted({int(y) for y in re.findall(r"(?<!\d)(20[0-3]\d)(?!\d)", text)})
