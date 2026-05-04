"""claude-reply: tap a notification on your phone, get a one-shot web form,
type a reply, and it's injected into your tmux pane via send-keys.

Bind to 0.0.0.0:8765 on the host running Claude. Mint endpoint requires
X-Mint-Secret. Reply endpoint is gated only by the unguessable token in
the URL — assumes you're on a trusted network (LAN or VPN).
"""
from __future__ import annotations

import base64
import json
import os
import secrets
import sqlite3
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response
from fastapi.templating import Jinja2Templates

CONFIG_DIR = Path(os.environ.get("CLAUDE_REPLY_CONFIG", "~/.config/claude-reply")).expanduser()
CONFIG_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = CONFIG_DIR / "tokens.sqlite"
SECRET_PATH = CONFIG_DIR / "mint-secret"
TOKEN_TTL_SECONDS = 60 * 60  # 1 hour

# Pane safety: only inject into panes running these commands. Override via env
# (comma-separated). Default keeps it tight to Claude Code's interpreters.
ALLOWED_PANE_COMMANDS = set(
    os.environ.get("CLAUDE_REPLY_ALLOWED_COMMANDS", "claude,node,python,python3").split(",")
)

# Optional HTTP Basic auth on /r/{token} routes. Format: "user:password".
# When set, the URL token alone is no longer sufficient — browsers also need
# the basic-auth credentials. /mint and /burn always use X-Mint-Secret.
BASIC_AUTH = os.environ.get("CLAUDE_REPLY_BASIC_AUTH", "").strip()
BASIC_AUTH_REALM = os.environ.get("CLAUDE_REPLY_BASIC_AUTH_REALM", "claude-reply")

# Mint secret bootstrap. Generated once, persisted, never logged.
if not SECRET_PATH.exists():
    SECRET_PATH.write_text(secrets.token_urlsafe(32))
    SECRET_PATH.chmod(0o600)
MINT_SECRET = SECRET_PATH.read_text().strip()

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
app = FastAPI(title="claude-reply", docs_url=None, redoc_url=None)


@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with db() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS tokens (
                token       TEXT PRIMARY KEY,
                target      TEXT NOT NULL,
                created_at  INTEGER NOT NULL,
                used_at     INTEGER
            )
        """)
        # Best-effort additive migrations. SQLite raises on duplicate column.
        for col, decl in [
            ("title", "TEXT"),
            ("context", "TEXT"),
            ("kind", "TEXT"),          # "prompt" (default) or "picker"
            ("options_json", "TEXT"),  # JSON array of {label, description} for picker
        ]:
            try:
                c.execute(f"ALTER TABLE tokens ADD COLUMN {col} {decl}")
            except sqlite3.OperationalError:
                pass


def sweep_expired() -> int:
    """Delete tokens older than TTL. Called lazily on each request."""
    cutoff = int(time.time()) - TOKEN_TTL_SECONDS
    with db() as c:
        cur = c.execute("DELETE FROM tokens WHERE created_at < ?", (cutoff,))
        return cur.rowcount


def pane_command(target: str) -> str | None:
    """Returns the running command in the pane, or None if pane is gone."""
    try:
        out = subprocess.run(
            ["tmux", "display-message", "-t", target, "-p", "#{pane_current_command}"],
            check=True, capture_output=True, text=True, timeout=2,
        )
        return out.stdout.strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return None


def _check_pane(target: str) -> tuple[bool, str]:
    cmd = pane_command(target)
    if cmd is None:
        return False, f"pane {target} no longer exists"
    if cmd not in ALLOWED_PANE_COMMANDS:
        return False, f"pane {target} is running '{cmd}', not in allowlist"
    return True, cmd


def send_to_pane(target: str, message: str) -> tuple[bool, str]:
    """send-keys the message + Enter into the target pane. Returns (ok, detail)."""
    ok, detail = _check_pane(target)
    if not ok:
        return False, detail
    try:
        subprocess.run(
            ["tmux", "send-keys", "-t", target, "-l", message],
            check=True, capture_output=True, timeout=2,
        )
        # -l sends literally (no key-name interpretation). Now press Enter.
        subprocess.run(
            ["tmux", "send-keys", "-t", target, "Enter"],
            check=True, capture_output=True, timeout=2,
        )
        return True, "sent"
    except subprocess.CalledProcessError as e:
        return False, f"tmux send-keys failed: {e.stderr.decode().strip()}"


def send_picker_selection(target: str, index: int) -> tuple[bool, str]:
    """For an active AskUserQuestion picker: arrow Down N times then Enter to
    select option N (0-indexed). Pre-flight verifies the pane still hosts
    Claude — we can't externally tell whether the picker itself is up, so a
    misfire here would inject literal `[B[B` escape sequences into the next
    prompt input. Acceptable v1 risk; mitigated by short token TTLs and the
    burn-on-prompt hook.
    """
    ok, detail = _check_pane(target)
    if not ok:
        return False, detail
    if index < 0 or index > 32:
        return False, f"option index {index} out of range"
    try:
        keys = ["Down"] * index + ["Enter"]
        subprocess.run(
            ["tmux", "send-keys", "-t", target] + keys,
            check=True, capture_output=True, timeout=2,
        )
        return True, f"selected option {index}"
    except subprocess.CalledProcessError as e:
        return False, f"tmux send-keys failed: {e.stderr.decode().strip()}"


def send_escape_then_text(target: str, message: str) -> tuple[bool, str]:
    """For an active picker when the user wants free text: Escape cancels
    the picker (returns the AskUserQuestion as 'rejected' to Claude), then
    text + Enter submits as a fresh prompt. Claude sees both the rejection
    and the new message and responds to the new message.
    """
    ok, detail = _check_pane(target)
    if not ok:
        return False, detail
    try:
        subprocess.run(
            ["tmux", "send-keys", "-t", target, "Escape"],
            check=True, capture_output=True, timeout=2,
        )
        time.sleep(0.2)  # let the picker actually dismiss before typing
        subprocess.run(
            ["tmux", "send-keys", "-t", target, "-l", message],
            check=True, capture_output=True, timeout=2,
        )
        subprocess.run(
            ["tmux", "send-keys", "-t", target, "Enter"],
            check=True, capture_output=True, timeout=2,
        )
        return True, "canceled picker, submitted text"
    except subprocess.CalledProcessError as e:
        return False, f"tmux send-keys failed: {e.stderr.decode().strip()}"


def _basic_auth_check(request: Request) -> Response | None:
    """Returns a 401 response if Basic auth is configured and the request
    fails it. Returns None when the request is allowed through (either
    because auth isn't configured or the credentials match).
    Constant-time compare to keep timing leaks off the table.
    """
    if not BASIC_AUTH:
        return None
    header = request.headers.get("Authorization", "")
    if header.startswith("Basic "):
        try:
            decoded = base64.b64decode(header[6:]).decode("utf-8", errors="replace")
            if secrets.compare_digest(decoded, BASIC_AUTH):
                return None
        except (ValueError, UnicodeDecodeError):
            pass
    return Response(
        content="Authentication required.",
        status_code=401,
        headers={"WWW-Authenticate": f'Basic realm="{BASIC_AUTH_REALM}"'},
    )


@app.on_event("startup")
def _startup() -> None:
    init_db()
    sweep_expired()


@app.get("/healthz", response_class=PlainTextResponse)
def healthz() -> str:
    return "ok"


@app.post("/burn", response_class=PlainTextResponse)
async def burn(request: Request, target: str = Form(...)) -> str:
    """Mark any outstanding tokens for a target as used. Called from the
    UserPromptSubmit hook so local prompts invalidate pending notifications.

    Auth: same X-Mint-Secret as /mint. Always-200 to keep the hook silent.
    """
    if request.headers.get("X-Mint-Secret") != MINT_SECRET:
        # Silent 200 — never want to break the user's prompt loop on auth fail
        return "0"
    if not target:
        return "0"
    with db() as c:
        cur = c.execute(
            "UPDATE tokens SET used_at = ? WHERE target = ? AND used_at IS NULL",
            (int(time.time()), target),
        )
        return str(cur.rowcount)


@app.post("/mint", response_class=PlainTextResponse)
async def mint(
    request: Request,
    target: str = Form(...),
    title: str = Form(""),
    context: str = Form(""),
    kind: str = Form("prompt"),
    options_json: str = Form(""),
) -> str:
    if request.headers.get("X-Mint-Secret") != MINT_SECRET:
        raise HTTPException(status_code=401, detail="bad secret")
    if not target or len(target) > 64:
        raise HTTPException(status_code=400, detail="bad target")
    # Cap stored context — these end up rendered in HTML and we don't want
    # to inflate SQLite for some pathological notification body.
    title = title[:200]
    context = context[:2000]
    if kind not in ("prompt", "picker"):
        kind = "prompt"
    # Validate options_json — store only if it parses to a list. Anything
    # else degrades to plain prompt mode.
    if kind == "picker":
        try:
            parsed = json.loads(options_json or "[]")
            if not isinstance(parsed, list) or not parsed:
                kind, options_json = "prompt", ""
        except json.JSONDecodeError:
            kind, options_json = "prompt", ""
    token = secrets.token_urlsafe(16)
    with db() as c:
        c.execute(
            """INSERT INTO tokens
               (token, target, created_at, title, context, kind, options_json)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (token, target, int(time.time()),
             title or None, context or None,
             kind, options_json or None),
        )
    return token


def _load_token(token: str) -> sqlite3.Row | None:
    sweep_expired()
    with db() as c:
        row = c.execute("SELECT * FROM tokens WHERE token = ?", (token,)).fetchone()
    return row


@app.get("/r/{token}", response_class=HTMLResponse)
def reply_form(request: Request, token: str):
    auth_fail = _basic_auth_check(request)
    if auth_fail:
        return auth_fail
    row = _load_token(token)
    if row is None:
        return templates.TemplateResponse(request, "expired.html", {}, status_code=410)
    if row["used_at"] is not None:
        return templates.TemplateResponse(request, "used.html", {"target": row["target"]}, status_code=410)
    pane = pane_command(row["target"]) or "(pane gone)"
    options = []
    kind = row["kind"] or "prompt"
    if kind == "picker" and row["options_json"]:
        try:
            options = json.loads(row["options_json"])
        except json.JSONDecodeError:
            options = []
            kind = "prompt"
    return templates.TemplateResponse(
        request, "reply.html",
        {
            "token": token, "target": row["target"], "pane": pane,
            "title": row["title"], "context": row["context"],
            "kind": kind, "options": options,
        },
    )


@app.post("/r/{token}", response_class=HTMLResponse)
def reply_submit(
    request: Request,
    token: str,
    message: str = Form(""),
    choice: str = Form(""),
    other_text: str = Form(""),
):
    auth_fail = _basic_auth_check(request)
    if auth_fail:
        return auth_fail
    row = _load_token(token)
    if row is None:
        return templates.TemplateResponse(request, "expired.html", {}, status_code=410)
    if row["used_at"] is not None:
        return templates.TemplateResponse(request, "used.html", {"target": row["target"]}, status_code=410)

    kind = row["kind"] or "prompt"
    summary = ""

    if kind == "picker":
        if choice == "other":
            text = other_text.strip()
            if not text:
                return templates.TemplateResponse(
                    request, "error.html",
                    {"detail": "Selected Other but no text provided.", "target": row["target"]},
                    status_code=400,
                )
            ok, detail = send_escape_then_text(row["target"], text)
            summary = f"(canceled picker) {text}"
        elif choice.isdigit():
            idx = int(choice)
            ok, detail = send_picker_selection(row["target"], idx)
            try:
                opts = json.loads(row["options_json"] or "[]")
                summary = f"selected: {opts[idx]['label']}"
            except (json.JSONDecodeError, IndexError, KeyError):
                summary = f"selected option #{idx}"
        else:
            return templates.TemplateResponse(
                request, "error.html",
                {"detail": "No selection made.", "target": row["target"]},
                status_code=400,
            )
    else:
        # Plain prompt-mode reply (idle notification, generic message, etc.)
        if not message.strip():
            return templates.TemplateResponse(
                request, "error.html",
                {"detail": "Empty message.", "target": row["target"]},
                status_code=400,
            )
        ok, detail = send_to_pane(row["target"], message)
        summary = message

    if not ok:
        return templates.TemplateResponse(
            request, "error.html", {"detail": detail, "target": row["target"]}, status_code=409
        )

    with db() as c:
        c.execute("UPDATE tokens SET used_at = ? WHERE token = ?", (int(time.time()), token))

    return templates.TemplateResponse(
        request, "success.html", {"target": row["target"], "message": summary}
    )
