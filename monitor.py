#!/usr/bin/env python3
"""SSC notification monitor: fetch notices, detect new ones, alert via Telegram.

State (state/seen.json) is only ever extended with notices whose Telegram alert
was delivered (or with the baseline on the first successful run). A source
failure never changes it.
"""

import html
import json
import os
import socket
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Dict, List

from sources import ssc

STATE_PATH = Path(__file__).resolve().parent / "state" / "seen.json"
STATE_KEY = "SSC"
MAX_SEEN = 5000

MAX_TITLE_CHARS = 300
MAX_MESSAGE_CHARS = 4000
TELEGRAM_TIMEOUT = 20
TELEGRAM_ATTEMPTS = 3


class StateError(Exception):
    pass


class TelegramError(Exception):
    pass


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------

def load_state(path: Path) -> List[str]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise StateError(f"cannot read {path.name}: {exc}") from None
    ids = data.get(STATE_KEY) if isinstance(data, dict) else None
    if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
        raise StateError(f'{path.name} must be {{"{STATE_KEY}": [<string ids>]}}')
    return list(dict.fromkeys(ids))


def save_state(path: Path, ids: List[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".seen-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({STATE_KEY: ids}, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


# --------------------------------------------------------------------------
# Telegram
# --------------------------------------------------------------------------

def _truncate(text: str, limit: int) -> str:
    # Truncate the raw text BEFORE escaping so HTML entities are never split.
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _esc(text: str) -> str:
    return html.escape(text, quote=False)


def format_message(notice: Dict[str, str]) -> str:
    title = _esc(_truncate(notice["title"], MAX_TITLE_CHARS))
    date = _esc(notice["date"] or "Not specified")
    url = notice["url"]

    def build(include_plain_url: bool) -> str:
        lines = [
            "\U0001F6A8 <b>NEW SSC NOTIFICATION</b>",
            "",
            f"\U0001F4CC {title}",
            "",
            f"\U0001F4C5 Date: {date}",
        ]
        if url:
            href = _esc(url).replace('"', "&quot;")
            lines += ["", f'\U0001F517 <a href="{href}">Open Official Notification</a>']
            if include_plain_url:
                lines.append(_esc(url))
        lines += ["", "Source: Staff Selection Commission (SSC)"]
        return "\n".join(lines)

    text = build(True)
    if len(text) > MAX_MESSAGE_CHARS:
        text = build(False)
    return text


def _telegram_description(exc: urllib.error.HTTPError) -> str:
    try:
        data = json.loads(exc.read(4096).decode("utf-8", errors="replace"))
        return str(data.get("description", ""))[:200]
    except (ValueError, OSError, AttributeError):
        return ""


def send_telegram(
    token: str,
    chat_id: str,
    text: str,
    *,
    opener: Callable = urllib.request.urlopen,
    sleep: Callable = time.sleep,
) -> None:
    """Send one message via the official Telegram Bot API.

    Raises TelegramError on failure. Messages never include the token.
    """
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=json.dumps(
            {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }
        ).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    last_error = "unknown error"
    for attempt in range(1, TELEGRAM_ATTEMPTS + 1):
        delay = 2.0 ** attempt
        try:
            with opener(request, timeout=TELEGRAM_TIMEOUT) as response:
                body = response.read()
            payload = json.loads(body)
        except urllib.error.HTTPError as exc:
            code = exc.code
            description = _telegram_description(exc)
            exc.close()
            if code == 429:
                delay = 5.0
            elif code < 500:
                raise TelegramError(f"Telegram API HTTP {code}: {description}") from None
            last_error = f"HTTP {code}"
        except (OSError, socket.timeout, ValueError) as exc:
            last_error = f"{type(exc).__name__}"
        else:
            if isinstance(payload, dict) and payload.get("ok") is True:
                return
            description = payload.get("description", "") if isinstance(payload, dict) else ""
            raise TelegramError(f"Telegram rejected the message: {str(description)[:200]}")

        if attempt < TELEGRAM_ATTEMPTS:
            sleep(delay)

    raise TelegramError(f"Telegram send failed after {TELEGRAM_ATTEMPTS} attempts: {last_error}")


# --------------------------------------------------------------------------
# Monitor
# --------------------------------------------------------------------------

def run(
    fetch: Callable[[], List[Dict[str, str]]],
    send: Callable[[str], None],
    state_path: Path = STATE_PATH,
) -> int:
    try:
        previous = load_state(state_path)
    except StateError as exc:
        print(f"STATE ERROR: {exc}. State left untouched.")
        return 1

    try:
        notices = fetch()
    except ssc.SourceError as exc:
        print(f"SOURCE FAILURE: {exc}")
        print("State unchanged. No alerts sent.")
        return 1

    if not notices:
        print("SOURCE FAILURE: SSC returned no records (treated as failure, not as 'no notices').")
        print("State unchanged. No alerts sent.")
        return 1

    current_ids = [n["notice_id"] for n in notices]
    print(f"SSC records found: {len(notices)}")

    if not previous:
        save_state(state_path, current_ids[:MAX_SEEN])
        print(f"Baseline created: {len(notices)}")
        print("New alerts: 0")
        return 0

    seen = set(previous)
    if not seen.intersection(current_ids):
        print(
            "SOURCE FAILURE: none of the previously seen notice IDs appear in the "
            "current SSC listing (source or ID format changed?). Refusing to alert."
        )
        print("State unchanged. No alerts sent.")
        return 1

    new_notices = [n for n in notices if n["notice_id"] not in seen]
    state = list(previous)
    sent = 0
    failed = False
    for notice in new_notices:
        try:
            send(format_message(notice))
        except TelegramError as exc:
            print(f"TELEGRAM FAILURE for notice {notice['notice_id']}: {exc}")
            failed = True
            break
        state.append(notice["notice_id"])
        save_state(state_path, state[-MAX_SEEN:])
        sent += 1

    print(f"New notices: {len(new_notices)}")
    print(f"New alerts sent: {sent}")
    if failed:
        print(f"Undelivered (will be retried next run): {len(new_notices) - sent}")
        return 1
    return 0


def main() -> int:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        print("ERROR: TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set.")
        return 1
    return run(
        ssc.fetch_notices,
        lambda text: send_telegram(token, chat_id, text),
        STATE_PATH,
    )


if __name__ == "__main__":
    sys.exit(main())
