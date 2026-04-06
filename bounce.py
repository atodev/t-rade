"""
bounce.py — Telegram /bounce command listener + process watchdog.

Add to crontab (runs every minute):
  * * * * * cd /home/tombutler/t-rade && /home/tombutler/t-rade/.venv/bin/python bounce.py >> /tmp/bounce.out 2>&1

Behaviour
---------
- Polls Telegram getUpdates (offset stored in .telegram_offset to avoid replays).
- /bounce → kills main.py, tries GUI launch; if that dies within 15s falls back
  to --headless mode. Sends Telegram confirmation with mode used.
- If main.py is not found running → sends a one-time "stalled" alert.
  Flag cleared only after a confirmed successful restart.
"""

import os
import json
import subprocess
import urllib.request
import urllib.parse
import time
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT  = os.getenv("TELEGRAM_CHAT_ID")

OFFSET_FILE  = ".telegram_offset"
DEAD_FLAG    = ".process_dead_alerted"
CMD_FILE     = ".t-rade-cmd"
HERE         = os.path.dirname(os.path.abspath(__file__))
APP_SCRIPT   = os.path.join(HERE, "main.py")
VENV_PYTHON  = os.path.join(HERE, ".venv", "bin", "python")
LOG_FILE     = "/tmp/t-rade.out"
STATUS_LINES = 25


# ── Helpers ───────────────────────────────────────────────────────────────────

def _ts():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def send(msg):
    data = urllib.parse.urlencode({"chat_id": TELEGRAM_CHAT, "text": msg}).encode()
    req  = urllib.request.Request(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", data=data
    )
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print(f"[{_ts()}] send failed: {e}")


def fetch_updates(offset):
    url = (
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
        f"/getUpdates?offset={offset}&limit=20&timeout=0"
    )
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return json.loads(r.read()).get("result", [])
    except Exception as e:
        print(f"[{_ts()}] getUpdates failed: {e}")
        return []


def load_offset():
    try:
        with open(OFFSET_FILE) as f:
            return int(f.read().strip())
    except Exception:
        return 0


def save_offset(val):
    with open(OFFSET_FILE, "w") as f:
        f.write(str(val))


def is_running():
    try:
        result = subprocess.run(
            ["pgrep", "-f", "python.*main.py"],
            capture_output=True, text=True
        )
        return bool(result.stdout.strip())
    except Exception:
        return False


def tail_log(n=8):
    """Return last n lines of t-rade.out for crash diagnostics."""
    try:
        with open(LOG_FILE) as f:
            lines = f.readlines()
        return "".join(lines[-n:]).strip()
    except Exception:
        return "(log unavailable)"


def restart_app():
    """Kill any existing main.py and relaunch in headless mode."""
    print(f"[{_ts()}] Killing existing main.py...")
    subprocess.run(["pkill", "-f", "python.*main.py"], capture_output=True)
    time.sleep(3)

    env = os.environ.copy()
    log_out = open(LOG_FILE, "a")
    subprocess.Popen(
        [VENV_PYTHON, APP_SCRIPT, "--headless", "--autostart"],
        cwd=HERE,
        env=env,
        stdout=log_out,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    print(f"[{_ts()}] main.py launched in headless mode.")
    time.sleep(8)
    if is_running():
        return "headless mode"
    crash = tail_log()
    print(f"[{_ts()}] Launch failed. Last log:\n{crash}")
    return None


# ── Main ──────────────────────────────────────────────────────────────────────

offset  = load_offset()
updates = fetch_updates(offset)

bounce_requested = False
status_requested = False
start_requested  = False
stop_requested   = False
menu_requested   = False
new_offset = offset

for update in updates:
    uid  = update.get("update_id", 0)
    new_offset = max(new_offset, uid + 1)

    msg     = update.get("message", {})
    text    = msg.get("text", "").strip().lower()
    chat_id = str(msg.get("chat", {}).get("id", ""))

    if chat_id != TELEGRAM_CHAT:
        continue
    if text.startswith("/bounce"):
        bounce_requested = True
    elif text.startswith("/status"):
        status_requested = True
    elif text.startswith("/start"):
        start_requested = True
    elif text.startswith("/stop"):
        stop_requested = True
    elif text.startswith("/menu"):
        menu_requested = True

save_offset(new_offset)

if bounce_requested:
    send("🔄 /bounce received — restarting t-rade now...")
    print(f"[{_ts()}] /bounce command received.")
    mode = restart_app()
    if mode:
        send(f"✅ t-rade restarted in {mode}. Trading will begin automatically.")
        if os.path.exists(DEAD_FLAG):
            os.remove(DEAD_FLAG)
    else:
        crash = tail_log()
        send(
            f"⚠️ Restart failed — process died immediately.\n"
            f"Last log:\n{crash}"
        )

if menu_requested:
    send(
        "📟 t-rade commands:\n\n"
        "/status  — last 25 lines of the live log\n"
        "/start   — start the trading session\n"
        "/stop    — stop the trading session\n"
        "/bounce  — kill and restart the process\n"
        "/menu    — show this list"
    )

if status_requested:
    log = tail_log(STATUS_LINES)
    send(f"📋 t-rade log (last {STATUS_LINES} lines):\n\n{log}")

if start_requested:
    if is_running():
        with open(os.path.join(HERE, CMD_FILE), "w") as f:
            f.write("start")
        send("▶️ /start sent to t-rade.")
    else:
        send("⚠️ t-rade is not running. Send /bounce first.")

if stop_requested:
    if is_running():
        with open(os.path.join(HERE, CMD_FILE), "w") as f:
            f.write("stop")
        send("⏹️ /stop sent to t-rade.")
    else:
        send("⚠️ t-rade is not running.")

elif not is_running():
    if not os.path.exists(DEAD_FLAG):
        send(
            "🚨 t-rade has stalled — process is not running!\n"
            "Send /bounce to restart it."
        )
        with open(DEAD_FLAG, "w") as f:
            f.write(_ts())
        print(f"[{_ts()}] main.py not running — alert sent.")
    else:
        print(f"[{_ts()}] main.py still not running (awaiting /bounce).")

else:
    if os.path.exists(DEAD_FLAG):
        os.remove(DEAD_FLAG)
    print(f"[{_ts()}] main.py running OK.")
