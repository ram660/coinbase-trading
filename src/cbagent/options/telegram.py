"""Send messages with a Telegram bot. The token and chat id come from the environment (GitHub secrets in the cloud,
config/.env locally) and are never printed."""
from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request

API = "https://api.telegram.org/bot{token}/{method}"
MAX_LEN = 4000  # Telegram's limit is 4096 characters


def credentials(env: dict[str, str] | None = None) -> tuple[str, str]:
    env = {**(env or {}), **{k: v for k, v in os.environ.items() if k.startswith("TELEGRAM_") and v}}
    return env.get("TELEGRAM_BOT_TOKEN", ""), env.get("TELEGRAM_CHAT_ID", "")


def _call(token: str, method: str, params: dict) -> dict:
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(API.format(token=token, method=method), data=data)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:  # the URL holds the token, so report only Telegram's own description
        try:
            desc = json.load(e).get("description", "")
        except Exception:
            desc = ""
        raise RuntimeError(f"Telegram {method} failed: HTTP {e.code} {desc}".rstrip()) from None
    except urllib.error.URLError as e:
        raise RuntimeError(f"Telegram {method} failed: {e.reason}") from None


def chunks(text: str, size: int = MAX_LEN) -> list[str]:
    """Split on blank lines, then lines, so each message stays under Telegram's limit."""
    out, cur = [], ""
    for block in text.split("\n"):
        while len(block) > size:
            out.append(block[:size])
            block = block[size:]
        if len(cur) + len(block) + 1 > size:
            out.append(cur)
            cur = block
        else:
            cur = f"{cur}\n{block}" if cur else block
    if cur:
        out.append(cur)
    return out


def send(text: str, env: dict[str, str] | None = None, call=_call) -> int:
    """Send text as plain messages (no Markdown, so tickers and prices never break parsing). Returns how many were sent."""
    token, chat = credentials(env)
    if not token or not chat:
        raise RuntimeError("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID (GitHub secrets, or config/.env locally).")
    parts = chunks(text)
    for part in parts:
        call(token, "sendMessage", {"chat_id": chat, "text": part, "disable_web_page_preview": "true"})
    return len(parts)


def find_chat_ids(env: dict[str, str] | None = None, call=_call) -> list[str]:
    """Chats that messaged the bot recently, as 'id  name' lines. Message the bot first, then run this once."""
    token, _ = credentials(env)
    if not token:
        raise RuntimeError("Set TELEGRAM_BOT_TOKEN first.")
    seen: dict[str, str] = {}
    for upd in call(token, "getUpdates", {}).get("result", []):
        chat = (upd.get("message") or upd.get("channel_post") or {}).get("chat") or {}
        if "id" in chat:
            seen[str(chat["id"])] = chat.get("username") or chat.get("title") or chat.get("first_name") or ""
    return [f"{cid}  {name}".rstrip() for cid, name in seen.items()]
