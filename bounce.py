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
HERE         = os.path.dirname(os.path.abspath(__file__))
APP_SCRIPT   = os.path.join(HERE, "main.py")
VENV_PYTHON  = os.path.join(HERE, ".venv", "bin", "python")
LOG_FILE     = "/tmp/t-rade.out"


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


def find_display():
    """Return a usable DISPLAY value, or None if none found."""
    # Check env first (works when called from an X session)
    if os.environ.get("DISPLAY"):
        return os.environ["DISPLAY"]
    # Try common VNC / X displays on Pi
    for d in (":0", ":1", ":10"):
        result = subprocess.run(
            ["xdpyinfo", "-display", d],
            capture_output=True, text=True
        )
        if result.returncode == 0:
            return d
    return None


def launch(extra_args):
    env = os.environ.copy()
    display = find_display()
    if display:
        env["DISPLAY"] = display
    log_out = open(LOG_FILE, "a")
    proc = subprocess.Popen(
        [VENV_PYTHON, APP_SCRIPT] + extra_args,
        cwd=HERE,
        env=env,
        stdout=log_out,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    return proc


def restart_app():
    """
    Kill any existing main.py, then try:
      1. GUI mode (--autostart) — wait 15s and confirm it's still running.
      2. If GUI dies, fall back to --headless --autostart.
    Returns a string describing what was launched.
    """
    print(f"[{_ts()}] Killing existing main.py...")
    subprocess.run(["pkill", "-f", "python.*main.py"], capture_output=True)
    time.sleep(3)

    display = find_display()
    if display:
        print(f"[{_ts()}] Display found: {display}. Trying GUI mode...")
        launch(["--autostart"])
        time.sleep(15)   # give Tkinter time to fully initialise (or crash)
        if is_running():
            print(f"[{_ts()}] GUI mode running OK.")
            return "GUI mode (--autostart)"
        # GUI failed — log the crash output
        crash = tail_log()
        print(f"[{_ts()}] GUI launch failed. Last log:\n{crash}")
        print(f"[{_ts()}] Falling back to headless mode...")
    else:
        print(f"[{_ts()}] No display found. Starting in headless mode directly.")

    # Headless fallback
    time.sleep(2)
    launch(["--headless", "--autostart"])
    time.sleep(10)
    if is_running():
        print(f"[{_ts()}] Headless mode running OK.")
        return "headless mode (--autostart)"
    return None   # both attempts failed


# ── Main ──────────────────────────────────────────────────────────────────────

offset  = load_offset()
updates = fetch_updates(offset)

bounce_requested = False
new_offset = offset

for update in updates:
    uid  = update.get("update_id", 0)
    new_offset = max(new_offset, uid + 1)

    msg     = update.get("message", {})
    text    = msg.get("text", "").strip()
    chat_id = str(msg.get("chat", {}).get("id", ""))

    if text.lower().startswith("/bounce") and chat_id == TELEGRAM_CHAT:
        bounce_requested = True

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
