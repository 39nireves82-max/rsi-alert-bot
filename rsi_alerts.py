import os
import requests
import pandas as pd
import yfinance as yf
from dotenv import load_dotenv

# 1. Pfade definieren
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PARENT_DIR = os.path.dirname(BASE_DIR)

# .env Datei laden
env_path = os.path.join(BASE_DIR, ".env")
load_dotenv(env_path, override=True)

# 2. Zugangsdaten auslesen
TELEGRAM_TOKEN = (
    os.getenv("TELEGRAM_BOT_TOKEN")
    or os.getenv("TELEGRAM_TOKEN")
    or os.getenv("BOT_TOKEN")
)
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID") or os.getenv("CHAT_ID")

# Pfad zur zentralen Ticker-Datei im Ordner "RSI Bot"
TICKER_FILE = os.path.join(PARENT_DIR, "RSI Bot", "ticker_liste.txt")


# Telegram Nachricht senden
def send_telegram_message(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("❌ Fehler: Telegram Zugangsdaten fehlen!")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown",
    }

    try:
        response = requests.post(url, json=payload, timeout=10)
        response.raise_for_status()
        print("\n✅ Alarm-Nachricht erfolgreich an Telegram gesendet!")
        return True
    except Exception as e:
        print(f"\n❌ Fehler beim Senden der Telegram-Nachricht: {e}")
        return False


# Ticker aus der zentralen Datei laden
def load_tickers():
    if not os.path.exists(TICKER_FILE):
        print(f"❌ Ticker-Datei nicht gefunden unter: {TICKER_FILE}")
        return []

    items = []
    with open(TICKER_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if ";" in line:
                parts = line.split(";", 1)
                ticker = parts[0].strip()
                name = parts[1].strip() if len(parts) > 1 else ticker
                if ticker:
                    items.append((ticker, name))

    return items


# RSI und Zielpreise berechnen
def calc_rsi_and_targets(df_ticker, t_30=30, t_70=70):
    df = df_ticker.dropna(subset=["Close"]).copy()
    if len(df) < 15:
        return None

    close = df["Close"]
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(alpha=1 / 14, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / 14, adjust=False).mean()
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))

    latest_close = close.iloc[-1]
    latest_rsi = rsi.iloc[-1]
    latest_ag = avg_gain.iloc[-1]
    latest_al = avg_loss.iloc[-1]

    al_n = latest_al * 13 / 14
    ag_n = latest_ag * 13 / 14

    p70 = latest_close + max(
        0, (t_70 / (100 - t_70) * al_n * 14) - (latest_ag * 13)
    )
    p30 = latest_close - max(
        0, (((ag_n * 14) / (t_30 / (100 - t_30))) - (latest_al * 13))
    )

    return latest_close, latest_rsi, p30, p70


# Hauptfunktion
def main():
    items = load_tickers()

    if not items:
        print("⚠️ Keine Ticker in der Datei gefunden.")
        return

    print(f"🔍 Überprüfe {len(items)} Ticker auf RSI-Signale...\n")

    alerts = []
    table_rows = []
    tickers_only = [item[0] for item in items]

    try:
        batch_df = yf.download(
            tickers_only,
            period="2y",
            interval="1wk",
            group_by="ticker",
            progress=False,
        )

        for ticker, name in items:
            try:
                if len(tickers_only) == 1:
                    df_single = batch_df.copy()
                else:
                    df_single = (
                        batch_df[ticker].copy()
                        if ticker in batch_df.columns.levels[0]
                        else pd.DataFrame()
                    )

                if df_single.empty or "Close" not in df_single:
                    continue

                tgt_low = (
                    35
                    if any(
                        x in ticker
                        for x in [
                            "IQQQ",
                            "6II0",
                            "ISPY",
                            "USPY",
                            "PG",
                            "XYL",
                            "ABBN",
                        ]
                    )
                    else 30
                )
                tgt_high = 70

                res = calc_rsi_and_targets(df_single, tgt_low, tgt_high)
                if not res:
                    continue

                close, rsi, p30, p70 = res

                try:
                    curr = yf.Ticker(ticker).fast_info.get("currency", "USD")
                except Exception:
                    curr = "USD"

                # Status für Tabellenausgabe ermitteln
                status = "NEUTRAL"
                if rsi <= tgt_low:
                    status = "🟢 KAUFEN"
                    alerts.append(
                        f"🟢 *KAUFSIGNAL: {name} ({ticker})*\n"
                        f"• Aktueller Kurs: {close:.2f} {curr}\n"
                        f"• Weekly RSI: *{rsi:.1f}* (Grenze: ≤ {tgt_low})\n"
                        f"• Ziel (Kaufzone): {p30:.2f} {curr}\n"
                    )
                elif rsi >= tgt_high:
                    status = "🔴 VERKAUFEN"
                    alerts.append(
                        f"🔴 *VERKAUFSIGNAL: {name} ({ticker})*\n"
                        f"• Aktueller Kurs: {close:.2f} {curr}\n"
                        f"• Weekly RSI: *{rsi:.1f}* (Grenze: ≥ {tgt_high})\n"
                        f"• Ziel (Verkauf): {p70:.2f} {curr}\n"
                    )

                # Daten für die CMD-Tabelle sammeln
                table_rows.append({
                    "Ticker": ticker,
                    "Name": name[:18],  # Namen für saubere Tabellenspalte kürzen
                    "Kurs": f"{close:.2f} {curr}",
                    "Weekly RSI": f"{rsi:.1f}",
                    "Status": status
                })

            except Exception as e:
                print(f"⚠️ Fehler bei {ticker}: {e}")

    except Exception as e:
        print(f"❌ Batch-Download-Fehler: {e}")

    # 1. TABELLEN-AUSGABE IN DER CMD
    if table_rows:
        df_display = pd.DataFrame(table_rows)
        print("=" * 65)
        print("📊 RSI BOT - ÜBERSICHT DER GEPRÜFTEN WERTE")
        print("=" * 65)
        print(df_display.to_string(index=False))
        print("=" * 65)

    # 2. TELEGRAM ALARME SENDEN
    if alerts:
        full_msg = (
            "🚨 *RSI ALARM BOT STATUS UPDATE* 🚨\n\n" + "\n---\n".join(alerts)
        )
        send_telegram_message(full_msg)
    else:
        print("\nℹ️ Keine RSI-Ausschläge im Kauf-/Verkauf-Bereich. Keine Telegram-Nachricht erforderlich.")


if __name__ == "__main__":
    main()