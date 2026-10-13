"""The daily digest (brief 8): a local HTML page in digest/ (gitignored), newest also as digest/latest.html."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from jinja2 import Environment, select_autoescape

from applier import db
from applier.config import Config
from applier.network import linkedin_search_url
from applier.paths import Paths
from applier.safety import budget_status, submission_gate

TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Job digest {{ day }}</title>
<style>
:root { --bg:#fbfaf7; --fg:#1d1d1b; --muted:#6b6a65; --line:#e4e1d8; --card:#fff; --accent:#1f5f8b; --urgent:#b3261e; }
@media (prefers-color-scheme: dark) { :root { --bg:#161615; --fg:#ecebe6; --muted:#a19f97; --line:#33322e; --card:#1f1f1d; --accent:#7db7e0; --urgent:#f2948c; } }
body { margin:0; background:var(--bg); color:var(--fg); font:15px/1.5 Calibri, "Segoe UI", sans-serif; }
main { max-width:860px; margin:0 auto; padding:24px 16px 64px; }
h1 { font-size:24px; margin:0 0 4px; } h2 { font-size:17px; margin:28px 0 8px; border-bottom:1px solid var(--line); padding-bottom:4px; }
.muted { color:var(--muted); } .urgent { color:var(--urgent); font-weight:600; }
.card { background:var(--card); border:1px solid var(--line); border-radius:8px; padding:10px 12px; margin:8px 0; }
a { color:var(--accent); } pre { white-space:pre-wrap; font:inherit; margin:6px 0 0; background:var(--bg); padding:8px; border-radius:6px; }
table { border-collapse:collapse; width:100%; } td, th { text-align:left; padding:4px 8px 4px 0; border-bottom:1px solid var(--line); vertical-align:top; }
</style></head><body><main>
<h1>Job digest, {{ day }}</h1>
<p class="muted">Submitted today {{ submitted_today }} of {{ cap }} · API spend today ${{ "%.2f"|format(spent) }} of ${{ "%.2f"|format(budget) }} · Mode {{ mode }}{% if not gate_ok %} · Submitting is off now: {{ gate_reasons }}{% endif %}</p>

<h2>Urgent ({{ urgent|length }})</h2>
{% for f in urgent %}<div class="card"><span class="urgent">{{ f.kind.replace('_', ' ') }}</span>{% if f.due_at %} · due {{ f.due_at }}{% endif %}<br>{{ f.message }}</div>
{% else %}<p class="muted">Nothing urgent.</p>{% endfor %}

<h2>Ready for you</h2>
{% for a in ready %}<div class="card"><b>{{ a.company }}</b>: {{ a.role_title }} · {{ a.status }}{% if a.approved_at %} · approved{% endif %}
{% if a.ats == 'ashby' and a.approved_at %}<br>Ashby: run <code>assist {{ a.id }}</code> (or use the dashboard) and click Submit yourself.{% endif %}
{% if a.needs_me_reason %}<br><span class="muted">Needs you: {{ a.needs_me_reason }}</span>{% endif %}</div>
{% else %}<p class="muted">No applications waiting for you.</p>{% endfor %}

<h2>Highlights ({{ highlights|length }} new)</h2>
{% for h in highlights %}<div class="card"><b>{{ h.company }}</b>: <a href="{{ h.url }}">{{ h.title }}</a> · score {{ h.score }}<br>
<span class="muted">{{ h.reason }}</span>
{% for p in h.people %}<br>· {{ p.name }}, {{ p.title }} (<a href="{{ p.source_url }}">source</a>, <a href="{{ p.linkedin }}">LinkedIn search</a>): {{ p.why }}{% endfor %}</div>
{% else %}<p class="muted">No new highlights.</p>{% endfor %}

<h2>Networking suggestions ({{ people|length }})</h2>
{% for p in people %}<div class="card"><b>{{ p.name }}</b>, {{ p.title }} at {{ p.company }} · <a href="{{ p.source_url }}">source</a> · <a href="{{ p.linkedin }}">LinkedIn search</a><br>
<span class="muted">{{ p.why }}</span>{% if p.note and p.note_status == 'checked' %}<pre>{{ p.note }}</pre>{% endif %}</div>
{% else %}<p class="muted">No new people to reach out to.</p>{% endfor %}

<h2>Applications</h2>
<p>{% for s, n in by_status %}{{ s }} {{ n }}{% if not loop.last %} · {% endif %}{% else %}None yet.{% endfor %}</p>
{% if changes %}<table><tr><th>When</th><th>Company</th><th>Change</th></tr>
{% for c in changes %}<tr><td>{{ c.when }}</td><td>{{ c.company }}</td><td>{{ c.old_status or 'new' }} → {{ c.new_status }}</td></tr>{% endfor %}</table>{% endif %}

<h2>Job emails in the last day ({{ emails|length }})</h2>
{% for e in emails %}<div class="card"><b>{{ e.category.replace('_', ' ') }}</b> · {{ e.company or e.from_name }} · {{ e.subject }}<br><span class="muted">{{ e.summary }}</span></div>
{% else %}<p class="muted">No job emails.</p>{% endfor %}

<h2>Other flags ({{ other|length }})</h2>
{% for f in other %}<div class="card">{{ f.kind.replace('_', ' ') }}: {{ f.message }}</div>{% else %}<p class="muted">None.</p>{% endfor %}
</main></body></html>
"""


def _local(text: str | None, config: Config) -> str:
    return db.from_db_time(text).astimezone(config.submit_window.tz).strftime("%b %d %H:%M") if text else ""


def build(conn: sqlite3.Connection, config: Config, paths: Paths, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    tz = config.submit_window.tz
    since = db.to_db_time(now - timedelta(days=1))
    gate = submission_gate(conn, config, paths, now)
    budget = budget_status(conn, config, now)
    flags = conn.execute("SELECT * FROM flags WHERE status = 'open' ORDER BY created_at DESC").fetchall()
    highlights = []
    for h in conn.execute("""SELECT h.*, c.name AS company, p.title, p.url FROM highlights h
                             JOIN companies c ON c.id = h.company_id JOIN postings p ON p.id = h.posting_id
                             WHERE h.status = 'new' ORDER BY h.score DESC"""):
        people = [dict(p, linkedin=linkedin_search_url(p["name"], h["company"])) for p in json.loads(h["people_json"])]
        highlights.append(dict(h, people=people))
    people = [dict(p, linkedin=linkedin_search_url(p["name"], p["company"])) for p in conn.execute(
        """SELECT pe.*, c.name AS company FROM people pe JOIN companies c ON c.id = pe.company_id
           WHERE pe.status = 'new' ORDER BY pe.created_at DESC LIMIT 15""")]
    changes = [dict(c, when=_local(c["created_at"], config)) for c in conn.execute(
        """SELECT e.*, c.name AS company FROM events e JOIN applications a ON a.id = e.application_id
           JOIN companies c ON c.id = a.company_id WHERE e.event_type = 'status_change' AND e.created_at >= ?
           ORDER BY e.created_at DESC""", (since,))]
    return {
        "day": now.astimezone(tz).strftime("%A, %B %d, %Y"),
        "submitted_today": db.submissions_on(conn, now.astimezone(tz).date(), tz),
        "cap": config.max_applications_per_day, "spent": budget.spent, "budget": budget.limit, "mode": config.mode,
        "gate_ok": gate.allowed, "gate_reasons": "; ".join(gate.reasons),
        "urgent": [f for f in flags if f["severity"] == "urgent"],
        "other": [f for f in flags if f["severity"] != "urgent"],
        "ready": [dict(a) for a in conn.execute(
            """SELECT a.*, c.name AS company FROM applications a JOIN companies c ON c.id = a.company_id
               WHERE a.status IN ('prepared', 'needs_me') ORDER BY a.id""")],
        "highlights": highlights, "people": people, "changes": changes,
        "by_status": conn.execute("SELECT status, COUNT(*) FROM applications GROUP BY status ORDER BY 2 DESC").fetchall(),
        "emails": [dict(e) for e in conn.execute(
            """SELECT e.*, c.name AS company FROM emails e LEFT JOIN companies c ON c.id = e.company_id
               WHERE e.category != 'other' AND e.received_at >= ? ORDER BY e.received_at DESC""", (since,))],
    }


def write_digest(conn: sqlite3.Connection, config: Config, paths: Paths, now: datetime | None = None) -> Path:
    from applier.mail.process import expire_past_flags

    now = now or datetime.now(timezone.utc)
    expire_past_flags(conn, now.astimezone(config.submit_window.tz))
    env = Environment(autoescape=select_autoescape(default=True, default_for_string=True))
    html = env.from_string(TEMPLATE).render(**build(conn, config, paths, now))
    folder = paths.root / "digest"
    folder.mkdir(exist_ok=True)
    out = folder / f"{now.astimezone(config.submit_window.tz):%Y-%m-%d}.html"
    out.write_text(html, encoding="utf-8")
    (folder / "latest.html").write_text(html, encoding="utf-8")
    return out
