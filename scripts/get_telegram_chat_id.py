"""
get_telegram_chat_id.py -- Interactive Telegram Chat ID Finder & Verifier
========================================================================
Run this script, then send any message (or /start) to your bot on Telegram.
This script detects the message, displays your Chat ID, automatically updates .env,
and sends a test confirmation message!
"""

import os
import sys
import time
import json
import urllib.request
from dotenv import load_dotenv

env_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".env"))
load_dotenv(env_path)

token = os.environ.get("TELEGRAM_BOT_TOKEN")
if not token:
    print("ERROR: TELEGRAM_BOT_TOKEN not found in .env!")
    sys.exit(1)

# Check bot identity
try:
    me_url = f"https://api.telegram.org/bot{token}/getMe"
    with urllib.request.urlopen(urllib.request.Request(me_url), timeout=10) as resp:
        bot_info = json.loads(resp.read().decode("utf-8")).get("result", {})
        bot_username = bot_info.get("username", "UnknownBot")
        bot_name = bot_info.get("first_name", "Bot")
except Exception as e:
    print(f"ERROR: Could not connect to Telegram API with your token: {e}")
    sys.exit(1)

print("=" * 70)
print(f"TELEGRAM CHAT ID FINDER FOR: @{bot_username} ({bot_name})")
print("=" * 70)
print(f"1. Open Telegram on your phone or desktop.")
print(f"2. Search for: @{bot_username}")
print(f"   (or open link: https://t.me/{bot_username})")
print(f"3. Click START or send any message (e.g. 'hello').")
print("=" * 70)
print("Listening for incoming messages... (Waiting up to 60s)")

found_chat = None
start_t = time.time()

while time.time() - start_t < 60:
    url = f"https://api.telegram.org/bot{token}/getUpdates"
    try:
        with urllib.request.urlopen(urllib.request.Request(url), timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            results = data.get("result", [])
            if results:
                last_update = results[-1]
                msg = last_update.get("message") or last_update.get("channel_post") or {}
                chat = msg.get("chat", {})
                chat_id = chat.get("id")
                if chat_id:
                    found_chat = {
                        "id": str(chat_id),
                        "type": chat.get("type", "private"),
                        "title": chat.get("title") or chat.get("first_name") or chat.get("username", "User"),
                    }
                    break
    except Exception:
        pass
    time.sleep(2)

if not found_chat:
    print("\nTimeout: No message received in 60s.")
    print("Please send a message to @" + bot_username + " and run this script again.")
    sys.exit(0)

cid = found_chat["id"]
ctype = found_chat["type"]
cname = found_chat["title"]

print("\n" + "=" * 70)
print(f"SUCCESS! DETECTED CHAT ID: {cid}")
print(f"Chat Type:  {ctype}")
print(f"Chat Name:  {cname}")
print("=" * 70)

# Update .env automatically
with open(env_path, "r", encoding="utf-8") as f:
    lines = f.readlines()

new_lines = []
for line in lines:
    if line.strip().startswith("TELEGRAM_CHAT_ID="):
        new_lines.append(f"TELEGRAM_CHAT_ID={cid}\n")
    elif line.strip().startswith("TELEGRAM_ENABLED="):
        new_lines.append("TELEGRAM_ENABLED=true\n")
    else:
        new_lines.append(line)

with open(env_path, "w", encoding="utf-8") as f:
    f.writelines(new_lines)

print(f">> Automatically saved TELEGRAM_CHAT_ID={cid} into .env")

# Send confirmation message
send_url = f"https://api.telegram.org/bot{token}/sendMessage"
payload = json.dumps({
    "chat_id": cid,
    "text": "<b>NSE-Quant-Lab Alerts Active</b>\nTelegram chat ID configured successfully! Ready for live signals & fills.",
    "parse_mode": "HTML"
}).encode("utf-8")
headers = {"Content-Type": "application/json"}
try:
    req = urllib.request.Request(send_url, data=payload, headers=headers)
    with urllib.request.urlopen(req, timeout=10) as resp:
        print(">> Dispatched test alert to your Telegram! Check your Telegram app.")
except Exception as e:
    print(f"Warning: Could not send test message: {e}")

print("=" * 70)
