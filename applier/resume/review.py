"""A local HTML page showing every entry of the master resume, for the user to check (brief 5)."""

from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from applier.resume.model import MasterResume
from applier.resume.render import TEMPLATES
from applier.resume.verify import open_questions


def review_html(master: MasterResume) -> str:
    groups = master.all_groups()
    affected: dict[str, int] = {}
    for group in groups:
        for variant in group.variants:
            if variant.conflict:
                affected[variant.conflict] = affected.get(variant.conflict, 0) + 1
    stats = {
        "groups": len(groups),
        "variants": sum(len(g.variants) for g in groups),
        "disputed": sum(v.status == "disputed" for g in groups for v in g.variants),
        "open": len(open_questions(master)),
    }
    qnum = {c.id: i + 1 for i, c in enumerate(master.conflicts)}
    env = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape(["html", "j2"]))
    return env.get_template("review.html.j2").render(
        master=master, stats=stats, affected=affected, qnum=qnum, open_ids=set(open_questions(master))
    )


def write_review(master: MasterResume, out: Path) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(review_html(master), encoding="utf-8")
    return out
