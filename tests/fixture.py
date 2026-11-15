"""A small made-up archive for the tests: two clients, no real files, every date set on purpose.

The files are tiny placeholders (the scanner reads metadata and tolerates files it can't parse); what matters is the
folder layout, the file names and the modification times, which are what the register works from when there is no
embedded XMP history. Each project exercises one rule; the expected outcome is written next to it.
"""
import os
from datetime import datetime
from pathlib import Path

AS_OF = "2026-06-30"   # the data date the tests count against

# (path inside the archive, modification date). Comments: the expected outcome for the project.
FILES = [
    # Two working versions of the layout: CJ's own work, start trusted (confidence high). Catalogue, 2019 (the word
    # "stampa" in the PDF name also makes it advertising: a project can carry more than one craft).
    ("Client A/Catalogo 2019/catalogo_v1.indd", "2019-03-04"),
    ("Client A/Catalogo 2019/catalogo_v2.indd", "2019-03-20"),
    ("Client A/Catalogo 2019/catalogo_2019_stampa.pdf", "2019-04-02"),
    # A layout and its PDF with the same name: versions of one piece, start trusted. A stand at a fair: an event, 2023.
    ("Client A/Stand Sicam 2023/stand.ai", "2023-09-01"),
    ("Client A/Stand Sicam 2023/stand_def.pdf", "2023-10-01"),
    # Material received from the client, dated inside the client's active years: received by its name alone.
    ("Client A/Materiale cliente/logo_cliente.ai", "2023-05-02"),
    # A logo with two versions: identity, 2021, start trusted.
    ("Client B/Logo/logo.ai", "2021-05-10"),
    ("Client B/Logo/logo_v2.ai", "2021-05-12"),
    # A lone clean file far older (over 180 days) than the client's own work: received, not counted.
    ("Client B/Brochure/brochure.indd", "2015-06-10"),
    # A project named for a year whose file date is far older (a reused 2008 asset): its start moves to the first
    # delivery, 2014-03-10, with low confidence, so it counts in 2014 but stays out of the months table. It has a
    # client of its own: next to newer work of the same client, a lone old file reads as received material.
    ("Client C/Proposte 2014/bozzetto.indd", "2008-02-01"),
    ("Client C/Proposte 2014/proposta.pdf", "2014-03-10"),
    # Work in the last 30 days before AS_OF: a newsletter delivered on 2026-06-25 (a delivery inside the window).
    ("Client B/Newsletter giugno/newsletter.indd", "2026-06-20"),
    ("Client B/Newsletter giugno/newsletter_def.pdf", "2026-06-25"),
    # A contracts folder: folders named like this are never opened, whatever their files are called.
    ("Client B/Contratti/documento.pdf", "2026-06-15"),
    # A price list still being worked on, no PDF yet: open work ("sulla scrivania"). One file: confidence medium.
    ("Client B/Listino 2026/listino.indd", "2026-06-28"),
]


def build(root):
    """Write the archive under root (a directory that exists) and return root."""
    root = Path(root)
    for rel, day in FILES:
        f = root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"%PDF-1.4\n% conta test fixture\n" if f.suffix == ".pdf" else b"conta test fixture\n")
        t = datetime.strptime(day + " 12:00", "%Y-%m-%d %H:%M").timestamp()
        os.utime(f, (t, t))
    return root
