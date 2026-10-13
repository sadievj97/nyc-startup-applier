"""Command line: `python -m applier <command>`. Messages are written for a non-programmer."""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

from applier import db
from applier.blocklist import BlockReport, sync_company_blocks
from applier.companies import company_key, load_aliases
from applier.config import Config, ConfigError, load_config, load_targets
from applier.envkeys import key_state, load_env, setup_logging, template_has_secret
from applier.paths import Paths, default_paths
from applier.config import load_targets
from applier.jobs.http import PoliteClient
from applier.jobs.pipeline import detect_missing, discover_hn, fetch_all
from applier.jobs.store import choose_per_company, is_new, score_all, sync_companies_csv
from applier.profile import load_profile
from applier.resume.build import build_master
from applier.resume.extract import run_extract
from applier.resume.model import load_groups, load_master, save_master
from applier.resume.render import RenderError, base_document, render_pdf, resume_filename
from applier.resume.review import write_review
from applier.resume.sources import list_sources
from applier.resume.verify import verify_master
from applier.safety import budget_status, in_submit_window, submission_gate

MODE_MEANING = {
    "dry_run": "fills forms, never clicks Submit",
    "review": "prepares everything, waits for your approval",
    "auto": "submits without waiting, within all limits",
}

# Personal files that are kept out of git; created from their .example versions if missing.
PERSONAL_FILES = {
    "applied_before.csv": "applied_before.example.csv",
    "do_not_apply.csv": "do_not_apply.example.csv",
    "company_aliases.csv": "company_aliases.example.csv",
}


@dataclass
class Context:
    paths: Paths
    config: Config
    conn: sqlite3.Connection
    blocks: BlockReport


def _ensure_personal_files(paths: Paths) -> list[str]:
    created = []
    for name, example in PERSONAL_FILES.items():
        target = paths.config_dir / name
        source = paths.config_dir / example
        if not target.exists() and source.exists():
            shutil.copyfile(source, target)
            created.append(name)
    return created


def _open(paths: Paths) -> Context:
    load_env(paths.env_file)
    setup_logging(paths.logs_dir, paths.env_file)
    config = load_config(paths.config_file)
    _ensure_personal_files(paths)
    conn = db.open_db(config.database_file(paths.root))
    blocks = sync_company_blocks(conn, paths, config)
    return Context(paths, config, conn, blocks)


def _relative(paths: Paths, path: Path) -> str:
    try:
        return str(path.relative_to(paths.root))
    except ValueError:
        return str(path)


def _row(label: str, value: str) -> None:
    print(f"  {label:<20} {value}")


# ---------------------------------------------------------------- commands


def cmd_init(ctx: Context, _args: argparse.Namespace) -> int:
    print("Database ready at", _relative(ctx.paths, ctx.config.database_file(ctx.paths.root)))
    print(f"Blocked companies loaded: {ctx.blocks.total}")
    for error in ctx.blocks.errors:
        print("  WARNING:", error)
    return 0


def cmd_status(ctx: Context, _args: argparse.Namespace) -> int:
    paths, config, conn = ctx.paths, ctx.config, ctx.conn
    now = db.utc_now()
    tz = config.submit_window.tz
    local = now.astimezone(tz)

    print("NYC startup applier: status")
    print()
    _row("Mode", f"{config.mode} ({MODE_MEANING[config.mode]})")
    _row("Master switch", "on" if config.enabled else "OFF (enabled: false in config.yaml)")
    _row("PAUSE file", "PRESENT: nothing will be submitted" if paths.pause_file.exists() else "not present")
    window_state = "open" if in_submit_window(config, now) else "closed"
    _row(
        "Submit window",
        f"{config.submit_window.start:%H:%M} to {config.submit_window.end:%H:%M} New York time "
        f"(now {local:%H:%M}, {window_state})",
    )
    _row("Submitted today", f"{db.submissions_on(conn, local.date(), tz)} of {config.max_applications_per_day}")
    _row(
        "Approval needed",
        f"for the first {config.review_first_n} applications ({db.submissions_total(conn)} submitted so far)",
    )

    state = key_state(paths.env_file, paths.env_example_file, "ANTHROPIC_API_KEY")
    key_text = {
        "filled": "filled in",
        "placeholder": "not filled in yet: paste your key on the ANTHROPIC_API_KEY line of .env",
        "missing": "missing: add an ANTHROPIC_API_KEY line to .env",
    }[state]
    _row("Claude API key", key_text)
    if template_has_secret(paths.env_example_file):
        print("    WARNING: .env.example contains what looks like a real key. That file is saved in git;")
        print("    move the key to .env and put a placeholder back in .env.example.")
    budget = budget_status(conn, config, now)
    _row("API spending today", f"${budget.spent:.2f} of ${budget.limit:.2f}")

    try:
        load_profile(paths.profile_file)
        _row("Profile", f"{_relative(paths, paths.profile_file)} is valid")
    except ConfigError as exc:
        _row("Profile", "PROBLEM, see below")
        print("    " + str(exc).replace("\n", "\n    "))
    try:
        targets = load_targets(paths.targets_file)
        _row("Targets", f"{_relative(paths, paths.targets_file)} is valid ({len(targets.role_families)} role families)")
    except ConfigError as exc:
        _row("Targets", "PROBLEM, see below")
        print("    " + str(exc).replace("\n", "\n    "))

    parts = [
        f"{ctx.blocks.counts.get('do_not_apply', 0)} from do_not_apply.csv",
        f"{ctx.blocks.counts.get('applied_before', 0)} from applied_before.csv",
        f"{ctx.blocks.counts.get('jobs_folder', 0)} from folders",
    ]
    _row("Blocked companies", f"{ctx.blocks.total} ({', '.join(parts)})")
    for error in ctx.blocks.errors:
        print("    WARNING:", error)

    counts = db.table_counts(conn)
    _row(
        "Database",
        f"{_relative(paths, config.database_file(paths.root))}: {counts['companies']} companies, "
        f"{counts['postings']} postings, {counts['applications']} applications, {counts['open_flags']} open flags",
    )

    gate = submission_gate(conn, config, paths, now)
    print()
    if gate.allowed:
        print("Could the agent submit right now?  Yes")
    else:
        print("Could the agent submit right now?  No, because:")
        for reason in gate.reasons:
            print("  -", reason)
    return 0


def cmd_blocks(ctx: Context, args: argparse.Namespace) -> int:
    query = "SELECT * FROM company_blocks"
    params: tuple = ()
    if args.reason:
        query += " WHERE reason = ?"
        params = (args.reason,)
    rows = ctx.conn.execute(query + " ORDER BY display_name COLLATE NOCASE", params).fetchall()
    print(f"{len(rows)} blocked companies (the agent never applies to these):")
    for row in rows:
        watched = "highlights on" if row["highlight"] else "highlights off"
        print(f"  {row['display_name']:<45} {row['reason']:<15} {watched}")
    for error in ctx.blocks.errors:
        print("WARNING:", error)
    return 0


def cmd_check_company(ctx: Context, args: argparse.Namespace) -> int:
    key = company_key(args.name, load_aliases(ctx.paths.aliases_csv))
    print(f'"{args.name}" is matched as company key "{key}"')
    block = db.is_blocked(ctx.conn, key)
    if block:
        print(f"  Blocked: yes ({block['reason']}, from {block['source']})")
        print(f"  Highlights: {'on' if block['highlight'] else 'off'}")
    else:
        print("  Blocked: no")
    app = db.existing_application(ctx.conn, key)
    if app:
        print(f"  Existing application: #{app['id']} {app['role_title']} ({app['status']})")
    else:
        print("  Existing application: none")
    can_apply = not block and not app
    print("  The agent " + ("may" if can_apply else "will NOT") + " apply here.")
    return 0


def cmd_pause(ctx: Context, _args: argparse.Namespace) -> int:
    ctx.paths.pause_file.write_text(
        "While this file exists, the applier submits nothing. Delete it (or run resume) to continue.\n",
        encoding="utf-8",
    )
    print("Paused. A PAUSE file now exists, so nothing will be submitted.")
    return 0


def cmd_resume(ctx: Context, _args: argparse.Namespace) -> int:
    if ctx.paths.pause_file.exists():
        ctx.paths.pause_file.unlink()
        print("Resumed. The PAUSE file was removed. All other limits still apply.")
    else:
        print("Not paused: there was no PAUSE file.")
    return 0


def cmd_reset_company(ctx: Context, args: argparse.Namespace) -> int:
    key = company_key(args.name, load_aliases(ctx.paths.aliases_csv))
    app = db.existing_application(ctx.conn, key)
    if app is None:
        print(f'No application on record for "{args.name}" (key "{key}"). Nothing to reset.')
        return 0
    if not args.yes:
        print(f"This would archive application #{app['id']} ({app['role_title']}, {app['status']})")
        print("and allow one new application to this company. Run again with --yes to do it.")
        return 1
    db.reset_company(ctx.conn, key, args.reason or "reset by user")
    print(f"Reset. Application #{app['id']} was archived; the company can receive one new application.")
    return 0


def _profile_build(ctx: Context) -> int:
    paths = ctx.paths
    sources = [p.name for p in list_sources(paths.source_dir)]
    if not sources:
        print(f"No resume files in {_relative(paths, paths.source_dir)}. Add .docx or .pdf files there first.")
        return 1
    items = run_extract(paths.source_dir, paths.extracted_file)
    print(f"Read {len(sources)} resume files: {len(items)} distinct paragraphs.")
    master, report = build_master(load_groups(paths.groups_file), items, sources)
    if paths.master_file.exists():
        previous = load_master(paths.master_file)
        if previous.meta.status == "confirmed" and previous.content_hash() == master.content_hash():
            master.meta = previous.meta  # nothing changed: keep the confirmation
    save_master(paths.master_file, master)
    groups = master.all_groups()
    print(f"Master resume saved: {len(groups)} groups, {sum(len(g.variants) for g in groups)} wordings "
          f"({master.meta.status}).")
    for conflict_id, count in sorted(report.disputed.items()):
        print(f"  {count:>3} wording(s) wait on question {conflict_id}")
    for conflict_id, count in sorted(report.excluded.items()):
        print(f"  {count:>3} wording(s) left out because of {conflict_id}")
    if report.empty_groups:
        print("  Groups with no wording:", ", ".join(report.empty_groups))
    if report.unassigned:
        print(f"{len(report.unassigned)} paragraph(s) did not match any group (add a rule in resume_groups.yaml):")
        for para in report.unassigned:
            print(f"  {para.id}: {para.text[:110]}")
        return 1
    return 0


def cmd_profile(ctx: Context, args: argparse.Namespace) -> int:
    paths = ctx.paths
    if args.action == "extract":
        items = run_extract(paths.source_dir, paths.extracted_file)
        print(f"{len(items)} distinct paragraphs written to {_relative(paths, paths.extracted_file)}")
        return 0
    if args.action == "build":
        return _profile_build(ctx)

    master = load_master(paths.master_file)
    if args.action == "review":
        out = write_review(master, paths.review_file)
        print(f"Review page written: {out}")
        print("Open it by double-clicking the file, or paste its path into your browser's address bar.")
        return 0

    report = verify_master(master, paths.source_dir)
    if args.action == "verify" or not report.ok:
        for note in report.notes:
            print("  note:", note)
        for problem in report.problems:
            print("  PROBLEM:", problem)
        print("Master resume check:", "passed" if report.ok else f"{len(report.problems)} problem(s)")
        return 0 if report.ok else 1

    # confirm
    master.meta.status = "confirmed"
    master.meta.confirmed_at = db.to_db_time(db.utc_now())
    master.meta.content_hash = master.content_hash()
    save_master(paths.master_file, master)
    print("Master resume confirmed. Tailoring may now use it; any later change needs a new confirmation.")
    return 0


def cmd_render(ctx: Context, args: argparse.Namespace) -> int:
    paths = ctx.paths
    try:
        profile = load_profile(paths.profile_file)
        master = load_master(paths.master_file)
    except ConfigError as exc:
        print(exc)
        return 2
    doc = base_document(master, profile)
    out = paths.rendered_dir / resume_filename(profile.name.full, args.company)
    max_pages = 2 if ctx.config.allow_two_pages else 1
    try:
        result = render_pdf(doc, out, paths.browsers_dir, max_pages=max_pages)
    except RenderError as exc:
        print("Could not render:", exc)
        return 1
    status = "DRAFT (master resume not confirmed yet)" if doc.draft_note else "final"
    print(f"Resume saved: {result.path}")
    print(f"  {result.pages} page(s), {result.font_pt}pt font, {status}")
    return 0


# ---------------------------------------------------------------- finding jobs (Phase 3)


def _money_range(row) -> str:
    low, high = row["pay_min"], row["pay_max"]
    if low is None and high is None:
        return "pay not posted"
    unit = "" if (row["pay_interval"] or "year") == "year" else f" per {row['pay_interval']}"
    parts = [f"${v:,.0f}" for v in (low, high) if v is not None]
    return " to ".join(parts) + unit


def _print_posting(row, now, config, verbose: bool = False) -> None:
    new = " NEW" if is_new(row["first_seen"], config, now) else ""
    place = row["location"] or "no location"
    print(f"{row['score']:>4}  #{row['id']:<5} {row['company_name']} | {row['title']}{new}")
    print(f"       {place} ({row['workplace_type']}) | {_money_range(row)} | {row['ats']}")
    print(f"       {row['url']}")
    if verbose:
        for line in (row["score_explanation"] or "").splitlines():
            print("       " + line)
    else:
        print("       " + (row["score_explanation"] or "").splitlines()[0][:220])


def _score_and_choose(ctx: Context) -> tuple[dict, dict]:
    targets = load_targets(ctx.paths.targets_file)
    return score_all(ctx.conn, targets, ctx.config), choose_per_company(ctx.conn, ctx.config)


def cmd_companies(ctx: Context, args: argparse.Namespace) -> int:
    count = sync_companies_csv(ctx.conn, ctx.paths, load_aliases(ctx.paths.aliases_csv))
    print(f"companies.csv: {count} row(s) loaded.")
    rows = ctx.conn.execute(
        """SELECT c.*, (SELECT COUNT(*) FROM postings p WHERE p.company_id = c.id AND p.closed_at IS NULL) AS open_postings,
                  EXISTS (SELECT 1 FROM company_blocks b WHERE b.company_key = c.company_key) AS blocked
           FROM companies c ORDER BY c.name COLLATE NOCASE"""
    ).fetchall()
    print(f"{len(rows)} companies tracked:")
    for row in rows:
        board = f"{row['ats']}:{row['board_token']}" if row["board_token"] else row["ats"]
        flag = "  [never apply]" if row["blocked"] else ""
        if row["discovery_status"] != "approved":
            flag += f"  [{row['discovery_status']}]"
        print(f"  {row['name'][:38]:<38} {row['discovery_source']:<13} {board[:34]:<34} {row['open_postings']:>3} open{flag}")
    return 0


def cmd_discover_hn(ctx: Context, args: argparse.Namespace) -> int:
    with PoliteClient() as client:
        result = discover_hn(ctx.conn, client, load_aliases(ctx.paths.aliases_csv), args.month)
    if result.thread is None:
        print("No 'Ask HN: Who is hiring?' thread found" + (f" for {args.month}" if args.month else "") + ".")
        return 1
    print(f"{result.thread}: {len(result.companies)} posts mention New York.")
    print("  " + ", ".join(result.companies[:60]) + (" ..." if len(result.companies) > 60 else ""))
    return 0


def cmd_detect(ctx: Context, args: argparse.Namespace) -> int:
    with PoliteClient() as client:
        results = detect_missing(ctx.conn, client, args.limit)
    if not results:
        print("Every company already has a known job board (or is marked manual).")
    for name, ref in results:
        print(f"  {name[:40]:<40} " + (f"{ref.ats}:{ref.token or '-'}  ({ref.how})" if ref else "no supported board found: marked manual"))
    return 0


def cmd_fetch(ctx: Context, args: argparse.Namespace) -> int:
    run_id = db.start_run(ctx.conn, "fetch", ctx.config.mode)
    with PoliteClient() as client:
        summary = fetch_all(ctx.conn, client, load_targets(ctx.paths.targets_file), db.utc_now(), args.company)
    note = (f"{summary.boards} boards read: {summary.counts.new} new, {summary.counts.updated} updated, "
            f"{summary.counts.closed} closed postings")
    db.finish_run(ctx.conn, run_id, "ok" if not summary.errors else "failed", note)
    print(note)
    for name in summary.missing:
        print(f"  board not found: {name} (flagged)")
    for error in summary.errors:
        print(f"  error: {error} (flagged)")
    return 0


def cmd_score(ctx: Context, args: argparse.Namespace) -> int:
    scored, chosen = _score_and_choose(ctx)
    print("Filters:", ", ".join(f"{k} {v}" for k, v in sorted(scored.items())) or "nothing to score")
    print("Choices:", ", ".join(f"{k} {v}" for k, v in sorted(chosen.items())) or "none")
    return 0


def cmd_postings(ctx: Context, args: argparse.Namespace) -> int:
    decision = args.decision or "chosen"
    rows = ctx.conn.execute(
        """SELECT p.*, c.name AS company_name FROM postings p JOIN companies c ON c.id = p.company_id
           WHERE p.closed_at IS NULL AND p.decision = ? ORDER BY p.score DESC, p.first_seen DESC LIMIT ?""",
        (decision, args.limit),
    ).fetchall()
    totals = ctx.conn.execute(
        "SELECT decision, COUNT(*) AS n FROM postings WHERE closed_at IS NULL GROUP BY decision ORDER BY n DESC"
    ).fetchall()
    print("Open postings by decision: " + ", ".join(f"{r['decision']} {r['n']}" for r in totals))
    print(f"\n{decision} ({len(rows)} shown):")
    now = db.utc_now()
    for row in rows:
        _print_posting(row, now, ctx.config)
    return 0


def cmd_explain(ctx: Context, args: argparse.Namespace) -> int:
    row = ctx.conn.execute(
        "SELECT p.*, c.name AS company_name FROM postings p JOIN companies c ON c.id = p.company_id WHERE p.id = ?",
        (args.posting_id,),
    ).fetchone()
    if row is None:
        print(f"No posting #{args.posting_id}.")
        return 1
    _print_posting(row, db.utc_now(), ctx.config, verbose=True)
    print(f"       Decision: {row['decision']}" + (f" ({row['decision_reason']})" if row["decision_reason"] else ""))
    print(f"       First seen {row['first_seen']}; published {row['published_at'] or 'unknown'}")
    return 0


def _claude_client(ctx: Context):
    """A Claude API client, or None with instructions if the key is not filled in."""
    state = key_state(ctx.paths.env_file, ctx.paths.env_example_file, "ANTHROPIC_API_KEY")
    if state != "filled":
        print("The Claude API key is not filled in: paste it on the ANTHROPIC_API_KEY line of .env, then save.")
        return None
    import anthropic

    return anthropic.Anthropic()


def cmd_research(ctx: Context, args: argparse.Namespace) -> int:
    from applier.jobs import research

    client = _claude_client(ctx)
    if client is None:
        return 1
    rows = research.companies_to_research(ctx.conn, args.refresh, args.company, args.limit)
    if not rows:
        print("No company needs research (facts are recent, or no posting is worth considering yet).")
        return 0
    print(f"Checking startup facts for {len(rows)} companies (web search, {ctx.config.models.research})...")
    run_id = db.start_run(ctx.conn, "research")
    spent = 0.0
    http = PoliteClient()
    for row in rows:
        try:
            outcome = research.research_company(client, ctx.conn, ctx.config, row, run_id, http=http)
        except research.BudgetReached:
            db.add_flag(ctx.conn, "budget_reached", "Daily Claude API budget reached during company research")
            print("  Daily budget reached; stopping. The rest will be checked tomorrow.")
            break
        spent += outcome.cost_usd
        if outcome.facts is None:
            print(f"  {row['name'][:30]:<30} could not check ({outcome.error}); kept and flagged")
            continue
        f = outcome.facts
        summary = (f"founded {f.founded_year or '?'}, ~{f.headcount or '?'} people, "
                   f"{'private' if f.is_private else 'public' if f.is_private is False else 'ownership ?'}, "
                   f"{f.industry or 'industry ?'}")
        unsure = f" (not confirmed: {', '.join(outcome.uncertain)})" if outcome.uncertain else ""
        print(f"  {row['name'][:30]:<30} {summary}{unsure}  ${outcome.cost_usd:.3f}")
        if f.uncertain:
            print(f"  {'':<30} notes: {'; '.join(f.uncertain)[:200]}")
    db.finish_run(ctx.conn, run_id, "ok", f"researched {len(rows)} companies for ${spent:.2f}")
    print(f"Spent ${spent:.2f}. Run 'score' to apply the startup filter.")
    return 0


def cmd_propose_funded(ctx: Context, args: argparse.Namespace) -> int:
    from applier.jobs import research

    client = _claude_client(ctx)
    if client is None:
        return 1
    try:
        outcome = research.propose_funded(client, ctx.conn, ctx.config, load_aliases(ctx.paths.aliases_csv), args.days)
    except research.BudgetReached:
        print("Daily budget reached; try again tomorrow.")
        return 1
    if outcome.error:
        print(f"Funding search failed: {outcome.error}.")
        return 1
    print(f"Search returned {outcome.search_links} links; "
          f"{len(outcome.proposals)} new proposal(s), {len(outcome.skipped)} skipped.")
    for s in outcome.proposals:
        weak = "  (front page only: check)" if s.source_url.rstrip("/").count("/") <= 2 else ""
        print(f"  PROPOSED {s.name}: {' '.join(filter(None, [s.round, s.amount, s.announced]))}  {s.source_url}{weak}")
    for name, why in outcome.skipped:
        print(f"  skipped  {name}: {why[:150]}")
    if outcome.proposals:
        print("Approve with: approve-company \"NAME\"   or dismiss with: dismiss-company \"NAME\"")
    return 0


def _set_discovery(ctx: Context, name: str, status: str) -> int:
    key = company_key(name, load_aliases(ctx.paths.aliases_csv))
    row = ctx.conn.execute("SELECT * FROM companies WHERE company_key = ?", (key,)).fetchone()
    if row is None:
        print(f'No company "{name}" is tracked or proposed.')
        return 1
    with ctx.conn:
        ctx.conn.execute("UPDATE companies SET discovery_status = ? WHERE id = ?", (status, row["id"]))
    if status == "approved" and row["discovery_source"] == "funding_news":
        import csv

        with ctx.paths.companies_csv.open("a", encoding="utf-8", newline="") as handle:
            csv.writer(handle, lineterminator="\r\n").writerow(
                [row["name"], row["website"] or "", "", "", "", (row["notes"] or "")[:200]]
            )
        print(f"Approved {row['name']} and added it to companies.csv.")
    else:
        print(f"{row['name']}: {status}.")
    return 0


def cmd_approve_company(ctx: Context, args: argparse.Namespace) -> int:
    return _set_discovery(ctx, args.name, "approved")


def cmd_dismiss_company(ctx: Context, args: argparse.Namespace) -> int:
    return _set_discovery(ctx, args.name, "dismissed")


def _print_packet(result) -> None:
    report = result.report
    print(f"Packet: {result.directory}")
    if result.resume:
        print(f"  Resume: {result.resume.name} ({result.pages} page)")
    if result.cover_letter:
        print(f"  Cover letter: {result.cover_letter.words} words, {len(result.cover_letter.company_facts)} company "
              f"fact(s) from {len(result.cover_letter.pages)} page(s) of the company's site")
    for note in result.notes:
        print(f"  note: {note}")
    print(f"  Fact-check: {'PASSED' if report.passed else 'BLOCKED'}: {report.verbatim} verbatim line(s), "
          f"{report.reworded} reworded, {report.model_items} checked by the model")
    for finding in report.findings:
        print(f"    [{finding.by}] {finding.where}: {finding.problem}")
        if finding.text:
            print(f"      > {finding.text[:160]}")
    print(f"  Cost: ${result.cost_usd:.3f}")


def cmd_tailor(ctx: Context, args: argparse.Namespace) -> int:
    from applier.llm import BudgetReached, ModelAnswerError
    from applier.tailor.packet import build_packet

    client = _claude_client(ctx)
    if client is None:
        return 1
    try:
        result = build_packet(client, ctx.conn, ctx.config, ctx.paths, args.posting_id,
                              cover_letter=args.cover_letter, plant=args.plant_fake_metric)
    except BudgetReached:
        print("Daily Claude API budget reached; try again tomorrow.")
        return 1
    except (ModelAnswerError, ValueError) as exc:
        print(f"Could not tailor: {exc}")
        return 1
    _print_packet(result)
    return 0 if result.report.passed else 1


def cmd_check_packet(ctx: Context, args: argparse.Namespace) -> int:
    from applier.llm import BudgetReached
    from applier.tailor.packet import recheck_packet

    client = _claude_client(ctx)
    if client is None:
        return 1
    try:
        result = recheck_packet(client, ctx.conn, ctx.config, ctx.paths, args.posting_id)
    except BudgetReached:
        print("Daily Claude API budget reached; try again tomorrow.")
        return 1
    except ValueError as exc:
        print(exc)
        return 1
    _print_packet(result)
    return 0 if result.report.passed else 1


def cmd_lca(ctx: Context, args: argparse.Namespace) -> int:
    from applier.jobs import lca

    files = sorted((ctx.paths.data_dir / "lca").glob("LCA_Disclosure_Data_*.xlsx"))
    if not files:
        print("No LCA file in data/lca. Download it from the Department of Labor site (see README) and put it there.")
        return 1
    path = files[-1]
    current = lca.loaded_source(ctx.conn)
    if current and current["source_file"] == path.name and current["file_size"] == path.stat().st_size and not args.reload:
        print(f"{path.name} is already loaded ({current['rows_read']:,} filings, {current['loaded_at']}).")
    else:
        print(f"Reading {path.name}; this takes several minutes...", flush=True)
        summaries, rows = lca.summarize(path, progress=lambda n: print(f"  {n:,} filings read", flush=True))
        lca.save(ctx.conn, summaries, path, rows)
        print(f"Saved sponsorship history for {len(summaries):,} employer names from {rows:,} filings.")
    source = lca.loaded_source(ctx.conn)["source_file"]
    print("\nTracked companies:")
    for company in ctx.conn.execute("SELECT name, company_key FROM companies ORDER BY name COLLATE NOCASE"):
        row = lca.lookup(ctx.conn, company["company_key"])
        summary = (f"{row['filings']} filings ({row['certified']} certified, {row['nyc_filings']} New York)"
                   if row else "no filings found under this name")
        print(f"  {company['name'][:36]:<36} {summary}")
    print(f"\nSource: {source}. Run 'score' to add this to score explanations.")
    return 0


def cmd_find(ctx: Context, args: argparse.Namespace) -> int:
    print("1. Companies from companies.csv")
    listed = sync_companies_csv(ctx.conn, ctx.paths, load_aliases(ctx.paths.aliases_csv))
    print(f"   {listed} row(s)")
    if not args.no_hn:
        print("2. Hacker News 'Who is hiring?'")
        cmd_discover_hn(ctx, argparse.Namespace(month=None))
    print("3. Detecting job boards")
    cmd_detect(ctx, argparse.Namespace(limit=args.detect_limit))
    print("4. Reading job boards")
    cmd_fetch(ctx, argparse.Namespace(company=None))
    print("5. Filtering, scoring, one per company")
    cmd_score(ctx, args)
    print()
    return cmd_postings(ctx, argparse.Namespace(decision="chosen", limit=args.limit))

def _print_session(result) -> None:
    print(f"{result.company} | {result.title} (posting #{result.posting_id})")
    if result.application_id:
        print(f"Application #{result.application_id}: {result.status}")
    else:
        print(f"Status: {result.status}")
    for line in result.problems:
        print(f"  problem: {line}")
    for line in result.needs_me:
        print(f"  needs you: {line}")
    if getattr(result, "posts", None):
        print(f"  the page sent data to: {sorted(set(result.posts))}")
    if result.blocked_requests:
        print(f"  safety net blocked {len(result.blocked_requests)} submission request(s): {result.blocked_requests}")
    for shot in result.screenshots:
        print(f"  screenshot: {shot}")
    if result.answers_file:
        print(f"  answers to copy and paste: {result.answers_file}")
    if result.cost_usd:
        print(f"  Claude API cost: ${result.cost_usd:.2f}")


def _sync_folders(ctx: Context) -> None:
    from applier.apply.folders import sync_folders

    for note in sync_folders(ctx.conn, ctx.config, ctx.paths):
        print(f"  note: {note}")


def cmd_folders(ctx: Context, _args: argparse.Namespace) -> int:
    _sync_folders(ctx)
    where = ctx.config.applications_folder or str(ctx.paths.data_dir)
    print(f"Application folders and applications.csv are up to date in {where}")
    return 0


def cmd_dry_run(ctx: Context, args: argparse.Namespace) -> int:
    from applier.apply.session import StopApplication, dry_run
    from applier.llm import BudgetReached

    client = _claude_client(ctx)
    if client is None:
        return 1
    try:
        result = dry_run(client, ctx.conn, ctx.config, ctx.paths, args.posting_id, queue=args.queue,
                         headless=args.headless, linger_seconds=args.linger)
    except BudgetReached:
        print("Daily Claude API budget reached; try again tomorrow.")
        return 1
    except (StopApplication, ValueError) as exc:
        print(f"Stopped: {exc}")
        return 1
    _print_session(result)
    _sync_folders(ctx)
    print("Nothing was submitted (dry run).")
    return 0 if not result.problems else 1


def cmd_applications(ctx: Context, args: argparse.Namespace) -> int:
    rows = ctx.conn.execute(
        """SELECT a.id, c.name, a.role_title, a.status, a.approved_at, a.submitted_at, a.needs_me_reason
           FROM applications a JOIN companies c ON c.id = a.company_id ORDER BY a.id DESC LIMIT ?""",
        (args.limit,)).fetchall()
    if not rows:
        print("No applications yet.")
    for r in rows:
        approved = " (approved)" if r["approved_at"] and not r["submitted_at"] else ""
        print(f"#{r['id']:<4} {r['status']:<12}{approved} {r['name']} | {r['role_title']}")
        if r["status"] == "needs_me" and r["needs_me_reason"]:
            print(f"       needs you: {r['needs_me_reason'][:200]}")
    return 0


def cmd_approve(ctx: Context, args: argparse.Namespace) -> int:
    from applier.apply.session import StopApplication, approve

    try:
        notes = approve(ctx.conn, ctx.config, args.application_id)
    except StopApplication as exc:
        print(exc)
        return 1
    print(f"Application #{args.application_id} approved.")
    _sync_folders(ctx)
    for note in notes:
        print(f"  {note}")
    return 0


def cmd_assist(ctx: Context, args: argparse.Namespace) -> int:
    from applier.apply.session import StopApplication, assisted
    from applier.llm import BudgetReached

    client = _claude_client(ctx)
    if client is None:
        return 1
    try:
        result = assisted(client, ctx.conn, ctx.config, ctx.paths, args.application_id, wait_minutes=args.wait)
    except BudgetReached:
        print("Daily Claude API budget reached; try again tomorrow.")
        return 1
    except (StopApplication, ValueError) as exc:
        print(f"Stopped: {exc}")
        return 1
    _print_session(result)
    _sync_folders(ctx)
    print("Submitted and confirmed." if result.submitted else "Not submitted (no confirmation page was seen).")
    return 0 if result.submitted else 1


# ---------------------------------------------------------------- email (Phase 6)

REAUTH_STEPS = """Google ended the Gmail sign-in (it does this every 7 days while your Google app is in Testing).
To sign in again: run  .venv/Scripts/python -m applier gmail-auth  (or ask Claude Code to), choose
your Google account in the browser, click Continue on the "Google hasn't verified this app" page, and allow
"Read your email". It takes about 30 seconds."""


def _gmail(ctx: Context):
    from applier.mail.gmail import GmailReauth, GmailSetupNeeded, service

    try:
        return service(ctx.paths)
    except GmailSetupNeeded as exc:
        print(f"Gmail is not set up yet: {exc}.")
    except GmailReauth:
        db.add_flag(ctx.conn, "gmail_reauth", "Google ended the Gmail sign-in; sign in again with gmail-auth",
                    severity="urgent")
        print(REAUTH_STEPS)
    return None


def cmd_gmail_auth(ctx: Context, _args: argparse.Namespace) -> int:
    from applier.mail.gmail import GmailSetupNeeded, authorize

    contact = load_profile(ctx.paths.profile_file).contact
    account = contact.inbox_account or contact.email
    print(f"Opening your browser: sign in as {account} and allow read-only access...")
    try:
        address = authorize(ctx.paths, contact.email, account)
    except GmailSetupNeeded as exc:
        print(f"Not signed in: {exc}.")
        return 1
    with ctx.conn:
        ctx.conn.execute("UPDATE flags SET status = 'resolved', resolved_at = strftime('%Y-%m-%dT%H:%M:%SZ','now') "
                         "WHERE kind = 'gmail_reauth' AND status = 'open'")
    print(f"Signed in as {address} (it holds {contact.email}) with read-only access.")
    return 0


def cmd_check_email(ctx: Context, args: argparse.Namespace) -> int:
    from applier.llm import BudgetReached
    from applier.mail.process import sync_inbox

    svc = _gmail(ctx)
    client = _claude_client(ctx) if svc else None
    if svc is None or client is None:
        return 1
    try:
        result = sync_inbox(client, ctx.conn, ctx.config, svc, limit=args.limit)
    except BudgetReached:
        print("Daily Claude API budget reached; try again tomorrow.")
        return 1
    print(f"Emails checked: {result.seen} ({result.skipped_not_job} not about jobs and never sent to the model, "
          f"{result.classified} sorted, {result.linked} linked to applications). Cost ${result.cost_usd:.3f}")
    for change in result.status_changes:
        print(f"  status: {change}")
    for flag in result.flags:
        print(f"  flag: {flag}")
    _sync_folders(ctx)
    return 0


def _known_keys(ctx: Context) -> set[str]:
    keys = {r[0] for r in ctx.conn.execute("SELECT company_key FROM company_blocks")}
    keys |= {r[0] for r in ctx.conn.execute("SELECT company_key FROM applications")}
    return keys


def cmd_propose_applied_before(ctx: Context, args: argparse.Namespace) -> int:
    import csv

    from applier.llm import BudgetReached
    from applier.mail.process import propose_applied_before

    svc = _gmail(ctx)
    client = _claude_client(ctx) if svc else None
    if svc is None or client is None:
        return 1
    try:
        proposals, usd = propose_applied_before(client, ctx.conn, ctx.config, svc, _known_keys(ctx), args.limit)
    except BudgetReached:
        print("Daily Claude API budget reached; try again tomorrow.")
        return 1
    out = ctx.paths.data_dir / "applied_before_proposals.csv"
    with out.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=["company", "applied_on", "role", "source", "subject"])
        writer.writeheader()
        writer.writerows(proposals)
    print(f"{len(proposals)} companies from application confirmations in the last 12 months that the agent did not "
          f"know (cost ${usd:.3f}). Nothing is saved until you confirm:")
    for p in proposals:
        print(f"  {p['applied_on']}  {p['company']}  ({p['role'] or 'role not named'})")
    print(f"List saved to {out}. Confirm with: confirm-applied-before --all  (or name the companies to keep)")
    return 0


def cmd_confirm_applied_before(ctx: Context, args: argparse.Namespace) -> int:
    import csv

    source = ctx.paths.data_dir / "applied_before_proposals.csv"
    if not source.exists():
        print("No proposals yet: run propose-applied-before first.")
        return 1
    with source.open(encoding="utf-8-sig") as handle:
        proposals = list(csv.DictReader(handle))
    wanted = {company_key(n) for n in args.names}
    chosen = [p for p in proposals if args.all or company_key(p["company"]) in wanted]
    if not chosen:
        print("Nothing chosen: pass --all or the company names to keep.")
        return 1
    target = ctx.paths.applied_before_csv
    with target.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        for p in chosen:
            writer.writerow([p["company"], "", p["applied_on"], p["role"], "inbox confirmation", "confirmed by you"])
    print(f"Added {len(chosen)} companies to config/applied_before.csv; they will never get a new application.")
    return 0


# ---------------------------------------------------------------- highlights, networking, digest, dashboard


def cmd_highlights(ctx: Context, _args: argparse.Namespace) -> int:
    from applier.network import find_highlights

    created = find_highlights(ctx.conn, ctx.config)
    rows = ctx.conn.execute("""SELECT h.*, c.name AS company, p.title FROM highlights h JOIN companies c ON c.id = h.company_id
                               JOIN postings p ON p.id = h.posting_id WHERE h.status = 'new' ORDER BY h.score DESC""").fetchall()
    print(f"{len(created)} new highlight(s); {len(rows)} not yet seen:")
    for h in rows:
        print(f"  {h['score']:>3}  {h['company']} | {h['title']}\n       {h['reason']}")
    return 0


def cmd_network(ctx: Context, args: argparse.Namespace) -> int:
    from applier.llm import BudgetReached
    from applier.network import companies_to_network, draft_note, find_highlights, find_people

    client = _claude_client(ctx)
    if client is None:
        return 1
    try:
        if args.note:
            person = ctx.conn.execute("SELECT * FROM people WHERE id = ?", (args.note,)).fetchone()
            if person is None:
                print(f"No person #{args.note}.")
                return 1
            result = draft_note(client, ctx.conn, ctx.config, ctx.paths, person)
            print(result.note)
            if result.status == "checked":
                print(f"(passed the fact-check; cost ${result.cost_usd:.3f})")
            else:
                print("NOT USABLE: the fact-check found: " + "; ".join(result.problems))
                print(f"(cost ${result.cost_usd:.3f})")
            return 0 if result.status == "checked" else 1
        find_highlights(ctx.conn, ctx.config)
        pairs = companies_to_network(ctx.conn, args.company)[: args.limit]
        if not pairs:
            print("No companies need people right now (they already have some, or there are no applications or highlights).")
        total = 0.0
        for company, posting in pairs:
            outcome = find_people(client, ctx.conn, ctx.config, company, posting)
            total += outcome.cost_usd
            print(f"{company['name']}: {len(outcome.added)} people added" + (f" ({outcome.error})" if outcome.error else ""))
            for reason in outcome.rejected:
                print(f"  not used: {reason}")
            if not args.no_notes:
                for person_id in outcome.added:
                    person = ctx.conn.execute("SELECT * FROM people WHERE id = ?", (person_id,)).fetchone()
                    total += draft_note(client, ctx.conn, ctx.config, ctx.paths, person).cost_usd
        print(f"Cost ${total:.2f}. See the Networking page of the dashboard, or the digest.")
    except BudgetReached:
        print("Daily Claude API budget reached; try again tomorrow.")
        return 1
    return 0


def cmd_digest(ctx: Context, args: argparse.Namespace) -> int:
    import os

    from applier.digest import write_digest
    from applier.notify import notify_urgent

    from datetime import datetime, timezone

    from applier.daily import due

    if args.scheduled and not due(ctx.conn, "digest", ctx.config.schedule.digest, ctx.config, datetime.now(timezone.utc)):
        print("Today's digest is already written; nothing to do.")
        return 0
    run_id = db.start_run(ctx.conn, "digest", ctx.config.mode)
    out = write_digest(ctx.conn, ctx.config, ctx.paths)
    sent = notify_urgent(ctx.conn, ctx.paths) if not args.no_notify else 0
    db.finish_run(ctx.conn, run_id, "ok", str(out))
    print(f"Digest written to {out}" + (f"; {sent} urgent notification(s) shown" if sent else ""))
    if args.open:
        os.startfile(out)  # noqa: S606 (opens the page in your default browser)
    return 0


def cmd_notify(ctx: Context, _args: argparse.Namespace) -> int:
    from applier.notify import notify_urgent

    print(f"{notify_urgent(ctx.conn, ctx.paths)} urgent notification(s) shown.")
    return 0


def cmd_dashboard(ctx: Context, args: argparse.Namespace) -> int:
    import threading
    import webbrowser

    from applier.dashboard import create_app

    app = create_app(ctx.paths)
    url = f"http://127.0.0.1:{args.port}/"
    print(f"Dashboard at {url} (only this computer can open it). Press Ctrl+C here to stop it.")
    if not args.no_open:
        from applier.mail.gmail import _chrome

        chrome = _chrome()  # your choice: Google Chrome when installed
        opener = webbrowser.get(chrome) if chrome else webbrowser
        threading.Timer(1.0, lambda: opener.open(url)).start()
    app.run(host="127.0.0.1", port=args.port, debug=False, use_reloader=False)
    return 0


# ---------------------------------------------------------------- daily run and submitting (Phase 7)


def cmd_submit(ctx: Context, args: argparse.Namespace) -> int:
    from applier.apply.session import StopApplication
    from applier.apply.submit import submit_application
    from applier.llm import BudgetReached

    client = _claude_client(ctx)
    if client is None:
        return 1
    try:
        result = submit_application(client, ctx.conn, ctx.config, ctx.paths, args.application_id, headless=args.headless)
    except BudgetReached:
        print("Daily Claude API budget reached; try again tomorrow.")
        return 1
    except (StopApplication, ValueError) as exc:
        print(f"Not submitted: {exc}")
        return 1
    _print_session(result)
    _sync_folders(ctx)
    print({"submitted": "Submitted and confirmed.",
           "submitted_unconfirmed": "Submit was clicked but no confirmation appeared; it will not be sent again."}
          .get(result.status, "Not submitted."))
    return 0 if result.submitted else 1


def cmd_daily(ctx: Context, args: argparse.Namespace) -> int:
    from datetime import datetime, timezone

    from applier.daily import due, run_daily
    from applier.mail.gmail import GmailReauth, GmailSetupNeeded, service

    now = datetime.now(timezone.utc)
    if args.scheduled and not due(ctx.conn, "daily", ctx.config.schedule.daily_run, ctx.config, now):
        print("The daily run already happened since the last scheduled time; nothing to do.")
        return 0
    client = _claude_client(ctx)
    gmail = None
    try:
        gmail = service(ctx.paths)
    except GmailSetupNeeded:
        pass
    except GmailReauth:
        db.add_flag(ctx.conn, "gmail_reauth", "Google ended the Gmail sign-in; run gmail-auth to sign in again",
                    severity="urgent")
    print(f"Daily run, mode {ctx.config.mode}, {now.astimezone(ctx.config.submit_window.tz):%Y-%m-%d %H:%M} New York time")
    result = run_daily(client, ctx.conn, ctx.config, ctx.paths, gmail=gmail, now=now, headless=args.headless,
                       log=lambda line: print(f"  {line}"))
    for line in result.prepared:
        print(f"  prepared: {line}")
    for line in result.submitted:
        print(f"  submitted: {line}")
    for line in result.waiting:
        print(f"  waiting: {line}")
    print(f"Cost of this run: ${result.cost_usd:.2f}")
    return 0


def cmd_check_proposals(ctx: Context, args: argparse.Namespace) -> int:
    from applier.jobs import research

    client = _claude_client(ctx)
    if client is None:
        return 1
    rows = ctx.conn.execute(
        """SELECT * FROM companies c WHERE discovery_status = 'proposed'
           AND NOT EXISTS (SELECT 1 FROM company_facts f WHERE f.company_id = c.id AND f.fact = 'description')
           ORDER BY created_at DESC, id LIMIT ?""", (args.limit,)).fetchall()
    if not rows:
        print("Every proposed startup has been checked already.")
        return 0
    run_id = db.start_run(ctx.conn, "proposal_check")
    spent = 0.0
    for row in rows:
        try:
            outcome = research.check_proposal(client, ctx.conn, ctx.config, row, run_id)
        except research.BudgetReached:
            print("Daily budget reached; the rest will be checked tomorrow.")
            break
        spent += outcome.cost_usd
        f = outcome.facts
        if f is None:
            print(f"{row['name']}: could not check ({outcome.error})")
            continue
        sure = lambda fact: "" if outcome.confirmed.get(fact) else " (not confirmed)"  # noqa: E731
        print(f"{row['name']}  [{f.website or 'website unknown'}]")
        print(f"  what it does: {f.description or 'unknown'}{sure('description')}")
        print(f"  New York: {'yes' if f.nyc_based else 'no' if f.nyc_based is False else 'unknown'}"
              f"{' (' + f.location + ')' if f.location else ''}{sure('nyc_based')}")
        print(f"  founded {f.founded_year or '?'}, about {f.headcount or '?'} people, {f.industry or 'industry ?'}; "
              f"funding: {f.funding or 'not confirmed'}{sure('funding')}")
    db.finish_run(ctx.conn, run_id, "ok", f"checked {len(rows)} proposals for ${spent:.2f}")
    print(f"Cost ${spent:.2f}. Approve with approve-company \"Name\" or dismiss with dismiss-company \"Name\".")
    return 0


def cmd_draft_answers(ctx: Context, args: argparse.Namespace) -> int:
    from applier.apply.session import StopApplication, draft_answers
    from applier.llm import BudgetReached

    client = _claude_client(ctx)
    if client is None:
        return 1
    try:
        answer_set, usd = draft_answers(client, ctx.conn, ctx.config, ctx.paths, args.posting_id)
    except BudgetReached:
        print("Daily Claude API budget reached; try again tomorrow.")
        return 1
    except (StopApplication, ValueError) as exc:
        print(f"Not drafted: {exc}")
        return 1
    for a in answer_set.answers:
        if a.category in ("free_text", "other", "current_company", "current_title", "current_company_role"):
            state = {"ok": "ready (fact-checked)", "skip": "left blank", "needs_me": "needs you"}[a.status]
            print()
            print(f"{a.label}\n  [{state}]" + (f" {a.reason}" if a.status != "ok" and a.reason else ""))
            if a.value:
                print("  " + str(a.value))
    print()
    print(f"Cost ${usd:.2f}. These exact answers are used when the form is filled next.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m applier", description="NYC startup job-application agent")
    parser.add_argument("--root", type=Path, help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="create the database and load the blocked-company lists")
    sub.add_parser("status", help="show settings, limits, and whether submitting is allowed now")
    blocks = sub.add_parser("blocks", help="list companies the agent will never apply to")
    blocks.add_argument("--reason", choices=db.BLOCK_REASONS)
    check = sub.add_parser("check-company", help="check whether the agent may apply to a company")
    check.add_argument("name")
    sub.add_parser("pause", help="create the PAUSE file (stops all submissions)")
    sub.add_parser("resume", help="remove the PAUSE file")
    reset = sub.add_parser("reset-company", help="allow one new application to a company (archives the old one)")
    reset.add_argument("name")
    reset.add_argument("--reason")
    reset.add_argument("--yes", action="store_true", help="confirm the reset")
    profile = sub.add_parser("profile", help="build, review and confirm your master resume")
    profile.add_argument("action", choices=["extract", "build", "verify", "review", "confirm"])
    render = sub.add_parser("render", help="make a resume PDF from the master resume")
    render.add_argument("--company", help="company name for the file name")
    sub.add_parser("companies", help="load companies.csv and list tracked companies")
    hn = sub.add_parser("discover-hn", help="add New York companies from HN 'Who is hiring?'")
    hn.add_argument("--month", help='e.g. "October 2026" (default: latest thread)')
    detect_p = sub.add_parser("detect", help="find the job board of companies with an unknown one")
    detect_p.add_argument("--limit", type=int, default=50)
    fetch_p = sub.add_parser("fetch", help="read postings from Ashby, Greenhouse and Lever boards")
    fetch_p.add_argument("--company")
    sub.add_parser("score", help="filter, score and choose at most one posting per company")
    postings_p = sub.add_parser("postings", help="list postings (default: the chosen one per company)")
    postings_p.add_argument("--decision", choices=db.POSTING_DECISIONS)
    postings_p.add_argument("--limit", type=int, default=25)
    explain_p = sub.add_parser("explain", help="show the full score explanation of a posting")
    explain_p.add_argument("posting_id", type=int)
    research_p = sub.add_parser("research", help="check startup facts (founded, size, private, industry) by web search")
    research_p.add_argument("--company")
    research_p.add_argument("--limit", type=int, default=25)
    research_p.add_argument("--refresh", action="store_true", help="re-check facts younger than 90 days")
    funded_p = sub.add_parser("propose-funded", help="propose recently funded New York startups (needs your approval)")
    funded_p.add_argument("--days", type=int, default=30)
    approve_p = sub.add_parser("approve-company", help="start tracking a proposed company")
    approve_p.add_argument("name")
    dismiss_p = sub.add_parser("dismiss-company", help="stop tracking or decline a proposed company")
    dismiss_p.add_argument("name")
    cp = sub.add_parser("check-proposals", help="look up proposed startups (what they do, New York, size) before you approve")
    cp.add_argument("--limit", type=int, default=10)
    tailor_p = sub.add_parser("tailor", help="build an application packet: tailored resume, fact-check, cover letter")
    tailor_p.add_argument("posting_id", type=int)
    tailor_p.add_argument("--cover-letter", action="store_true", help="also write a cover letter")
    tailor_p.add_argument("--plant-fake-metric", action="store_true",
                          help="acceptance test: add an invented number and confirm the gate blocks it")
    check_p = sub.add_parser("check-packet", help="re-run the fact-check after you edit a packet")
    check_p.add_argument("posting_id", type=int)
    lca_p = sub.add_parser("lca", help="load sponsorship history from the Department of Labor LCA file in data/lca")
    lca_p.add_argument("--reload", action="store_true")
    find_p = sub.add_parser("find", help="run every finding step: companies, HN, detect, fetch, score")
    find_p.add_argument("--no-hn", action="store_true")
    find_p.add_argument("--detect-limit", type=int, default=50)
    find_p.add_argument("--limit", type=int, default=25)
    dry_p = sub.add_parser("dry-run", help="fill a posting's live form and take screenshots; never submits")
    dry_p.add_argument("posting_id", type=int)
    dry_p.add_argument("--queue", action="store_true", help="also create the company's application (for approval)")
    dry_p.add_argument("--headless", action="store_true", help="do not show the browser window")
    dry_p.add_argument("--linger", type=int, default=0, help="keep the window open this many seconds")
    apps_p = sub.add_parser("applications", help="list applications and their status")
    apps_p.add_argument("--limit", type=int, default=30)
    approve_app = sub.add_parser("approve", help="approve one prepared application for submission")
    approve_app.add_argument("application_id", type=int)
    assist_p = sub.add_parser("assist", help="Ashby: fill the form in a window; you click Submit")
    assist_p.add_argument("application_id", type=int)
    assist_p.add_argument("--wait", type=int, default=30, help="minutes to wait for your click")
    sub.add_parser("folders", help="update the per-application folders and applications.csv")
    sub.add_parser("gmail-auth", help="sign in to Gmail with read-only access (opens your browser)")
    email_p = sub.add_parser("check-email", help="read new job emails, update statuses and raise flags")
    email_p.add_argument("--limit", type=int, default=300)
    pab = sub.add_parser("propose-applied-before", help="propose companies from application confirmations (12 months)")
    pab.add_argument("--limit", type=int, default=300)
    cab = sub.add_parser("confirm-applied-before", help="save proposed companies to applied_before.csv")
    cab.add_argument("names", nargs="*")
    cab.add_argument("--all", action="store_true")
    sub.add_parser("highlights", help="find strong new roles at companies you applied to or watch")
    net = sub.add_parser("network", help="find people (public professional sources) and draft 1 to 2 line notes")
    net.add_argument("--company")
    net.add_argument("--limit", type=int, default=5)
    net.add_argument("--no-notes", action="store_true")
    net.add_argument("--note", type=int, help="draft the note for one person (by id)")
    dig = sub.add_parser("digest", help="write today's digest page and show urgent notifications")
    dig.add_argument("--open", action="store_true")
    dig.add_argument("--no-notify", action="store_true")
    dig.add_argument("--scheduled", action="store_true", help="only if the 07:30 digest has not been written yet")
    sub.add_parser("notify", help="show Windows notifications for new urgent flags")
    dash = sub.add_parser("dashboard", help="open the local dashboard in your browser")
    dash.add_argument("--port", type=int, default=8765)
    dash.add_argument("--no-open", action="store_true")
    sub_p = sub.add_parser("submit", help="submit one approved Greenhouse or Lever application (every rule checked)")
    sub_p.add_argument("application_id", type=int)
    sub_p.add_argument("--headless", action="store_true")
    da = sub.add_parser("draft-answers", help="write a form's answers (essays) without opening it, to read first")
    da.add_argument("posting_id", type=int)
    daily_p = sub.add_parser("daily", help="the daily run: find, score, email, highlights, prepare, submit")
    daily_p.add_argument("--scheduled", action="store_true", help="only if a scheduled run was missed or is due")
    daily_p.add_argument("--headless", action="store_true")
    return parser


COMMANDS = {
    "init": cmd_init,
    "status": cmd_status,
    "blocks": cmd_blocks,
    "check-company": cmd_check_company,
    "pause": cmd_pause,
    "resume": cmd_resume,
    "reset-company": cmd_reset_company,
    "profile": cmd_profile,
    "render": cmd_render,
    "companies": cmd_companies,
    "discover-hn": cmd_discover_hn,
    "detect": cmd_detect,
    "fetch": cmd_fetch,
    "score": cmd_score,
    "postings": cmd_postings,
    "explain": cmd_explain,
    "lca": cmd_lca,
    "tailor": cmd_tailor,
    "check-packet": cmd_check_packet,
    "research": cmd_research,
    "propose-funded": cmd_propose_funded,
    "approve-company": cmd_approve_company,
    "dismiss-company": cmd_dismiss_company,
    "check-proposals": cmd_check_proposals,
    "find": cmd_find,
    "dry-run": cmd_dry_run,
    "applications": cmd_applications,
    "approve": cmd_approve,
    "assist": cmd_assist,
    "folders": cmd_folders,
    "gmail-auth": cmd_gmail_auth,
    "check-email": cmd_check_email,
    "propose-applied-before": cmd_propose_applied_before,
    "confirm-applied-before": cmd_confirm_applied_before,
    "highlights": cmd_highlights,
    "network": cmd_network,
    "digest": cmd_digest,
    "notify": cmd_notify,
    "dashboard": cmd_dashboard,
    "submit": cmd_submit,
    "daily": cmd_daily,
    "draft-answers": cmd_draft_answers,
}


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    args = build_parser().parse_args(argv)
    paths = Paths(args.root.resolve()) if args.root else default_paths()
    try:
        ctx = _open(paths)
    except ConfigError as exc:
        print(exc)
        return 2
    try:
        return COMMANDS[args.command](ctx, args)
    except ConfigError as exc:
        print(exc)
        return 2
    finally:
        ctx.conn.close()
