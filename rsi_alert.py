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
    except Exception as e:
        print(f"Fehler beim Senden der Telegram-Nachricht: {e}")

# 2. RSI Berechnung (Wilder's RSI)
def calc_rsi(df_ticker):
    df = df_ticker.dropna(subset=["Close"]).copy()
    if len(df) < 15:
        return None
    close = df["Close"]
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1/14, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/14, adjust=False).mean()
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return rsi.iloc[-1]

# 3. Zu überwachende Ticker & individuelle Schwellenwerte
WATCHLIST = [
    {"sym": "PANW", "name": "Palo Alto Networks", "target_rsi": 30},
    {"sym": "PLTR", "name": "Palantir Technologies", "target_rsi": 30},
    {"sym": "XYL", "name": "Xylem Inc.", "target_rsi": 35},
    {"sym": "USPY", "name": "iShares S&P 500", "target_rsi": 99},
    {"sym": "IQQQ.DE", "name": "iShares Global Water", "target_rsi": 35},
    {"sym": "PG", "name": "Procter & Gamble", "target_rsi": 35},
    {"sym": "BTC-USD", "name": "Bitcoin USD", "target_rsi": 30},
    {"sym": "XPEV", "name": "XPeng Inc.", "target_rsi": 30},
]

def main():
    print("Starte RSI-Analyse...")
    alerts = []

    for item in WATCHLIST:
        sym = item["sym"]
        name = item["name"]
        target = item["target_rsi"]

        try:
            # Wöchentliche Daten für nachhaltige Signale (interval="1wk")
            ticker_data = yf.download(sym, period="1y", interval="1wk", progress=False)
            if not ticker_data.empty:
                current_rsi = calc_rsi(ticker_data)
                last_price = ticker_data["Close"].iloc[-1]
                
                # Wenn der RSI unter oder genau auf dem Zielwert liegt -> ALARM
                if current_rsi and current_rsi <= target:
                    alerts.append(
                        f"🚨 *RSI ALERT (Wöchentlich)*\n\n"
                        f"📌 *{name}* (`{sym}`)\n"
                        f"🔹 Aktueller Kurs: `${last_price:.2f}`\n"
                        f"📊 Aktueller RSI: *{current_rsi:.1f}* (Ziel: ≤ {target})\n"
                        f"💡 _Kaufzone/Chance erreicht!_"
                    )
        except Exception as e:
            print(f"Fehler bei Ticker {sym}: {e}")

    # Wenn Alarme vorhanden sind, als Nachricht senden
    if alerts:
        full_message = "\n\n------------------------\n\n".join(alerts)
        send_telegram_message(full_message)
        print(f"Es wurden {len(alerts)} Alarme gesendet!")
    else:
        print("Keine RSI-Auffälligkeiten heute.")

if __name__ == "__main__":
    main()
