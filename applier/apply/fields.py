"""What a form asks: one FormField per question, read from the live page (brief 6, step 1).

Readers are per hiring system because each builds its forms differently. Page text is untrusted (rule 7):
it is only parsed here, never treated as instructions.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

# Kinds of field the fillers know how to answer.
KINDS = ("text", "email", "tel", "url", "number", "date", "textarea", "file", "boolean", "radio",
         "checkboxes", "select", "location", "unknown")


@dataclass
class FormField:
    key: str  # stable id on the page (name, data-field-path or question id)
    label: str
    kind: str
    required: bool = False
    options: list[str] = field(default_factory=list)
    description: str = ""
    max_length: int | None = None
    accepts: str = ""  # for files: accepted types
    section: str = ""  # e.g. "eeo" for voluntary self-identification
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------- Ashby

ASHBY_EXTRACT = r"""
() => {
  const clean = (t) => (t || '').replace(/\s+/g, ' ').trim();
  const out = [];
  for (const entry of document.querySelectorAll('[data-field-path]')) {
    const path = entry.getAttribute('data-field-path');
    if (entry.parentElement && entry.parentElement.closest('[data-field-path]')) continue;  // nested
    const labelEl = entry.querySelector('label, legend, [class*="_heading_"]');
    const label = clean(labelEl ? labelEl.innerText : '');
    const required = !!(labelEl && /required/.test(labelEl.className || '')) ||
                     !!entry.querySelector('[required], [aria-required="true"]');
    const desc = clean([...entry.querySelectorAll('[class*="description"], [class*="_helper"], p')]
                       .map(e => e.innerText).join(' ')).slice(0, 400);
    const buttons = [...entry.querySelectorAll('button')].map(b => clean(b.innerText)).filter(Boolean);
    const radios = [...entry.querySelectorAll('input[type=radio]')];
    const boxes = [...entry.querySelectorAll('input[type=checkbox]')];
    const text = entry.querySelector('input[type=text], input[type=email], input[type=url], input[type=tel], input[type=number], input[type=date], input:not([type])');
    const area = entry.querySelector('textarea');
    const file = entry.querySelector('input[type=file]');
    const combo = entry.querySelector('[role=combobox], select');
    const labelOf = (input) => {
      const l = input.labels && input.labels[0] ? input.labels[0].innerText : (input.closest('label') || {}).innerText;
      return clean(l || input.value || '');
    };
    let kind = 'unknown', options = [], maxLength = null, accepts = '';
    if (file) { kind = 'file'; accepts = file.getAttribute('accept') || ''; }
    else if (boxes.length === 1 && buttons.includes('Yes') && buttons.includes('No')) { kind = 'boolean'; options = ['Yes', 'No']; }
    else if (radios.length) { kind = 'radio'; options = radios.map(labelOf); }
    else if (boxes.length > 1) { kind = 'checkboxes'; options = boxes.map(labelOf); }
    else if (boxes.length === 1) { kind = 'checkboxes'; options = [labelOf(boxes[0]) || label]; }
    else if (area) { kind = 'textarea'; maxLength = area.maxLength > 0 ? area.maxLength : null; }
    else if (combo) {
      kind = /location/i.test(label) ? 'location' : 'select';
      if (combo.tagName === 'SELECT') options = [...combo.options].map(o => clean(o.text)).filter(Boolean);
    }
    else if (text) {
      kind = (text.type || 'text');
      if (/location|city/i.test(label) && text.getAttribute('role') === 'combobox') kind = 'location';
      maxLength = text.maxLength > 0 ? text.maxLength : null;
    }
    out.push({key: path, label, kind, required, options, description: desc === label ? '' : desc,
              max_length: maxLength, accepts, section: /voluntary|self-identif|eeo|demographic/i.test(
                clean((entry.closest('[class*="_section_"]') || {}).innerText || '').slice(0, 200)) ? 'eeo' : ''});
  }
  return out;
}
"""


def ashby_fields(page) -> list[FormField]:
    page.wait_for_selector("[data-field-path]", timeout=30000)
    page.wait_for_timeout(800)
    return [FormField(**item) for item in page.evaluate(ASHBY_EXTRACT)]


# ---------------------------------------------------------------- page checks (rules 3 and 6)

PAGE_CHECKS = r"""
() => {
  const text = (document.body ? document.body.innerText : '').slice(0, 20000);
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    for (let e = el; e; e = e.parentElement) {
      const s = getComputedStyle(e);
      if (s.visibility === 'hidden' || s.display === 'none' || parseFloat(s.opacity) === 0) return false;
    }
    return r.width > 30 && r.height > 30 && r.bottom > 0 && r.right > 0;
  };
  const frames = [...document.querySelectorAll('iframe')].filter(visible).map(f => f.src || '');
  // A challenge is a visible puzzle frame or a visible "I'm not a robot" box. The small badge of an
  // invisible check (size=invisible) and hCaptcha's background frames are not challenges.
  const challenge = (src) => /recaptcha\/(api2|enterprise)\/bframe|hcaptcha\.com.*frame=(challenge|checkbox)|challenges\.cloudflare\.com|turnstile/i.test(src)
    || (/recaptcha\/(api2|enterprise)\/anchor/i.test(src) && !/size=invisible/i.test(src));
  return {
    text,
    challengeFrames: frames.filter(challenge),
    passwordFields: [...document.querySelectorAll('input[type=password]')].filter(visible).length,
    title: document.title,
  };
}
"""

_BOT_TEXT = [
    r"verify (that )?you are (a )?human", r"are you a robot", r"i'?m not a robot", r"checking your browser",
    r"just a moment\.\.\.", r"access denied", r"unusual traffic", r"complete the (security )?check",
    r"enter the (verification )?code (we|that we) (sent|emailed|texted)", r"we sent (you )?a (verification )?code",
    r"verify your (email|phone)", r"sign in to (apply|continue)", r"log in to (apply|continue)", r"create an account to apply",
]


def page_problems(page) -> list[str]:
    """Bot checks, verification steps and login walls (rule 3): any of these stops the application."""
    import re

    info = page.evaluate(PAGE_CHECKS)
    problems = []
    if info["challengeFrames"]:
        problems.append("a CAPTCHA or human-verification challenge is showing")
    if info["passwordFields"]:
        problems.append("the page asks for a password (login wall)")
    lowered = info["text"].lower()
    for pattern in _BOT_TEXT:
        if re.search(pattern, lowered):
            problems.append(f"the page says: '{re.search(pattern, lowered).group(0)}'")
            break
    return problems


def page_text(page) -> str:
    return page.evaluate("() => document.body ? document.body.innerText : ''")


# ---------------------------------------------------------------- Greenhouse (job-boards.greenhouse.io)

GREENHOUSE_EXTRACT = r"""
() => {
  const clean = (t) => (t || '').replace(/\s+/g, ' ').trim();
  const form = document.querySelector('#application-form') || document;
  const out = [], seen = new Set();
  const sectionOf = (el) => {
    const box = el.closest('[class*=eeo], [class*=demographic], [id*=eeo], [id*=demographic], section, fieldset');
    return box && /voluntary|self-identif|equal employment|eeo|demographic/i.test(clean(box.innerText).slice(0, 300)) ? 'eeo' : '';
  };
  for (const el of form.querySelectorAll('input, textarea, select')) {
    const id = el.id;
    if (!id || seen.has(id) || ['hidden', 'submit', 'button', 'search', 'checkbox', 'radio'].includes(el.type)) continue;
    if (/g-recaptcha|^iti/.test(id)) continue;
    const labelEl = form.querySelector(`label[for="${CSS.escape(id)}"]`);
    let label = clean(labelEl ? labelEl.innerText : '');
    let kind = el.type === 'tel' ? 'tel' : el.type === 'email' ? 'email' : 'text';
    if (el.type === 'file') {
      kind = 'file';
      const group = el.closest('.file-upload, [class*=upload], .field-wrapper') || el.parentElement;
      const named = group && group.querySelector('[id^=upload-label], .label:not(.visually-hidden)');
      label = clean(named ? named.innerText : '') || id;
    } else if (el.getAttribute('role') === 'combobox' || el.tagName === 'SELECT') kind = 'select';
    else if (el.tagName === 'TEXTAREA') {
      kind = 'textarea';
      if (/_text$/.test(id) && !el.offsetParent) continue;  // the hidden "enter manually" alternative to a file
    }
    const required = !!(el.required || el.getAttribute('aria-required') === 'true' || /\*\s*$/.test(label));
    seen.add(id);
    out.push({key: id, label: label.replace(/\s*\*\s*$/, ''), kind, required,
              options: el.tagName === 'SELECT' ? [...el.options].map(o => clean(o.text)).filter(t => t && !/^(select|please select)/i.test(t)) : [],
              description: '', max_length: el.maxLength > 0 ? el.maxLength : null,
              accepts: el.getAttribute('accept') || '', section: sectionOf(el),
              extra: {selector: `[id="${id}"]`, native: el.tagName === 'SELECT'}});
  }
  for (const set of form.querySelectorAll('fieldset')) {  // groups of checkboxes or radio buttons
    const inputs = [...set.querySelectorAll('input[type=checkbox], input[type=radio]')];
    if (!inputs.length) continue;
    const legend = clean((set.querySelector('legend') || {}).innerText || '');
    const key = inputs[0].name || set.id || legend;
    if (!key || seen.has(key)) continue;
    seen.add(key);
    const labelOf = (i) => clean((i.labels && i.labels[0] ? i.labels[0].innerText : (i.closest('label') || {}).innerText) || i.value);
    out.push({key, label: legend.replace(/\s*\*\s*$/, ''), kind: inputs[0].type === 'radio' ? 'radio' : 'checkboxes',
              required: /\*\s*$/.test(legend) || inputs.some(i => i.required), options: inputs.map(labelOf),
              description: '', max_length: null, accepts: '', section: sectionOf(set),
              extra: {selector: inputs[0].name ? `[name="${inputs[0].name}"]` : `fieldset#${CSS.escape(set.id)} input`}});
  }
  return out;
}
"""


def greenhouse_fields(page, api_questions: list[dict] | None = None) -> tuple[list[FormField], list[str]]:
    """Read the form, take options from Greenhouse's question list (or by opening each dropdown), and
    cross-check the page against that list (brief 6, step 1). Returns the fields and any mismatch."""
    page.wait_for_selector("#application-form", timeout=30000)
    page.wait_for_timeout(1000)
    fields = [FormField(**item) for item in page.evaluate(GREENHOUSE_EXTRACT)]
    by_key = {f.key: f for f in fields}
    problems = []
    for question in api_questions or []:
        for spec in question.get("fields", []):
            name = spec.get("name", "")
            if name.endswith("_text") and name[:-5] in by_key:  # "enter manually" alternative to a file
                continue
            f = by_key.get(name)
            if f is None:
                if question.get("required"):
                    problems.append(f"the page is missing '{question.get('label')}' from Greenhouse's question list")
                continue
            if question.get("label") and (not f.label or f.kind == "file"):
                f.label = question["label"].strip()
            f.required = f.required or bool(question.get("required"))
            if spec.get("values") and not f.options:
                f.options = [str(v.get("label")) for v in spec["values"]]
            if spec.get("type") == "multi_value_multi_select":
                f.extra["multi"] = True
    for f in fields:
        if f.kind == "select" and not f.options and not f.extra.get("native"):
            f.options = read_dropdown_options(page, f.extra["selector"])
    return fields, problems


def read_dropdown_options(page, selector: str) -> list[str]:
    """Open a dropdown, read its options, and close it again without choosing anything."""
    try:
        page.locator(selector).first.click()
        page.wait_for_selector("[role=option]:visible", timeout=4000)  # not the hidden phone-country list
        options = [t.strip() for t in page.locator("[role=option]:visible").all_inner_texts() if t.strip()]
    except Exception:
        options = []
    page.keyboard.press("Escape")
    page.wait_for_timeout(150)
    return options


# ---------------------------------------------------------------- Lever (jobs.lever.co)

LEVER_EXTRACT = r"""
() => {
  const clean = (t) => (t || '').replace(/\s+/g, ' ').trim();
  const out = [];
  document.querySelectorAll('#application-form .application-question').forEach((q, n) => {
    if (!q.offsetParent) return;  // hidden until another answer reveals it
    const labelEl = q.querySelector('.application-label') || q.querySelector('label');
    let label = clean(labelEl ? labelEl.innerText : '');
    const required = /✱/.test(label) || !!q.querySelector('[required], .required');
    label = label.replace(/✱/g, '').trim();
    const file = q.querySelector('input[type=file]');
    const radios = [...q.querySelectorAll('input[type=radio]')];
    const boxes = [...q.querySelectorAll('input[type=checkbox]')];
    const area = q.querySelector('textarea');
    const select = q.querySelector('select');
    const text = q.querySelector('input[type=text], input[type=email], input[type=tel], input[type=url], input:not([type])');
    const main = file || radios[0] || boxes[0] || area || select || text;
    if (!main) return;
    const name = main.getAttribute('name');
    const optionText = (i) => clean((i.closest('label') || {}).innerText || i.value);
    let kind = 'text', options = [];
    if (file) kind = 'file';
    else if (radios.length) { kind = 'radio'; options = radios.map(optionText); }
    else if (boxes.length) { kind = 'checkboxes'; options = boxes.map(optionText); }
    else if (area) kind = 'textarea';
    else if (select) { kind = 'select'; options = [...select.options].map(o => clean(o.text)).filter(t => t && !/^select/i.test(t)); }
    else if (text) { kind = text.classList.contains('location-input') ? 'location' : (text.type === 'email' ? 'email' : 'text'); }
    if (select) label = clean(label.replace(clean(select.innerText), ''));
    if (file && !q.querySelector('.application-label')) label = label.replace(/attach.*$/i, '').trim();
    const url = /^urls\[(.+)\]$/.exec(name || '');
    if (url) label = `${url[1]} URL`;
    const section = (/^eeo\[/.test(name || '') || (select && select.classList.contains('candidate-location')) ||
      /voluntary|self-identif|equal employment|demographic/i.test(clean((q.closest('.section') || {}).innerText || '').slice(0, 200))) ? 'eeo' : '';
    const selector = name ? `[name="${name}"]` : (select && select.className ? `select.${select.className.split(' ')[0]}` : '');
    if (!selector) return;
    out.push({key: name || selector, label: label.replace(/\s*\*\s*$/, ''), kind, required, options,
              description: '', max_length: (area || text) && (area || text).maxLength > 0 ? (area || text).maxLength : null,
              accepts: file ? (file.getAttribute('accept') || '') : '', section, extra: {selector}});
  });
  return out;
}
"""


def lever_fields(page) -> list[FormField]:
    page.wait_for_selector("#application-form .application-question", timeout=30000)
    page.wait_for_timeout(800)
    return [FormField(**item) for item in page.evaluate(LEVER_EXTRACT)]
