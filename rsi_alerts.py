import os
import requests
import yfinance as yf
import pandas as pd
import numpy as np

# ==========================================
# CONFIGURATION
# ==========================================
BOT_TOKEN = "8923271103:AAE4v7A-56LMHuGsQVfzkkpAMorVRoe3vSw"
CHAT_ID = "8689066548"
FILE_PATH = "ticker_liste.txt"

# ==========================================
# TELEGRAM FUNCTION
# ==========================================
def send_telegram_message(message: str):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": message,
        "parse_mode": "Markdown"
    }
    try:
        response = requests.post(url, json=payload, timeout=10)
        response.raise_for_status()
    except Exception as e:
        print(f"Fehler beim Senden der Telegram-Nachricht: {e}")

# ==========================================
# DATA LOADING & RSI CALCULATION
# ==========================================
def load_items():
    if not os.path.exists(FILE_PATH):
        return []
    with open(FILE_PATH, "r", encoding="utf-8") as f:
        saved_raw = f.read()
    items = []
    for item in saved_raw.split("|"):
        if ";" in item:
            parts = item.split(";")
            if len(parts) >= 2:
                items.append([parts[0].strip(), parts[1].strip()])
    return items

def calc_rsi_and_targets(df_ticker, t_30, t_70):
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
    df["RSI"] = 100 - (100 / (1 + rs))
    
    latest_close = close.iloc[-1]
    latest_ag = avg_gain.iloc[-1]
    latest_al = avg_loss.iloc[-1]
    
    al_n = latest_al * 13 / 14
    ag_n = latest_ag * 13 / 14
    
    p70 = latest_close + max(0, (t_70 / (100 - t_70) * al_n * 14) - (latest_ag * 13))
    p30 = latest_close - max(0, (((ag_n * 14) / (t_30 / (100 - t_30))) - (latest_al * 13)))
    
    return df["RSI"].iloc[-1], p30, p70, latest_close

# ==========================================
# MAIN CHECK ROUTINE
# ==========================================
def run_alert_check():
    items = load_items()
    if not items:
        print("Keine Ticker in ticker_liste.txt gefunden.")
        return

    tickers_list = [item[0] for item in items]
    print(f"Prüfe {len(tickers_list)} Ticker auf RSI-Signale...")

    try:
        batch_df = yf.download(tickers_list, period="5y", interval="1wk", group_by="ticker", progress=False)
    except Exception as e:
        print(f"Fehler beim Download der Daten: {e}")
        return

    alerts = []

    for sym, name in items:
        tgt_low = 35 if any(x in sym for x in ["IQQQ", "6II0", "USPY", "PG", "XYL", "ABBN"]) else 30
        tgt_high = 70

        if len(tickers_list) == 1:
            d_ticker = batch_df.copy()
        else:
            d_ticker = batch_df[sym].copy() if sym in batch_df.columns.levels[0] else pd.DataFrame()

        if not d_ticker.empty and "Close" in d_ticker and len(d_ticker["Close"].dropna()) >= 15:
            res = calc_rsi_and_targets(d_ticker, tgt_low, tgt_high)
            if res:
                current_rsi, target_buy, target_sell, current_price = res
                
                # Signal-Prüfung
                if current_rsi <= tgt_low:
                    alerts.append(
                        f"🟢 *KAUFSIGNAL (Überverkauft)*\n"
                        f"• *Name:* {name} (`{sym}`)\n"
                        f"• *Aktueller RSI:* `{current_rsi:.2f}` (Ziel: <= {tgt_low})\n"
                        f"• *Aktueller Kurs:* `{current_price:.2f}`\n"
                        f"• *Zielpreis Kaufzone:* `{target_buy:.2f}`"
                    )
                elif current_rsi >= tgt_high:
                    alerts.append(
                        f"🔴 *VERKAUFSIGNAL (Überkauft)*\n"
                        f"• *Name:* {name} (`{sym}`)\n"
                        f"• *Aktueller RSI:* `{current_rsi:.2f}` (Ziel: >= {tgt_high})\n"
                        f"• *Aktueller Kurs:* `{current_price:.2f}`\n"
                        f"• *Zielpreis Verkauf:* `{target_sell:.2f}`"
                    )

    if alerts:
        message_header = "🚨 *RSI ALARM NOTIFICATION* 🚨\n\n"
        full_message = message_header + "\n\n---\n\n".join(alerts)
        send_telegram_message(full_message)
        print(f"{len(alerts)} Alarm(e) erfolgreich an Telegram gesendet!")
    else:
        print("Keine RSI-Grenzwerte überschritten. Keine Benachrichtigung gesendet.")

if __name__ == "__main__":
    run_alert_check()