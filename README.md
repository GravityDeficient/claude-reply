# claude-reply

One-shot web form for replying to Claude Code prompts via `tmux send-keys`.

You're away from the keyboard. Claude pings your phone (via ntfy or any
push system that supports action buttons). You tap the notification. A web
form opens — pre-targeted at the right tmux pane. You type a reply. Claude
sees it as if you typed it.

## How it works

1. A `UserPromptSubmit` hook in Claude writes the current tmux address
   (`session:window.pane`) to `/tmp/claude-tmux-target` every turn.
2. When your notifier fires (e.g. ntfy), it POSTs to `/mint` on this
   service to get a one-shot token tied to that target, and adds a
   `Click:` + `Actions:` header to the push.
3. Tapping the notification opens `http://<your-host>:8765/r/<token>`.
4. The form POSTs back here. We validate the token, check the pane still
   runs Claude, and run `tmux send-keys -t <target> -l "<msg>"` + Enter.
5. Token is burned. Tokens auto-expire after 1 hour.

## Security model

- **Network**: bound to `0.0.0.0:8765` for LAN/VPN reach. No public exposure.
- **Mint endpoint**: requires `X-Mint-Secret` header (auto-generated, stored
  at `~/.config/claude-reply/mint-secret`, mode 600). Only the local
  notifier on the same host has the secret.
- **Reply endpoint**: gated by the unguessable URL token (16 random bytes,
  base64). One-shot, 1-hour TTL. No additional auth — relies on the
  trusted-network threat model.
- **Pane safety**: refuses to inject unless the target pane runs `claude`,
  `node`, `python`, or `python3` (override via `CLAUDE_REPLY_ALLOWED_COMMANDS`).
  Stops accidental paste into a root shell if you killed Claude.

## Install

```bash
git clone <this-repo> ~/Projects/claude-reply
cd ~/Projects/claude-reply
uv venv --python 3.11
uv pip install fastapi 'uvicorn[standard]' jinja2 python-multipart
```

Wire it into systemd as a user service:

```bash
ln -sf $PWD/claude-reply.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now claude-reply.service
loginctl enable-linger $(whoami)   # so it survives logout
```

Add the Claude hook (`~/.claude/settings.json`):

```json
{
  "hooks": {
    "UserPromptSubmit": [{
      "hooks": [{
        "type": "command",
        "command": "[ -n \"$TMUX\" ] && tmux display-message -p '#S:#I.#P' > /tmp/claude-tmux-target || true"
      }]
    }]
  }
}
```

Then teach your notifier to call `/mint` and attach the URL. Example shell
snippet for ntfy (the two `Click` and `Actions` headers are the magic):

```bash
SECRET=$(cat ~/.config/claude-reply/mint-secret)
TARGET=$(cat /tmp/claude-tmux-target 2>/dev/null)
TOKEN=$(curl -sf -X POST http://127.0.0.1:8765/mint \
    -H "X-Mint-Secret: $SECRET" \
    --data-urlencode "target=$TARGET" \
    --data-urlencode "title=$NOTIFICATION_TITLE" \
    --data-urlencode "context=$NOTIFICATION_BODY")

curl -X POST "$NTFY_URL/$NTFY_TOPIC" \
    -H "Title: $NOTIFICATION_TITLE" \
    -H "Click: $PUBLIC_BASE/r/$TOKEN" \
    -H "Actions: view, Reply, $PUBLIC_BASE/r/$TOKEN, clear=true" \
    -d "$NOTIFICATION_BODY"
```

`$PUBLIC_BASE` is whatever URL phones use to reach this service —
usually `http://<host-lan-ip>:8765` or a hostname behind a reverse proxy.

## Service ops

```bash
systemctl --user status claude-reply.service
systemctl --user restart claude-reply.service
journalctl --user -u claude-reply.service -f
```

Listens on `0.0.0.0:8765`. SQLite at `~/.config/claude-reply/tokens.sqlite`.

## Env overrides (read by the notifier shim)

| Env var | Default | What it does |
| --- | --- | --- |
| `CLAUDE_REPLY_TARGET_FILE`         | `/tmp/claude-tmux-target`              | Where to read the tmux address |
| `CLAUDE_REPLY_MINT_URL`            | `http://127.0.0.1:8765/mint`           | Where to mint tokens |
| `CLAUDE_REPLY_PUBLIC_BASE`         | _(none — set per host)_                | Base URL embedded in the notification |
| `CLAUDE_REPLY_SECRET_PATH`         | `~/.config/claude-reply/mint-secret`   | Where to read the mint secret |
| `CLAUDE_REPLY_ALLOWED_COMMANDS`    | `claude,node,python,python3`           | Pane allowlist (csv) |

## Files

| Path | Purpose |
| --- | --- |
| `app.py` | FastAPI app: routes, SQLite store, send-keys |
| `templates/` | Jinja templates (mobile-first, dark/light auto) |
| `claude-reply.service` | systemd user unit |
| `~/.config/claude-reply/mint-secret` | Auto-generated on first start |
| `~/.config/claude-reply/tokens.sqlite` | Token store |
| `/tmp/claude-tmux-target` | Current Claude tmux pane (written by hook) |

## Manual smoke test

```bash
SECRET=$(cat ~/.config/claude-reply/mint-secret)
TOKEN=$(curl -sX POST http://127.0.0.1:8765/mint \
    -H "X-Mint-Secret: $SECRET" \
    -d "target=$(cat /tmp/claude-tmux-target)")
echo "open: http://<your-host>:8765/r/$TOKEN"
```

## Known limitations

- Single-target file at `/tmp/claude-tmux-target` — concurrent Claude
  sessions on the same host race each other (last write wins). Fine for a
  single-user setup; would need per-PID files for multi-session.
- No HTTPS at the service itself. Either reverse-proxy to add TLS, or live
  with `http://` on a trusted network. The token is sent in the URL, so on
  open Wi-Fi you'd want HTTPS — over a VPN you're already encrypted at the
  tunnel layer.
- "Pane no longer hosts Claude" error doesn't burn the token. Friendly
  when Claude is briefly missing, but means a stolen URL can wait for a
  Claude session to land in that pane. Acceptable given the 1h TTL +
  trusted-network assumption.

## License

MIT
