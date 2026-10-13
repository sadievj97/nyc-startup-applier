"""Read-only Gmail access (brief 8): only the gmail.readonly scope, never send or modify.

credentials.json (your Google "Desktop app" client) and token.json (your sign-in) live in the project folder
and are gitignored; their contents are never printed. Google ends sign-ins after 7 days while the app is in
Testing status; that shows up as invalid_grant, which becomes a GmailReauth error and a flag.
"""

from __future__ import annotations

import base64
import email
import email.policy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import getaddresses, parsedate_to_datetime
from pathlib import Path
from typing import Any, Iterator

from applier.jobs.text import html_to_text
from applier.paths import Paths

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
BODY_LIMIT = 6000


class GmailSetupNeeded(Exception):
    """credentials.json is missing or there is no sign-in yet. The message says what to do."""


class GmailReauth(Exception):
    """Google ended the sign-in (invalid_grant): you need to sign in again."""


@dataclass
class Message:
    gmail_id: str
    thread_id: str
    from_address: str
    from_name: str
    subject: str
    received_at: datetime
    body: str
    to: list[str] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)

    @property
    def domain(self) -> str:
        return self.from_address.rsplit("@", 1)[-1].lower() if "@" in self.from_address else ""


def credentials_file(paths: Paths) -> Path:
    return paths.root / "credentials.json"


def token_file(paths: Paths) -> Path:
    return paths.root / "token.json"


def _chrome() -> str | None:
    """Sign in with Google Chrome when it is installed (your choice); otherwise the default browser."""
    import os
    import webbrowser

    for folder in (os.environ.get("PROGRAMFILES"), os.environ.get("PROGRAMFILES(X86)"), os.environ.get("LOCALAPPDATA")):
        exe = Path(folder or "") / "Google" / "Chrome" / "Application" / "chrome.exe"
        if folder and exe.exists():
            webbrowser.register("chrome", None, webbrowser.BackgroundBrowser(str(exe)))
            return "chrome"
    return None


def authorize(paths: Paths, expected_email: str, account: str | None = None) -> str:
    """Open your browser to sign in and allow read-only access. `account` is the Google account to sign in
    with when your email lives inside another account (profile.yaml contact.inbox_account). The sign-in is
    kept only if it is that account and the account really owns `expected_email`. Returns the account."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    account = account or expected_email
    if not credentials_file(paths).exists():
        raise GmailSetupNeeded("credentials.json is not in the project folder yet")
    flow = InstalledAppFlow.from_client_secrets_file(str(credentials_file(paths)), SCOPES)
    creds = flow.run_local_server(port=0, open_browser=True, browser=_chrome(), login_hint=account, prompt="consent",
                                  authorization_prompt_message="If no browser opened, visit: {url}",
                                  success_message="Signed in. You can close this tab and go back to Claude Code.")
    if set(creds.scopes or []) != set(SCOPES):
        raise GmailSetupNeeded("Google granted a different permission than read-only; nothing was saved")
    token_file(paths).write_text(creds.to_json(), encoding="utf-8")
    svc = service(paths)
    address = svc.users().getProfile(userId="me").execute().get("emailAddress", "")
    owned = {address.lower()} | {s.get("sendAsEmail", "").lower() for s in
                                 svc.users().settings().sendAs().list(userId="me").execute().get("sendAs", [])}
    if address.lower() != account.lower() or expected_email.lower() not in owned:
        token_file(paths).unlink()
        raise GmailSetupNeeded(f"you signed in as {address}, which is not {account} with {expected_email} as one of "
                               "its addresses; nothing was saved")
    return address


def service(paths: Paths) -> Any:
    """A read-only Gmail client. Refreshes the sign-in; raises GmailReauth when Google has ended it."""
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    if not token_file(paths).exists():
        raise GmailSetupNeeded("no Gmail sign-in yet: run 'gmail-auth'")
    creds = Credentials.from_authorized_user_file(str(token_file(paths)), SCOPES)
    if not creds.valid:
        if not creds.refresh_token:
            raise GmailReauth("the Gmail sign-in has no refresh token")
        try:
            creds.refresh(Request())
        except RefreshError as exc:
            raise GmailReauth(f"Google ended the Gmail sign-in ({str(exc)[:80]})") from exc
        token_file(paths).write_text(creds.to_json(), encoding="utf-8")
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _execute(request: Any, tries: int = 7) -> dict:
    """Run a Gmail request at a polite pace; when Gmail says to slow down, wait and try again."""
    import time

    from googleapiclient.errors import HttpError

    for attempt in range(tries):
        try:
            time.sleep(0.15)
            return request.execute()
        except HttpError as exc:
            status = getattr(exc.resp, "status", 0)
            slow_down = status == 429 or status >= 500 or (status == 403 and "ate" in str(exc) and "imit" in str(exc))
            if not slow_down or attempt == tries - 1:
                raise
            time.sleep(min(60, 2 ** attempt * 2))
    raise RuntimeError("unreachable")


def list_ids(svc: Any, query: str, limit: int = 500) -> Iterator[str]:
    request = svc.users().messages().list(userId="me", q=query, maxResults=min(limit, 500))
    count = 0
    while request is not None and count < limit:
        response = _execute(request)
        for item in response.get("messages", []):
            yield item["id"]
            count += 1
            if count >= limit:
                return
        request = svc.users().messages().list_next(request, response)


def _decode(data: str) -> str:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", errors="replace")


def _body(payload: dict) -> str:
    """The plain-text body, or the HTML body turned into text."""
    plain, html = [], []

    def walk(part: dict) -> None:
        mime = part.get("mimeType", "")
        data = (part.get("body") or {}).get("data")
        if data and mime == "text/plain":
            plain.append(_decode(data))
        elif data and mime == "text/html":
            html.append(_decode(data))
        for child in part.get("parts") or []:
            walk(child)

    walk(payload)
    text = "\n".join(plain) if plain else html_to_text("\n".join(html))
    return text[:BODY_LIMIT]


def parse_gmail(raw: dict) -> Message:
    headers = {h["name"].lower(): h["value"] for h in raw.get("payload", {}).get("headers", [])}
    name, address = (getaddresses([headers.get("from", "")]) or [("", "")])[0]
    received = datetime.fromtimestamp(int(raw.get("internalDate", "0")) / 1000, tz=timezone.utc)
    return Message(raw["id"], raw.get("threadId", ""), address.lower(), name, headers.get("subject", ""),
                   received, _body(raw.get("payload", {})),
                   [a for _, a in getaddresses([headers.get("to", "")]) if a], raw.get("labelIds", []))


def fetch(svc: Any, gmail_id: str) -> Message:
    return parse_gmail(_execute(svc.users().messages().get(userId="me", id=gmail_id, format="full")))


def parse_eml(path: Path, gmail_id: str | None = None) -> Message:
    """A saved .eml file as a Message (used by the tests' practice emails)."""
    msg = email.message_from_bytes(path.read_bytes(), policy=email.policy.default)
    name, address = (getaddresses([msg.get("From", "")]) or [("", "")])[0]
    part = msg.get_body(preferencelist=("plain", "html"))
    body = part.get_content() if part else ""
    if part is not None and part.get_content_type() == "text/html":
        body = html_to_text(body)
    when = parsedate_to_datetime(msg["Date"]) if msg["Date"] else datetime.now(timezone.utc)
    return Message(gmail_id or path.stem, gmail_id or path.stem, address.lower(), name, msg.get("Subject", ""),
                   when.astimezone(timezone.utc), body[:BODY_LIMIT])
