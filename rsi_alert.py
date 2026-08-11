import os
import yfinance as yf
import pandas as pd
import numpy as np
import requests

# 1. Telegram Zugangsdaten aus Umgebungsvariablen lesen
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

def send_telegram_message(message):
    """Sendet eine Push-Nachricht via Telegram API."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("Fehler: Telegram Zugangsdaten fehlen!")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown"
    }
    try:
        response = requests.post(url, json=payload)
        response.raise_for_status()
        print("Nachricht erfolgreich an Telegram gesendet!")
    except Exception as e:
        print(f"Fehler beim Senden der Telegram-Nachricht: {e}")

def main():
    print("Starte RSI-Testlauf...")
    # Test-Nachricht erzwingen
    test_msg = "🔔 *Test-Erfolg!* Dein RSI-Alert-Bot ist korrekt eingerichtet und einsatzbereit."
    send_telegram_message(test_msg)

if __name__ == "__main__":
    main()
