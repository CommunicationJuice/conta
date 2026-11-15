# conta

**Come contiamo il nostro lavoro.** Ogni notte Communication Juice, agenzia di pubblicità, eventi e pubbliche
relazioni di Milano, rilegge il proprio archivio di lavoro e ne ricava quanti progetti ha fatto, di che tipo, quando
sono cominciati e quando sono stati consegnati. I totali sono pubblici su
[cjuice.it/chi-siamo](https://cjuice.it/chi-siamo/) e, con il metodo e i file CSV, su
[cjuice.it/chi-siamo/dataset](https://cjuice.it/chi-siamo/dataset/). Questo è il codice che li conta.

---

*conta* (Italian: "it counts", and "the count") is the code that turns an agency's working archive into a public
count of its work. It is the code Communication Juice runs every night; the published totals come from it unchanged.

## What it does

1. **Scan** (`conta/nas_scan.py`): walks the archive, one top-level folder per client, and reads the *design* files
   (InDesign, Illustrator, PDF, IDML, ASE, SVG): XMP metadata, fonts, swatches, page counts, delivery PDFs. Photos,
   video and audio are skipped by extension. Files and folders whose names suggest credentials, administration, contracts,
   invoices or quotes are never opened. Python standard library only; resumable through a SQLite cache.
2. **Register** (`conta/brand_meta.py`): turns the files into a project register, one row per project folder: its
   kinds, when the agency's own work on it started (file history inherited from earlier jobs and clean files received
   from the client are left out), the last design edit, the deliveries, editions of the same piece across years.
3. **Kinds** (`conta/kinds.py`): the 19 crafts (identity, advertising, events, catalogues…), read from folder and
   file names in Italian and English.
4. **Count** (`conta/count.py`): from the register to the published totals (projects per year and per craft, events,
   starts per month, deliveries over the last 30 days). The rules are written out in words at the top of the file.

## Run it

```sh
python3 conta/nas_scan.py --root /path/to/archive --out /tmp/scan
python3 examples/count_archive.py /tmp/scan > totals.json
```

No dependencies beyond Python 3.9 or later.

## What is not here, and why

The agency's own layer stays private: which folders are clients, partners or the agency itself, which clients may be
named (and the confidentiality rulings behind that), paths and hosts, the nightly job and the push to the website.
Those are decisions and plumbing, not the method. The example script counts every folder as a client instead.

No client data is in this repository, and none is needed to run it.

## Licence

Code: MIT (see `LICENSE`). The published totals on cjuice.it carry their own licence, stated on the dataset page.
