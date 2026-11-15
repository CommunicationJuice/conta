"""From projects to the published totals: the numbers on cjuice.it/chi-siamo/ and cjuice.it/chi-siamo/dataset/.

Input: the project register (brand_meta.build_projects, one row per project folder) of every client whose work is
counted. Output, in the bi-public/v1 shapes:

  summary          projects per year (and how many were events), projects per craft, active clients
  now_30d          deliveries per day over the last 30 days, and the work still open ("sulla scrivania")
  starts_by_month  project starts per month, only where the start date is trusted (confidence floor)

The rules, in words:
- A project is counted once, in the year it started. Dates before 2000 are unset-file-date artefacts (1999-12-31,
  1904, 1970) and are treated as missing.
- A project named for a year ("Proposte 2014") whose start is more than a year earlier, and not certain, took that
  start from a reused asset: its start moves to the first delivery (else the last design edit), when that falls within
  a year of the named year.
- A project is an event when its kinds include "event". Events are projects too: they are inside the totals.
- Crafts: a project counts once in each craft it carries, so the crafts add up to more than the projects.
- Last 30 days: one project, one bucket. A delivery (the final delivery, else the first) inside the window counts on
  its day; otherwise any work inside the window counts the project as open.
- Starts by month: only starts with medium or high confidence; the rest stay out rather than land in a wrong month.
  That is why this table counts fewer projects than the total.
Who is counted (clients and partners, never the agency's own internal work; confidential clients are counted but never
named) is decided before this step, by the caller.
"""
import re
from collections import defaultdict
from datetime import date, timedelta

SCHEMA = "bi-public/v1"
SINCE_YEAR = 2008
EVENT_KINDS = {"event"}
WINDOW = 30
CONF_RANK = {"low": 0, "medium": 1, "high": 2}
START_CONFIDENCE_FLOOR = "medium"


def year(d):
    """The year of an ISO date string, or None."""
    return int(d[:4]) if d and str(d)[:4].isdigit() else None


def real_date(d):
    """Dates before 2000 are unset-file-date artefacts (1999-12-31, 1904, 1970)."""
    y = year(d)
    return d if y and y >= 2000 else None


def named_year_floor(p):
    """A project named for a year whose low/medium-confidence start is more than a year earlier took that start from a
    reused asset (e.g. an older vector lettering placed in a newer invitation). Move the start to the first delivery,
    else the last design edit, when that falls within a year of the named year."""
    s = real_date(p.get("started"))
    conf = ((p.get("dates") or {}).get("started") or {}).get("confidence")
    named = [int(y) for y in re.findall(r"(?<!\d)(20[0-3]\d)(?!\d)", str(p.get("project") or ""))]
    if not s or conf == "high" or not named or int(s[:4]) >= min(named) - 1:
        return p
    anchor = p.get("first_delivery") or real_date(p.get("last_design_edit"))
    if not anchor or int(anchor[:4]) < min(named) - 1:
        return p
    dates = dict(p.get("dates") or {})
    dates["started"] = {"source": "named_year_floor", "confidence": "low", "raw": s}
    return dict(p, started=anchor[:10], dates=dates)


class Tally:
    """Add counted projects one by one; then documents() gives the three bi-public/v1 documents."""

    def __init__(self, since_year=SINCE_YEAR):
        self.since_year = since_year
        self.per_year, self.ev_year, self.mix = defaultdict(int), defaultdict(int), defaultdict(int)
        self.rows = []   # (started, started confidence, delivery day, last work day)

    def add(self, p):
        """Count one project. Returns its year when it is inside the counted years, else None."""
        conf = ((p.get("dates") or {}).get("started") or {}).get("confidence") or "low"
        self.rows.append((real_date(p.get("started")), conf,
                          p.get("final_delivery") or p.get("first_delivery"),
                          max([x for x in (real_date(p.get("last_design_edit")), p.get("final_delivery"),
                                           p.get("first_delivery")) if x] or [None])))
        y = year(p.get("started"))
        counted = None
        if y and y >= self.since_year:
            self.per_year[y] += 1
            if EVENT_KINDS & set(p.get("kind") or []):
                self.ev_year[y] += 1
            counted = y
        for k in p.get("kind") or ["other"]:
            self.mix[k] += 1
        return counted

    def documents(self, as_of, meta, active_clients, counts_include):
        """(summary, now_30d, starts_by_month). as_of: the data date (YYYY-MM-DD); meta: fields every document carries
        (as_of, generated, provisional, note); active_clients: computed by the caller; counts_include: the
        visibilities that were counted."""
        per_year = self.per_year
        y = int(as_of[:4]) if as_of else date.today().year
        complete = bool(as_of) and as_of[5:10] == "12-31"
        end = date.fromisoformat(as_of[:10]) if as_of else date.today()
        start = end - timedelta(days=WINDOW - 1)
        by_day, on_table = [0] * WINDOW, 0
        for st, conf, dlv, work in self.rows:
            dd = date.fromisoformat(dlv) if dlv else None
            if dd and start <= dd <= end:
                by_day[(dd - start).days] += 1
            elif work and start <= date.fromisoformat(work) <= end:
                on_table += 1
        now_30d = dict(meta, schema=SCHEMA, window_days=WINDOW, delivered_by_day=by_day,
                       delivered=sum(by_day), on_table=on_table,
                       delivery_source="v8 delivery PDFs (earliest print-final, else earliest PDF); not yet email events")
        months = {str(yr): [0] * 12 for yr in sorted(per_year)}
        for st, conf, _, _ in self.rows:
            if st and year(st) in per_year and CONF_RANK.get(conf, 0) >= CONF_RANK[START_CONFIDENCE_FLOOR]:
                months[st[:4]][int(st[5:7]) - 1] += 1
        starts = dict(meta, schema=SCHEMA, years=months, dated_projects=sum(map(sum, months.values())),
                      total_projects=sum(per_year.values()), confidence_floor=START_CONFIDENCE_FLOOR)
        summary = dict(meta, schema=SCHEMA, since_year=self.since_year,
                       years_active=y - self.since_year,
                       calendar_years_worked=sum(1 for n in per_year.values() if n),
                       counts_include=sorted(counts_include),
                       current_year=y, current_year_complete=complete,
                       active_clients=active_clients, projects_per_year=dict(sorted(per_year.items())),
                       events_per_year=dict(sorted(self.ev_year.items())),
                       disciplines=dict(sorted(((k, n) for k, n in self.mix.items() if k != "other"), key=lambda kv: -kv[1])))
        return summary, now_30d, starts
