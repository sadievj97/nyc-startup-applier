"""The local dashboard (brief 7): http://127.0.0.1:8765, only reachable from this computer.

Pages: overview, review queue, applications, highlights, networking, flags, emails. Buttons change only the
local database or start the same commands you could run yourself (dry-run, assist, network); every
submission still goes through the safety gate in those commands. Forms carry a secret token so another web
page cannot press the buttons for you.
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, abort, flash, redirect, render_template_string, request, url_for

from applier import db
from applier.config import load_config
from applier.network import linkedin_search_url
from applier.paths import Paths
from applier.safety import budget_status, submission_gate

STYLE = """
:root { --bg:#fbfaf7; --fg:#1d1d1b; --muted:#6b6a65; --line:#e4e1d8; --card:#fff; --accent:#1f5f8b; --urgent:#b3261e; --ok:#2e7d32; }
@media (prefers-color-scheme: dark) { :root { --bg:#161615; --fg:#ecebe6; --muted:#a19f97; --line:#33322e; --card:#1f1f1d; --accent:#7db7e0; --urgent:#f2948c; --ok:#81c784; } }
* { box-sizing:border-box; } body { margin:0; background:var(--bg); color:var(--fg); font:15px/1.5 Calibri, "Segoe UI", sans-serif; }
nav { display:flex; flex-wrap:wrap; gap:4px 16px; padding:12px 16px; border-bottom:1px solid var(--line); background:var(--card); position:sticky; top:0; }
nav a { color:var(--fg); text-decoration:none; } nav a.on { color:var(--accent); font-weight:600; }
main { max-width:1100px; margin:0 auto; padding:16px 16px 64px; }
h1 { font-size:22px; margin:8px 0; } h2 { font-size:17px; margin:24px 0 8px; }
.muted { color:var(--muted); } .urgent { color:var(--urgent); font-weight:600; } .ok { color:var(--ok); }
.card { background:var(--card); border:1px solid var(--line); border-radius:8px; padding:10px 12px; margin:8px 0; }
.flash { border-color:var(--accent); }
a { color:var(--accent); } table { border-collapse:collapse; width:100%; } .wrap { overflow-x:auto; }
td, th { text-align:left; padding:6px 8px 6px 0; border-bottom:1px solid var(--line); vertical-align:top; }
form.inline { display:inline; } button { font:inherit; padding:3px 10px; border-radius:6px; border:1px solid var(--line); background:var(--bg); color:var(--fg); cursor:pointer; margin:2px 4px 2px 0; }
button.primary { border-color:var(--accent); color:var(--accent); } pre { white-space:pre-wrap; font:inherit; background:var(--bg); padding:8px; border-radius:6px; margin:6px 0; }
input, select { font:inherit; padding:3px 6px; border:1px solid var(--line); border-radius:6px; background:var(--card); color:var(--fg); }
"""

LAYOUT = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Applier dashboard</title><style>{{ style }}</style></head><body>
<nav>{% for href, name in nav %}<a href="{{ href }}" class="{{ 'on' if href == here else '' }}">{{ name }}</a>{% endfor %}</nav>
<main>{% for m in get_flashed_messages() %}<div class="card flash">{{ m }}</div>{% endfor %}{{ body|safe }}</main>
<script>document.querySelectorAll('[data-copy]').forEach(b => b.addEventListener('click', () => {
  navigator.clipboard.writeText(document.getElementById(b.dataset.copy).innerText); b.innerText = 'Copied'; }));</script>
</body></html>"""

NAV = [("/", "Overview"), ("/review", "Review queue"), ("/applications", "Applications"), ("/highlights", "Highlights"),
       ("/networking", "Networking"), ("/flags", "Flags"), ("/emails", "Emails")]


def _button(action: str, label: str, token: str, primary: bool = False, **fields) -> str:
    hidden = "".join(f'<input type="hidden" name="{k}" value="{v}">' for k, v in fields.items())
    return (f'<form class="inline" method="post" action="/action/{action}"><input type="hidden" name="token" '
            f'value="{token}">{hidden}<button class="{"primary" if primary else ""}">{label}</button></form>')


def create_app(paths: Paths) -> Flask:
    app = Flask(__name__)
    token = _stable_secret(paths, "dashboard_token")  # kept across restarts, so open pages keep working
    app.secret_key = _stable_secret(paths, "dashboard_session_key")
    app.config["ACTION_TOKEN"] = token

    def conn_and_config():
        config = load_config(paths.config_file)
        return db.open_db(config.database_file(paths.root)), config

    def page(body: str) -> str:
        # body is already built with every outside text escaped; it is inserted, never rendered as a template
        return render_template_string(LAYOUT, style=STYLE, nav=NAV, here=request.path, body=body)

    def background(*args: str, visible: bool = False) -> None:
        """Run an applier command the same way you would; visible ones get their own window."""
        flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0) if visible else getattr(subprocess, "CREATE_NO_WINDOW", 0)
        paths.logs_dir.mkdir(parents=True, exist_ok=True)
        log = open(paths.logs_dir / "dashboard_jobs.log", "a", encoding="utf-8")  # noqa: SIM115 (kept open by the child)
        subprocess.Popen([sys.executable, "-m", "applier", *args], cwd=str(paths.root), creationflags=flags,
                         stdout=None if visible else log, stderr=None if visible else log)

    @app.before_request
    def local_only():
        if request.remote_addr not in ("127.0.0.1", "::1"):
            abort(403)
        if request.method == "POST":
            origin = request.headers.get("Origin") or request.headers.get("Referer") or ""
            if origin and not origin.startswith(("http://127.0.0.1", "http://localhost")):
                abort(403)
            if not secrets.compare_digest(request.form.get("token", ""), token):
                return page("<h1>This page is out of date</h1><p>The button came from an older copy of the dashboard, "
                            "so nothing was done. <a href='/review'>Open the review queue again</a> and click once "
                            "more.</p>"), 403

    @app.get("/")
    def overview():
        conn, config = conn_and_config()
        now = datetime.now(timezone.utc)
        tz = config.submit_window.tz
        gate = submission_gate(conn, config, paths, now)
        budget = budget_status(conn, config, now)
        urgent = conn.execute("SELECT * FROM flags WHERE status = 'open' AND severity = 'urgent' ORDER BY created_at DESC").fetchall()
        ashby = conn.execute("""SELECT a.*, c.name AS company FROM applications a JOIN companies c ON c.id = a.company_id
                                WHERE a.ats = 'ashby' AND a.status = 'prepared' AND a.approved_at IS NOT NULL""").fetchall()
        counts = conn.execute("SELECT status, COUNT(*) FROM applications GROUP BY status ORDER BY 2 DESC").fetchall()
        body = [f"<h1>Overview</h1><p>Mode <b>{config.mode}</b> · submitted today {db.submissions_on(conn, now.astimezone(tz).date(), tz)} "
                f"of {config.max_applications_per_day} · API spend today ${budget.spent:.2f} of ${budget.limit:.2f}</p>",
                f"<p class='{'ok' if gate.allowed else 'muted'}'>Submitting now: "
                f"{'allowed' if gate.allowed else 'off (' + '; '.join(gate.reasons) + ')'}</p>",
                "<p>Applications: " + (" · ".join(f"{s} {n}" for s, n in counts) or "none yet") + "</p>",
                f"<h2>Urgent ({len(urgent)})</h2>"]
        body += [f"<div class='card'><span class='urgent'>{_e(f['kind'].replace('_', ' '))}</span>"
                 f"{' · due ' + f['due_at'] if f['due_at'] else ''}<br>{_e(f['message'])}</div>" for f in urgent] or \
            ["<p class='muted'>Nothing urgent.</p>"]
        body.append(f"<h2>Ashby applications ready for you to submit ({len(ashby)})</h2>")
        body += [f"<div class='card'><b>{_e(a['company'])}</b>: {_e(a['role_title'])} "
                 + _button("assist", "Open filled form (you click Submit)", token, True, id=a["id"]) + "</div>" for a in ashby] or \
            ["<p class='muted'>None. Approve prepared Ashby applications in the review queue.</p>"]
        return page("".join(body))

    @app.get("/review")
    def review():
        conn, config = conn_and_config()
        apps = conn.execute("""SELECT a.*, c.name AS company FROM applications a JOIN companies c ON c.id = a.company_id
                               WHERE a.status IN ('queued', 'prepared', 'needs_me') ORDER BY a.id""").fetchall()
        chosen = conn.execute("""SELECT p.*, c.name AS company FROM postings p JOIN companies c ON c.id = p.company_id
                                 WHERE p.decision = 'chosen' AND p.closed_at IS NULL ORDER BY p.score DESC""").fetchall()
        gate = submission_gate(conn, config, paths)
        body = ["<h1>Review queue</h1>",
                "" if gate.allowed else f"<p class='muted'>Submitting is closed right now ({_e('; '.join(gate.reasons))}). "
                "You can still approve; Open filled form and Submit now work again when this is lifted.</p>",
                "<h2>Prepared applications</h2>"]
        for a in apps:
            actions = []
            if a["status"] in ("prepared", "needs_me") and not a["approved_at"]:
                actions.append(_button("approve", "Approve", token, True, id=a["id"]))
            if a["ats"] == "ashby" and a["approved_at"] and a["status"] == "prepared":
                actions.append(_button("assist", "Open filled form", token, True, id=a["id"]))
            if a["ats"] in ("greenhouse", "lever") and a["approved_at"] and a["status"] == "prepared":
                actions.append(_button("submit", "Submit now (every rule is checked)", token, True, id=a["id"]))
            actions += [_button("mark_applied", "Mark applied manually", token, id=a["id"]),
                        _button("skip_app", "Skip", token, id=a["id"])]
            if a["packet_dir"]:
                actions.append(_button("open_folder", "Open packet", token, path=a["packet_dir"]))
            body.append(f"<div class='card'><b>{_e(a['company'])}</b>: {_e(a['role_title'])} · {a['status']}"
                        f"{' · approved' if a['approved_at'] else ''} · score {a['score'] or ''}<br>"
                        + (f"<span class='muted'>Needs you: {_e(a['needs_me_reason'])}</span><br>" if a["needs_me_reason"] else "")
                        + "".join(actions) + "</div>")
        if not apps:
            body.append("<p class='muted'>Nothing prepared yet.</p>")
        body.append("<h2>Chosen postings (one per company)</h2>")
        for p in chosen:
            body.append(f"<div class='card'><b>{_e(p['company'])}</b>: <a href='{_e(p['url'] or '')}'>{_e(p['title'])}</a> · "
                        f"score {p['score']} · {_e(p['location'] or '')}<br>"
                        + _button("prepare", "Prepare (fill the form, no submit)", token, True, id=p["id"])
                        + _button("mark_applied_posting", "Mark applied manually", token, id=p["id"])
                        + _button("skip_posting", "Skip", token, id=p["id"]) + "</div>")
        return page("".join(body))

    @app.get("/applications")
    def applications():
        conn, config = conn_and_config()
        status, query = request.args.get("status", ""), request.args.get("q", "").strip()
        sql = """SELECT a.*, c.name AS company FROM applications a JOIN companies c ON c.id = a.company_id WHERE 1=1"""
        params: list = []
        if status:
            sql += " AND a.status = ?"
            params.append(status)
        if query:
            sql += " AND (c.name LIKE ? OR a.role_title LIKE ?)"
            params += [f"%{query}%", f"%{query}%"]
        rows = conn.execute(sql + " ORDER BY a.id DESC", params).fetchall()
        options = "".join(f"<option value='{s}' {'selected' if s == status else ''}>{s}</option>" for s in db.APPLICATION_STATUSES)
        body = [f"<h1>Applications</h1><form method='get'><select name='status'><option value=''>any status</option>{options}"
                f"</select> <input name='q' placeholder='company or role' value='{_e(query)}'> <button>Filter</button></form>",
                "<div class='wrap'><table><tr><th>#</th><th>Company</th><th>Role</th><th>Status</th><th>Score</th><th>Submitted</th><th></th></tr>"]
        for a in rows:
            submitted = db.from_db_time(a["submitted_at"]).astimezone(config.submit_window.tz).strftime("%b %d %H:%M") if a["submitted_at"] else ""
            body.append(f"<tr><td>{a['id']}</td><td>{_e(a['company'])}</td><td><a href='{_e(a['url'] or '')}'>{_e(a['role_title'])}</a></td>"
                        f"<td>{a['status']}</td><td>{a['score'] or ''}</td><td>{submitted}</td><td>"
                        + (_button("open_folder", "Folder", token, path=a["packet_dir"]) if a["packet_dir"] else "") + "</td></tr>")
        body.append("</table></div>")
        return page("".join(body))

    @app.get("/highlights")
    def highlights():
        conn, _ = conn_and_config()
        rows = conn.execute("""SELECT h.*, c.name AS company, p.title, p.url FROM highlights h
                               JOIN companies c ON c.id = h.company_id JOIN postings p ON p.id = h.posting_id
                               WHERE h.status != 'dismissed' ORDER BY h.status = 'new' DESC, h.score DESC""").fetchall()
        body = ["<h1>Highlights</h1><p class='muted'>Strong roles at companies the agent never applies to. Never applied to; "
                "reach out yourself if you like.</p>"]
        for h in rows:
            people = json.loads(h["people_json"])
            body.append(f"<div class='card'><b>{_e(h['company'])}</b>: <a href='{_e(h['url'] or '')}'>{_e(h['title'])}</a> · "
                        f"score {h['score']} · {h['status']}<br><span class='muted'>{_e(h['reason'])}</span>"
                        + "".join(f"<br>· {_e(p['name'])}, {_e(p['title'])} (<a href='{_e(p['source_url'])}'>source</a>, "
                                  f"<a href='{_e(linkedin_search_url(p['name'], h['company']))}'>LinkedIn search</a>)" for p in people)
                        + "<br>" + _button("highlight_status", "Seen", token, id=h["id"], status="seen")
                        + _button("highlight_status", "Dismiss", token, id=h["id"], status="dismissed")
                        + _button("find_people", "Find people", token, company=h["company"]) + "</div>")
        if not rows:
            body.append("<p class='muted'>No highlights yet.</p>")
        return page("".join(body))

    @app.get("/networking")
    def networking():
        conn, _ = conn_and_config()
        rows = conn.execute("""SELECT pe.*, c.name AS company, p.title AS role FROM people pe JOIN companies c ON c.id = pe.company_id
                               LEFT JOIN postings p ON p.id = pe.posting_id WHERE pe.status != 'dismissed'
                               ORDER BY c.name, pe.id""").fetchall()
        body = ["<h1>Networking</h1><p class='muted'>People from public professional sources only (the posting, the company's "
                "site, press releases, talk bios). No contact details are collected and nothing is ever sent for you: open the "
                "LinkedIn search yourself, copy the note, and send it if you want.</p>",
                _button("find_people", "Find people for my applications and highlights", token, True, company="")]
        current = None
        for p in rows:
            if p["company"] != current:
                current = p["company"]
                body.append(f"<h2>{_e(current)}</h2>")
            note = ""
            if p["note"]:
                note = (f"<pre id='note{p['id']}'>{_e(p['note'])}</pre>"
                        + (f"<button data-copy='note{p['id']}'>Copy note</button>" if p["note_status"] == "checked" else
                           "<span class='urgent'>This draft failed a check; edit it before using.</span>"))
            body.append(f"<div class='card'><b>{_e(p['name'])}</b>, {_e(p['title'] or '')}{' · for ' + _e(p['role']) if p['role'] else ''} · "
                        f"{p['status']}<br><span class='muted'>{_e(p['why'] or '')}</span><br>"
                        f"<a href='{_e(p['source_url'])}'>Source</a> · <a href='{_e(linkedin_search_url(p['name'], p['company']))}'>"
                        f"LinkedIn search</a>{note}<br>"
                        + ("" if p["note_status"] == "checked" else
                           _button("draft_note", "Draft again" if p["note"] else "Draft a 1 to 2 line note", token, id=p["id"]))
                        + _button("person_status", "Contacted", token, id=p["id"], status="contacted")
                        + _button("person_status", "Dismiss", token, id=p["id"], status="dismissed") + "</div>")
        if not rows:
            body.append("<p class='muted'>No people yet. Press the button above (it uses a few cents of the API budget per company).</p>")
        return page("".join(body))

    @app.get("/flags")
    def flags():
        conn, _ = conn_and_config()
        rows = conn.execute("SELECT * FROM flags WHERE status = 'open' ORDER BY severity = 'urgent' DESC, created_at DESC").fetchall()
        body = [f"<h1>Flags ({len(rows)} open)</h1>"]
        body += [f"<div class='card'><span class='{'urgent' if f['severity'] == 'urgent' else ''}'>{_e(f['kind'].replace('_', ' '))}</span> · "
                 f"<span class='muted'>{f['created_at'][:10]}</span><br>{_e(f['message'])}<br>"
                 + _button("flag", "Resolved", token, id=f["id"], status="resolved")
                 + _button("flag", "Dismiss", token, id=f["id"], status="dismissed") + "</div>" for f in rows]
        return page("".join(body))

    @app.get("/emails")
    def emails():
        conn, config = conn_and_config()
        rows = conn.execute("""SELECT e.*, c.name AS company FROM emails e LEFT JOIN companies c ON c.id = e.company_id
                               ORDER BY e.received_at DESC LIMIT 100""").fetchall()
        body = ["<h1>Job emails</h1><p class='muted'>Only job emails are read; only sender, subject, date, category and a "
                "one-line summary are kept.</p><div class='wrap'><table><tr><th>Date</th><th>Category</th><th>Company</th><th>Subject</th><th>Summary</th></tr>"]
        for e in rows:
            when = db.from_db_time(e["received_at"]).astimezone(config.submit_window.tz).strftime("%b %d") if e["received_at"] else ""
            body.append(f"<tr><td>{when}</td><td>{e['category']}</td><td>{_e(e['company'] or e['from_name'] or '')}</td>"
                        f"<td>{_e(e['subject'] or '')}</td><td>{_e(e['summary'] or '')}</td></tr>")
        body.append("</table></div>")
        return page("".join(body))

    @app.post("/action/<name>")
    def action(name: str):
        from applier.apply.session import StopApplication, approve

        conn, config = conn_and_config()
        form = request.form
        back = request.referrer or url_for("overview")
        try:
            if name == "approve":
                notes = approve(conn, config, int(form["id"]))
                flash("Approved. " + " ".join(notes))
            elif name in ("assist", "submit"):
                app_row = conn.execute("SELECT * FROM applications WHERE id = ?", (int(form["id"]),)).fetchone()
                gate = submission_gate(conn, config, paths, application=app_row)
                if not gate.allowed:  # say why here, instead of a window that closes at once
                    flash("Not opened: " + "; ".join(gate.reasons) + ". Nothing was sent.")
                elif name == "assist":
                    background("assist", form["id"], visible=True)
                    flash("Opening the filled form in a new window (about a minute). Check it and click Submit yourself.")
                else:
                    background("submit", form["id"], visible=True)
                    flash("Submitting in a new window. It fills the form, checks every rule again, then clicks Submit.")
            elif name == "prepare":
                background("dry-run", form["id"], "--queue", visible=True)
                flash("Preparing: the form is filled in a window and never submitted. Refresh in a minute or two.")
            elif name == "find_people":
                background("network", *(["--company", form["company"]] if form.get("company") else []))
                flash("Looking for people in the background. Refresh in a minute or two.")
            elif name == "draft_note":
                background("network", "--note", form["id"])
                flash("Drafting the note in the background. Refresh in a moment.")
            elif name == "mark_applied":
                db.mark_submitted(conn, int(form["id"]), "submitted", db.utc_now())
                with conn:
                    conn.execute("INSERT INTO events (application_id, event_type, details) VALUES (?, 'note', ?)",
                                 (int(form["id"]), "marked applied manually on the dashboard"))
                flash("Marked as applied.")
            elif name == "mark_applied_posting":
                row = conn.execute("SELECT * FROM postings WHERE id = ?", (int(form["id"]),)).fetchone()
                app_id = db.create_application(conn, row["company_id"], row["title"], posting_id=row["id"],
                                               url=row["apply_url"] or row["url"], ats=row["ats"], score=row["score"])
                db.mark_submitted(conn, app_id, "submitted", db.utc_now())
                flash("Recorded as applied manually; the agent will not apply there again.")
            elif name == "skip_app":
                app_row = conn.execute("SELECT * FROM applications WHERE id = ?", (int(form["id"]),)).fetchone()
                if app_row["submitted_at"]:
                    flash("This application was already submitted; it cannot be skipped.")
                else:
                    with conn:
                        if app_row["posting_id"]:
                            conn.execute("INSERT OR IGNORE INTO posting_skips (posting_id, reason) VALUES (?, 'skipped on the dashboard')",
                                         (app_row["posting_id"],))
                    db.reset_company(conn, app_row["company_key"], "skipped by you on the dashboard")
                    flash("Skipped. The posting will not be chosen again.")
            elif name == "skip_posting":
                with conn:
                    conn.execute("INSERT OR IGNORE INTO posting_skips (posting_id, reason) VALUES (?, 'skipped on the dashboard')",
                                 (int(form["id"]),))
                    conn.execute("UPDATE postings SET decision = 'manual', decision_reason = 'you chose to skip this posting' WHERE id = ?",
                                 (int(form["id"]),))
                flash("Skipped. The posting will not be chosen again.")
            elif name == "highlight_status" and form["status"] in ("seen", "dismissed"):
                with conn:
                    conn.execute("UPDATE highlights SET status = ? WHERE id = ?", (form["status"], int(form["id"])))
            elif name == "person_status" and form["status"] in ("contacted", "dismissed"):
                with conn:
                    conn.execute("UPDATE people SET status = ? WHERE id = ?", (form["status"], int(form["id"])))
            elif name == "flag" and form["status"] in ("resolved", "dismissed"):
                with conn:
                    conn.execute("UPDATE flags SET status = ?, resolved_at = strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id = ?",
                                 (form["status"], int(form["id"])))
            elif name == "open_folder":
                target = Path(form["path"]).resolve()
                if target.is_dir() and (paths.root.resolve() in target.parents or
                                        (config.applications_folder and Path(config.applications_folder).resolve() in target.parents)):
                    os.startfile(target)  # noqa: S606 (opens File Explorer)
                else:
                    flash("That folder is not part of the applier.")
            else:
                abort(400)
        except (StopApplication, db.AlreadyApplied, db.CompanyBlocked, ValueError) as exc:
            flash(f"Not done: {exc}")
        return redirect(back)

    return app


def _stable_secret(paths: Paths, name: str) -> str:
    """A random secret made once and kept in data/ (private, out of git)."""
    path = paths.data_dir / name
    if path.exists() and len(path.read_text(encoding="utf-8").strip()) >= 32:
        return path.read_text(encoding="utf-8").strip()
    path.parent.mkdir(parents=True, exist_ok=True)
    value = secrets.token_urlsafe(32)
    path.write_text(value, encoding="utf-8")
    return value


def _e(text) -> str:
    from markupsafe import escape

    return str(escape(text if text is not None else ""))
