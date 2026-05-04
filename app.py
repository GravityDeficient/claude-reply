"""claude-reply: tap a notification on your phone, get a one-shot web form,
type a reply, and it's injected into your tmux pane via send-keys.

Bind to 0.0.0.0:8765 on the host running Claude. Mint endpoint requires
X-Mint-Secret. Reply endpoint is gated only by the unguessable token in
the URL — assumes you're on a trusted network (LAN or VPN).
"""
from __future__ import annotations

import os
import secrets
import sqlite3
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
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
        # Best-effort migration for the context column. SQLite will raise
        # on duplicate column, hence the try.
        try:
            c.execute("ALTER TABLE tokens ADD COLUMN title TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            c.execute("ALTER TABLE tokens ADD COLUMN context TEXT")
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


def send_to_pane(target: str, message: str) -> tuple[bool, str]:
    """send-keys the message + Enter into the target pane. Returns (ok, detail)."""
    cmd = pane_command(target)
    if cmd is None:
        return False, f"pane {target} no longer exists"
    if cmd not in ALLOWED_PANE_COMMANDS:
        return False, f"pane {target} is running '{cmd}', not in allowlist"
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
) -> str:
    if request.headers.get("X-Mint-Secret") != MINT_SECRET:
        raise HTTPException(status_code=401, detail="bad secret")
    if not target or len(target) > 64:
        raise HTTPException(status_code=400, detail="bad target")
    # Cap stored context — these end up rendered in HTML and we don't want
    # to inflate SQLite for some pathological notification body.
    title = title[:200]
    context = context[:2000]
    token = secrets.token_urlsafe(16)
    with db() as c:
        c.execute(
            "INSERT INTO tokens (token, target, created_at, title, context) VALUES (?, ?, ?, ?, ?)",
            (token, target, int(time.time()), title or None, context or None),
        )
    return token


def _load_token(token: str) -> sqlite3.Row | None:
    sweep_expired()
    with db() as c:
        row = c.execute("SELECT * FROM tokens WHERE token = ?", (token,)).fetchone()
    return row


@app.get("/r/{token}", response_class=HTMLResponse)
def reply_form(request: Request, token: str) -> HTMLResponse:
    row = _load_token(token)
    if row is None:
        return templates.TemplateResponse(request, "expired.html", {}, status_code=410)
    if row["used_at"] is not None:
        return templates.TemplateResponse(request, "used.html", {"target": row["target"]}, status_code=410)
    pane = pane_command(row["target"]) or "(pane gone)"
    return templates.TemplateResponse(
        request, "reply.html",
        {
            "token": token, "target": row["target"], "pane": pane,
            "title": row["title"], "context": row["context"],
        },
    )


@app.post("/r/{token}", response_class=HTMLResponse)
def reply_submit(request: Request, token: str, message: str = Form(...)) -> HTMLResponse:
    row = _load_token(token)
    if row is None:
        return templates.TemplateResponse(request, "expired.html", {}, status_code=410)
    if row["used_at"] is not None:
        return templates.TemplateResponse(request, "used.html", {"target": row["target"]}, status_code=410)

    ok, detail = send_to_pane(row["target"], message)
    if not ok:
        return templates.TemplateResponse(
            request, "error.html", {"detail": detail, "target": row["target"]}, status_code=409
        )

    with db() as c:
        c.execute("UPDATE tokens SET used_at = ? WHERE token = ?", (int(time.time()), token))

    return templates.TemplateResponse(
        request, "success.html", {"target": row["target"], "message": message}
    )
