#!/usr/bin/env python3
"""Quick test for Telegram API access using BOT_TOKEN from .env

Run this on the phone in the repo directory. It prints the getMe and getMyCommands responses
so we can verify the bot token and network connectivity.
"""
from dotenv import load_dotenv
import os
import requests
import sys

load_dotenv('.env')
BOT_TOKEN = os.getenv('BOT_TOKEN')
CHAT_ID = os.getenv('CHAT_ID')

if not BOT_TOKEN:
    print('BOT_TOKEN not set in .env')
    sys.exit(1)

TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

print('CHAT_ID set:', bool(CHAT_ID))

try:
    r = requests.get(f"{TG_API}/getMe", timeout=10)
    print('getMe status:', r.status_code)
    print(r.text[:2000])
except Exception as e:
    print('getMe exception:', e)

try:
    r2 = requests.get(f"{TG_API}/getMyCommands", timeout=10)
    print('getMyCommands status:', r2.status_code)
    print(r2.text[:2000])
except Exception as e:
    print('getMyCommands exception:', e)
