# NYC startup applier

A personal job-application agent. Every day it finds new postings at New York startups,
picks at most one role per company, tailors your resume to it (every line traced to your confirmed master
resume and fact-checked), fills the company's own application form, and submits it only when every rule
allows. It reads your job email (read-only), keeps every application's status up to date, highlights strong
roles at companies you already applied to, suggests people to reach out to, and writes you a morning digest.

The full brief is in PROMPT.md; the rules and every decision so far are in CLAUDE.md.

## Your day

| When | What happens | What you do |
|---|---|---|
| 7:30 | The digest is written to `digest\latest.html`; urgent items pop up as Windows notifications. | Read it. |
| 8:15 | The daily run: new postings, scores, job email, highlights, people, prepared forms, approved submissions. | Nothing. |
| Any time | The dashboard shows what is waiting for you. | Approve, skip, or submit Ashby forms yourself. |

If the PC was asleep at those times, each job runs as soon as you log on or unlock it, once.
Submissions happen only between 08:00 and 20:00 New York time, at most 5 a day; anything prepared later waits.

## The dashboard

Run this, and it opens in your browser (only this computer can open it):

```
.venv\Scripts\python -m applier dashboard
```

- **Overview**: today's limits, urgent items, and Ashby forms ready for you to submit.
- **Review queue**: prepared applications (Approve, Skip, Mark applied manually, Submit now, Open filled form)
  and the chosen postings (Prepare, Skip, Mark applied manually).
- **Applications**, **Highlights**, **Networking** (people, source links, a LinkedIn search link to open
  yourself, and a 1 to 2 line note to copy), **Flags**, **Emails**.

## Modes (the `mode:` line in config\config.yaml)

- `dry_run`: fills forms and takes screenshots, never submits. Nothing is queued.
- `review`: prepares each application, then waits for your Approve. Greenhouse and Lever are then submitted
  by the agent; Ashby forms open filled in a window and you click Submit yourself.
- `auto`: submits without waiting, still under every rule; the first 10 submissions (`review_first_n`) still
  need your approval.

Change it in Notepad (right-click config\config.yaml, Open with, Notepad), save, and check with `status`.

## Stop everything at once

```
.venv\Scripts\python -m applier pause
```

This creates a file named PAUSE in this folder; nothing is submitted while it exists. `resume` removes it.
Setting `enabled: false` in config\config.yaml does the same.

## Where your things are

- `<your Jobs folder>\Claude Applications` (config.applications_folder): one folder per application (the resume sent, answers, screenshots,
  confirmation, job description) and `applications.csv`, which opens in Excel.
- `profile\`: your profile, master resume, writing style. Private, never in git.
- `config\`: settings (`config.yaml`), target roles (`targets.yaml`), companies to watch (`companies.csv`),
  and your lists: `applied_before.csv`, `do_not_apply.csv`. Any subfolder of your Jobs folder (config.do_not_apply_folders) also means
  "never apply" (except `Claude Applications`).
- `packets\`: the working files for each posting. `digest\`: the daily pages. `logs\`: what each run did.

## When something needs you

Flags appear in the digest and on the dashboard. The common ones:

- **Questions only you answer**: anything the profile does not cover, AI-use questions ("Are you an LLM?"),
  work authorization for countries other than the US, or whether you live in New York now.
- **Bot check**: a CAPTCHA or verification step stopped an application. The packet and answers are saved;
  finish it by hand in a few minutes.
- **Fact-check failed**: a tailored line, cover letter or note was not supported by your confirmed resume.
- **Gmail sign-in ended**: Google ends it every 7 days while your Google app is in Testing. Run
  `.venv\Scripts\python -m applier gmail-auth`, choose your Google account, click Continue, allow
  "Read your email" (about 30 seconds).
- **Unconfirmed submission**: Submit was clicked but no confirmation appeared. It is never sent again; check
  your inbox.

## Useful commands

All start with `.venv\Scripts\python -m applier`:

| Command | What it does |
|---|---|
| `status` | Settings, limits, and whether submitting is allowed right now |
| `daily` | The whole daily run now |
| `digest --open` | Write today's digest and open it |
| `applications` | List applications and their status |
| `approve <id>` | Approve one prepared application |
| `assist <id>` | Ashby: open the filled form; you click Submit |
| `submit <id>` | Greenhouse or Lever: submit one approved application |
| `dry-run <posting id>` | Fill one form without submitting (screenshots and answers saved) |
| `check-email` | Read new job email now |
| `network --company "X"` | Find people at a company and draft notes |
| `check-company "X"` | Whether the agent may apply to a company, and why not |
| `pause` / `resume` | Stop or allow all submissions |

## Costs

Claude API spending stops at `daily_budget_usd` (5 dollars) each New York day. For one day only, add a line
under `budget_overrides` in config\config.yaml (for example `2026-10-14: 6.0`). Each run logs its cost.

## Scheduling

```
powershell -ExecutionPolicy Bypass -File scripts\install_schedule.ps1
```

registers two tasks in Task Scheduler (folder "NYC Applier"): the 7:30 digest and the 8:15 daily run, each
also at log on and unlock, running only while you are logged on. `scripts\remove_schedule.ps1` removes them.

On a PC with Modern Standby (check with `powercfg /a`), programs pause while asleep, so the tasks cannot wake it. If it stays
plugged in, logged in (a locked screen is fine) and is set never to sleep on power (the screen may still turn
off), the jobs run on time; otherwise they run as soon as you unlock it.

## Tests

```
.venv\Scripts\python -m pytest
```
