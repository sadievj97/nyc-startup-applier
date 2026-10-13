"""Fill a form in a visible browser (brief 6, step 3), read every value back, and take a screenshot.

In dry runs two guards make submission impossible: requests that look like a submission are blocked and
logged, and any form 'submit' event is cancelled in the page. The agent never clicks a submit button in a
dry run, so the guards are a safety net, not the plan.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page

from applier.apply.answers import Answer

_SUBMIT_REQUEST = re.compile(r"submit", re.I)
# In a request body only an operation named like a submission counts (form definitions mention "submit" too).
_SUBMIT_BODY = re.compile(r'"operationName"\s*:\s*"[^"]*submit|mutation\s+\w*submit|"op"\s*:\s*"[^"]*submit', re.I)
_BLOCK_SUBMIT_EVENTS = """
document.addEventListener('submit', (event) => {
  event.preventDefault(); event.stopImmediatePropagation();
  window.__applierBlockedSubmit = (window.__applierBlockedSubmit || 0) + 1;
}, true);
"""


@dataclass
class GuardLog:
    blocked: list[str] = field(default_factory=list)
    posts: list[str] = field(default_factory=list)


def guard_dry_run(page: Page, block_paths: list[str] | tuple[str, ...] = ()) -> GuardLog:
    """Block anything that looks like a submission: requests named like one, and any POST to the posting's
    own page or apply page (where Greenhouse and Lever forms send applications). Records the URL of every
    POST (never the body)."""
    log = GuardLog()
    paths = [p.rstrip("/") for p in block_paths if p and p.strip("/")]

    def handle(route, request):
        if request.method == "POST":
            body = request.post_data or ""
            log.posts.append(request.url.split("?")[0])
            path = urlparse(request.url).path.rstrip("/")
            if (_SUBMIT_REQUEST.search(request.url) or _SUBMIT_BODY.search(body[:4000])
                    or any(path == p or path.startswith(p + "/") for p in paths)):
                log.blocked.append(request.url.split("?")[0])
                return route.abort()
        return route.continue_()

    page.route("**/*", handle)
    page.add_init_script(_BLOCK_SUBMIT_EVENTS)
    return log


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


VISIBLE_OPTION = "[role=option]:visible"


def _squash(text: str) -> str:
    return re.sub(r"\s+", "", text or "").lower()


def _search_text(value: str) -> str:
    """What to type into a searchable dropdown: the words before any code ('United States +1' -> 'United States')."""
    return re.split(r"\s*[+(]", value)[0].strip() or value


US_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado",
    "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia", "FL": "Florida", "GA": "Georgia",
    "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan",
    "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York", "NC": "North Carolina",
    "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania",
    "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas",
    "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
    "WI": "Wisconsin", "WY": "Wyoming",
}


def _part_found(part: str, option: str) -> bool:
    """A part of a place ('Boston', 'MA') appears in an option as a whole word, or as the state's full name."""
    names = [part] + ([US_STATES[part.upper()]] if part.upper() in US_STATES else [])
    return any(re.search(r"\b" + re.escape(n.lower()) + r"\b", option.lower()) for n in names)


def _best(texts: list[str], value: str) -> int | None:
    """Exact match first (ignoring spaces). A place ('Boston, MA') must match every part, so 'Boston,
    Virginia' never stands in for Boston, Massachusetts. Otherwise the shortest option containing the value
    ('United States' picks 'United States +1', not 'United States Minor Outlying Islands'). None if nothing fits."""
    for i, t in enumerate(texts):
        if _squash(t) == _squash(value):
            return i
    parts = [p.strip() for p in value.split(",") if p.strip()]
    if len(parts) > 1:
        found = [(len(t), i) for i, t in enumerate(texts) if all(_part_found(p, t) for p in parts)]
    else:
        found = [(len(t), i) for i, t in enumerate(texts) if value.lower().strip() in t.lower()]
    return min(found)[1] if found else None


def _pick_option(page: Page, box, value: str) -> str | None:
    """Open a dropdown or place search, type the value, and click the best matching option."""
    box.click()
    box.fill(_search_text(value))
    try:
        page.wait_for_selector(VISIBLE_OPTION, timeout=6000)
    except PlaywrightError:
        return None
    options = page.locator(VISIBLE_OPTION)
    texts = [t.strip() for t in options.all_inner_texts()]
    best = _best(texts, value)
    if best is None:
        page.keyboard.press("Escape")
        return None
    options.nth(best).click()
    return texts[best]


def _wait_for_upload(page: Page) -> None:
    """Wait until the upload and any resume reading are done: Lever's resume reader fills the form when it
    finishes and would wipe answers typed before then."""
    page.wait_for_timeout(1500)
    try:
        page.wait_for_load_state("networkidle", timeout=20000)
    except PlaywrightError:
        pass
    page.wait_for_timeout(1000)


# ---------------------------------------------------------------- Ashby


def _entry(page: Page, key: str):
    return page.locator(f'[data-field-path="{key}"]').first


def fill_ashby(page: Page, answers: list[Answer]) -> list[str]:
    problems = []
    # Files first: an uploaded resume can trigger the site's autofill, which must not overwrite answers.
    for a in sorted(answers, key=lambda a: a.kind != "file"):
        if a.status != "ok" or a.value in (None, "", []):
            continue
        entry = _entry(page, a.key)
        try:
            if a.kind == "file":
                entry.locator("input[type=file]").first.set_input_files(str(a.value))
                _wait_for_upload(page)
            elif a.kind in ("text", "email", "url", "tel", "number"):
                entry.locator("input").first.fill(str(a.value))
            elif a.kind == "textarea":
                entry.locator("textarea").first.fill(str(a.value))
            elif a.kind == "boolean":
                entry.get_by_role("button", name=str(a.value), exact=True).click()
            elif a.kind == "radio":
                entry.get_by_text(str(a.value), exact=True).first.click()
            elif a.kind == "checkboxes":
                for option in a.value:
                    entry.get_by_text(option, exact=True).first.click()
            elif a.kind in ("select", "location"):
                chosen = _pick_option(page, entry.locator("[role=combobox], input").first, str(a.value))
                if chosen is None:
                    a.status, a.reason = ("needs_me" if a.required else "skip"), f"no option matches '{a.value}'"
                    problems.append(f"{a.label}: no option matches '{a.value}'")
                else:
                    a.value = chosen
            else:
                problems.append(f"{a.label}: unsupported field type {a.kind}")
        except PlaywrightError as exc:
            problems.append(f"{a.label}: could not fill ({str(exc).splitlines()[0][:150]})")
    return problems


ASHBY_READBACK = r"""
() => {
  const clean = (t) => (t || '').replace(/\s+/g, ' ').trim();
  const out = {};
  for (const entry of document.querySelectorAll('[data-field-path]')) {
    const key = entry.getAttribute('data-field-path');
    const inputs = [...entry.querySelectorAll('input[type=text], input[type=email], input[type=url], input[type=tel], input[type=number], input:not([type]), textarea')];
    const buttons = [...entry.querySelectorAll('button')];
    const pressed = buttons.filter(b => b.getAttribute('aria-pressed') === 'true' ||
      /(_active|_selected|_checked|_pressed)/i.test(b.className)).map(b => clean(b.innerText));
    const checked = [...entry.querySelectorAll('input[type=checkbox]:checked, input[type=radio]:checked')]
      .map(i => clean((i.labels && i.labels[0] ? i.labels[0].innerText : (i.closest('label') || {}).innerText) || i.value));
    out[key] = {values: inputs.map(i => i.value), pressed, checked, text: clean(entry.innerText).slice(0, 300),
                buttonClasses: buttons.map(b => clean(b.innerText) + ':' + (b.className || '').slice(0, 120))};
  }
  return out;
}
"""


def verify_ashby(page: Page, answers: list[Answer]) -> list[str]:
    """Read every filled value back from the page; any mismatch is a problem."""
    return _compare(page.evaluate(ASHBY_READBACK), answers)


def _digits(text: str) -> str:
    return re.sub(r"\D", "", text or "")


def _compare(state: dict, answers: list[Answer]) -> list[str]:
    problems = []
    for a in answers:
        if a.status != "ok" or a.value in (None, "", []):
            continue
        got = state.get(a.key)
        if got is None and a.kind == "file" and Path(str(a.value)).name in state.get("__form_text__", ""):
            continue  # the upload box was replaced by the attached file's name
        if got is None:
            problems.append(f"{a.label}: field disappeared from the page")
            continue
        if a.kind == "tel" or a.category == "phone":
            if not any(_digits(v) and _digits(v)[-10:] == _digits(str(a.value))[-10:] for v in got["values"]):
                problems.append(f"{a.label}: reads back as {got['values'][:1]}")
        elif a.kind in ("text", "email", "url", "number", "textarea"):
            if str(a.value) not in got["values"]:
                problems.append(f"{a.label}: reads back as {got['values'][:1]}")
        elif a.kind == "file":
            name = Path(str(a.value)).name
            if name not in got["text"] and name not in got["values"]:
                problems.append(f"{a.label}: the file name does not show as attached")
        elif a.kind == "boolean":
            if got["pressed"] != [a.value]:
                problems.append(f"{a.label}: selected answer reads back as {got['pressed'] or 'nothing'} "
                                f"(buttons: {got['buttonClasses']})")
        elif a.kind in ("checkboxes", "radio"):
            wanted = a.value if isinstance(a.value, list) else [a.value]
            if not got["checked"] or not all(w in got["checked"] for w in wanted):
                problems.append(f"{a.label}: ticked options read back as {got['checked'] or 'none'}")
        elif a.kind in ("select", "location"):
            shown = " ".join([got["text"], *got["values"]]).lower()
            value = str(a.value)
            code = re.search(r"\+\d+$", value)  # a phone country shows only as a flag and its code ("+1")
            if _squash(value) not in _squash(shown) and not (code and re.search(re.escape(code.group(0)) + r"\b", shown)):
                problems.append(f"{a.label}: chosen option does not show")
    return problems


# ---------------------------------------------------------------- Greenhouse and Lever (fields found by selector)


def _tick(page: Page, selector: str, option: str) -> bool:
    """Tick the radio button or checkbox whose label reads `option`."""
    inputs = page.locator(selector)
    for i in range(inputs.count()):
        box = inputs.nth(i)
        text = box.evaluate("e => (e.closest('label') || {}).innerText || (e.labels && e.labels[0] ? e.labels[0].innerText : '') || e.value")
        if _clean(text) == _clean(option):
            try:
                box.check()
            except PlaywrightError:
                box.click(force=True)
            return True
    return False


def _choose_native(target, value: str) -> str | None:
    texts = [_clean(t) for t in target.evaluate("e => [...e.options].map(o => o.text)")]
    best = _best(texts, value)
    if best is None:
        return None
    target.select_option(index=best)
    return texts[best]


def _choose_place(page: Page, target, value: str) -> str:
    """Lever's 'Current location': type the place key by key (the search listens for key presses, and the
    field is cleared unless a suggestion is chosen), then click the best suggestion."""
    target.click()
    target.fill("")
    target.press_sequentially(value, delay=60)
    try:
        page.wait_for_selector(".dropdown-location:visible, [role=option]:visible", timeout=5000)
        options = page.locator(".dropdown-location:visible, [role=option]:visible")
        texts = [t.strip() for t in options.all_inner_texts()]
        best = _best(texts, value)
        if best is not None:
            options.nth(best).click()
            return texts[best]
    except PlaywrightError:
        pass
    return value


def fill_form(page: Page, answers: list[Answer]) -> list[str]:
    """Greenhouse and Lever: every field is found by the selector the reader saved."""
    problems = []
    for a in sorted(answers, key=lambda a: a.kind != "file"):  # files first (resume autofill)
        if a.status != "ok" or a.value in (None, "", []):
            continue
        if not a.selector:
            problems.append(f"{a.label}: the reader did not find where this field is")
            continue
        target = page.locator(a.selector).first
        try:
            if a.kind == "file":
                target.set_input_files(str(a.value))
                _wait_for_upload(page)
            elif a.kind in ("text", "email", "url", "tel", "number", "textarea"):
                target.fill(str(a.value))
            elif a.kind == "location":
                a.value = _choose_place(page, target, str(a.value))
            elif a.kind == "select":
                native = target.evaluate("e => e.tagName") == "SELECT"
                chosen = _choose_native(target, str(a.value)) if native else _pick_option(page, target, str(a.value))
                if chosen is None:
                    a.status, a.reason = ("needs_me" if a.required else "skip"), f"no option matches '{a.value}'"
                    problems.append(f"{a.label}: no option matches '{a.value}'")
                else:
                    a.value = chosen
            elif a.kind in ("radio", "checkboxes"):
                for option in (a.value if isinstance(a.value, list) else [a.value]):
                    if not _tick(page, a.selector, option):
                        problems.append(f"{a.label}: could not find the option '{option}'")
            else:
                problems.append(f"{a.label}: unsupported field type {a.kind}")
        except PlaywrightError as exc:
            problems.append(f"{a.label}: could not fill ({str(exc).splitlines()[0][:150]})")
    return problems


FORM_READBACK = r"""
(items) => {
  const clean = (t) => (t || '').replace(/\s+/g, ' ').trim();
  const out = {};
  for (const [key, selector] of items) {
    const els = [...document.querySelectorAll(selector)];
    if (!els.length) continue;
    const values = [], checked = [];
    for (const e of els) {
      if (e.type === 'file') values.push(...[...(e.files || [])].map(f => f.name));
      else if (e.type === 'checkbox' || e.type === 'radio') {
        if (e.checked) checked.push(clean((e.closest('label') || {}).innerText || (e.labels && e.labels[0] ? e.labels[0].innerText : '') || e.value));
      } else if (e.tagName === 'SELECT') values.push(clean(e.selectedOptions[0] ? e.selectedOptions[0].text : ''));
      else values.push(e.value);
    }
    const box = els[0].closest('.select__container, .application-question, .field-wrapper, .input-wrapper, .file-upload, fieldset') || els[0].parentElement;
        out[key] = {values, pressed: [], checked, text: clean(box ? box.innerText : '').slice(0, 400), buttonClasses: []};
  }
  out['__form_text__'] = clean((document.querySelector('#application-form') || document.body).innerText);
  return out;
}
"""


def verify_form(page: Page, answers: list[Answer]) -> list[str]:
    items = [[a.key, a.selector] for a in answers if a.selector]
    return _compare(page.evaluate(FORM_READBACK, items), answers)


def screenshot(page: Page, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(path), full_page=True)
    return path


def blocked_submit_events(page: Page) -> int:
    try:
        return int(page.evaluate("() => window.__applierBlockedSubmit || 0"))
    except PlaywrightError:
        return 0
