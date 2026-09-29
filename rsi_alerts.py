import os
import requests
import pandas as pd
import yfinance as yf
from dotenv import load_dotenv
import json
import datetime
import time
try:
    import zoneinfo
except ImportError:
    from backports import zoneinfo  # type: ignore

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
POLLING_INTERVAL = int(os.getenv("POLLING_INTERVAL", 60))
TICKER_FILE = os.path.join(PARENT_DIR, "RSI Bot", "ticker_liste.txt")
JOURNAL_FILE = os.path.join(BASE_DIR, "trade_journal.csv")
CONFIG_FILE = os.path.join(BASE_DIR, "ticker_config.json")
ALERT_STATE_FILE = os.path.join(BASE_DIR, "alert_state.json")

# Telegram Nachricht senden
USERS_DIR = os.path.join(PARENT_DIR, "Trading Dashboard", "users")

# Telegram Nachricht senden
def send_telegram_message(message, chat_id_target=TELEGRAM_CHAT_ID):
    if not TELEGRAM_TOKEN or not chat_id_target:
        return False

    # Unterstützt kommaseparierte Chat-IDs
    chat_ids = [cid.strip() for cid in str(chat_id_target).split(',') if cid.strip()]
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    success = True

    for chat_id in chat_ids:
        payload = {
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "HTML",
        }
        response = None
        try:
            response = requests.post(url, json=payload, timeout=10)
            response.raise_for_status()
        except Exception as e:
            err_detail = f" | Server-Antwort: {response.text}" if response is not None else ""
            print(f"\n❌ Fehler beim Senden an {chat_id}: {e}{err_detail}")
            success = False

    if success:
        print(f"\n✅ Alarm-Nachricht an {len(chat_ids)} Empfänger gesendet!")
    return success


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

def get_current_price(symbol):
    """
    Isolierte Hilfsfunktion für den Live-Kurs.
    Aktuell via yfinance (fast_info mit History-Fallback), vorbereitet für Tradovate WS/REST.
    """
    try:
        price = float(yf.Ticker(symbol).fast_info.get("lastPrice", 0.0))
        if price > 0:
            return price
    except Exception:
        pass
    try:
        hist = yf.Ticker(symbol).history(period="5d")
        if not hist.empty and "Close" in hist.columns:
            return float(hist["Close"].dropna().iloc[-1])
    except Exception:
        pass
    return 0.0

def check_open_trades(user_dir):
    journal_file = os.path.join(user_dir, "trade_journal.csv")
    config_file = os.path.join(user_dir, "ticker_config.json")
    alert_state_file = os.path.join(user_dir, "alert_state.json")
    
    if not os.path.exists(journal_file):
        return [], {}, alert_state_file
    
    try:
        df_journal = pd.read_csv(journal_file)
        open_trades = df_journal[df_journal['status'] == 'OPEN']
    except Exception:
        return [], {}, alert_state_file
        
    if open_trades.empty:
        return [], {}, alert_state_file

    config = {}
    if os.path.exists(config_file):
        with open(config_file, "r", encoding="utf-8") as f:
            config = json.load(f)
    
    alert_state = {}
    if os.path.exists(alert_state_file):
        with open(alert_state_file, "r", encoding="utf-8") as f:
            alert_state = json.load(f)

    alerts = []
    journal_modified = False

    for idx, trade in open_trades.iterrows():
        t_id = str(trade.get('id', ''))
        sym = str(trade['symbol'])
        acc_name = str(trade.get('account_name', ''))
        direction = str(trade.get('direction', 'Long'))
        entry_price = float(trade['entry_price'])
        sl_price = float(trade['sl_price'])
        tp_price = float(trade.get('tp_price', 0.0)) if pd.notna(trade.get('tp_price')) else 0.0
        planned_risk_eur = float(trade.get('planned_risk_eur', 0.0)) if pd.notna(trade.get('planned_risk_eur')) else 0.0
        atr_days = float(trade.get('atr_days', 10.0)) if pd.notna(trade.get('atr_days')) else 10.0
        entry_date_str = str(trade['entry_date'])
        
        exit_profile = "prop_guard"
        for acc in config.get("lab_accounts", {}).values():
            if acc.get("name") == acc_name:
                exit_profile = acc.get("exit_profile", "prop_guard")
                break
                
        exit_mode = trade.get('exit_mode')
        if not exit_mode or str(exit_mode) == 'nan':
            if exit_profile == "apex_lock": exit_mode = "TARGET_LOCKED"
            elif exit_profile in ["prop_guard", "defensive_swing", "apex_commodity_scale", "commodity_scale"]: exit_mode = "PROP_DEFENSIVE"
            else: exit_mode = "HOME_RUN_TREND" if "Trend" in str(trade.get("setup_type", "")) else "ALPHA_CASHFLOW"
            
        curr_price = get_current_price(sym)
        if curr_price <= 0:
            continue
            
        dir_m = 1 if direction == "Long" else -1
        risk_per_share = abs(entry_price - sl_price)
        if risk_per_share <= 0:
            continue
            
        profit_per_share = (curr_price - entry_price) * dir_m
        current_r = profit_per_share / risk_per_share
        
        days_held = 0
        try:
            try: dt = datetime.datetime.strptime(entry_date_str, "%d/%m/%Y %H:%M")
            except: dt = datetime.datetime.strptime(entry_date_str, "%Y-%m-%d %H:%M")
            days_held = (datetime.datetime.now() - dt).days
        except Exception:
            pass
            
        # --- EOD-AUTO-CLOSE FÜR APEX-FUTURES DIREKT IM JOURNAL ---
        sym_upper = sym.upper()
        is_apex_acc = (str(exit_mode).upper() == "TARGET_LOCKED") or ("apex" in acc_name.lower()) or (exit_profile in ["apex_lock", "apex_commodity_scale"])
        is_cme_future = ("=F" in sym_upper) or any(f in sym_upper for f in ["CL=F", "MCL", "NG=F", "QG", "NQ=F", "ES=F", "YM=F", "RTY=F"])
        
        if is_apex_acc and is_cme_future:
            try:
                berlin_tz = zoneinfo.ZoneInfo("Europe/Berlin")
                now_dt = datetime.datetime.now(berlin_tz)
            except Exception:
                now_dt = datetime.datetime.now()
            now_time = now_dt.time()
            is_energy = any(e in sym_upper for e in ["CL=F", "MCL", "NG=F", "QG"])
            
            trigger_auto_close = False
            if is_energy and now_time >= datetime.time(20, 25):
                trigger_auto_close = True
            elif not is_energy and now_time >= datetime.time(22, 50):
                trigger_auto_close = True
                
            if trigger_auto_close:
                if 'signal_price' not in df_journal.columns:
                    df_journal['signal_price'] = df_journal['entry_price']
                qty = float(trade.get('position_size', 0.0)) if pd.notna(trade.get('position_size')) else 0.0
                pt_val = float(trade.get('point_value', 1.0)) if pd.notna(trade.get('point_value')) else 1.0
                fx = float(trade.get('fx_rate', 1.0)) if pd.notna(trade.get('fx_rate')) else 1.0
                est_fees = float(trade.get('est_fees_eur', 0.0)) if pd.notna(trade.get('est_fees_eur')) else 0.0
                
                raw_pnl = (curr_price - entry_price) * qty * pt_val * dir_m
                pnl_eur = (raw_pnl * fx) - est_fees
                pnl_pct = ((curr_price / entry_price) - 1.0) * 100.0 * dir_m if entry_price > 0 else 0.0
                r_mult = (pnl_eur / planned_risk_eur) if planned_risk_eur > 0 else current_r
                
                df_journal['exit_date'] = df_journal['exit_date'].astype(object)
                df_journal['exit_reason'] = df_journal['exit_reason'].astype(object)
                df_journal['status'] = df_journal['status'].astype(object)
                
                df_journal.at[idx, 'status'] = 'CLOSED'
                df_journal.at[idx, 'exit_price'] = round(curr_price, 4)
                df_journal.at[idx, 'exit_date'] = now_dt.strftime("%d/%m/%Y %H:%M")
                df_journal.at[idx, 'exit_reason'] = 'EOD_Close (Apex-Schutz)'
                df_journal.at[idx, 'pnl_eur'] = round(pnl_eur, 2)
                df_journal.at[idx, 'pnl_pct'] = round(pnl_pct, 2)
                df_journal.at[idx, 'r_multiple'] = round(r_mult, 2)
                journal_modified = True
                
                eod_auto_key = f"{t_id}_eod_auto_close"
                if not alert_state.get(eod_auto_key):
                    alerts.append((
                        eod_auto_key,
                        f"🛑 AUTO-CLOSE: Position {sym} wurde zum Schutz vor Übernacht-Regelbrüchen automatisch im Journal geschlossen!"
                    ))
                continue

        # 0) Exit-Erkennung (SL / TP)
        sl_hit_key = f"{t_id}_sl_hit"
        is_sl_hit = (curr_price <= sl_price) if direction == "Long" else (curr_price >= sl_price)
        if is_sl_hit and not alert_state.get(sl_hit_key):
            alerts.append((
                sl_hit_key,
                f"🛑 <b>STOP-LOSS GERISSEN: {sym} ({direction})</b>\n"
                f"• Konto: {acc_name}\n"
                f"• Kurs: {curr_price:.4f} | SL: {sl_price:.4f}\n"
                f"• Aktion: Position im Broker prüfen und im Journal schließen!"
            ))

        if tp_price > 0 and tp_price < 900000:
            tp_hit_key = f"{t_id}_tp_hit"
            is_tp_hit = (curr_price >= tp_price) if direction == "Long" else (curr_price <= tp_price)
            if is_tp_hit and not alert_state.get(tp_hit_key):
                alerts.append((
                    tp_hit_key,
                    f"🎯 <b>TAKE-PROFIT ERREICHT: {sym} ({direction})</b>\n"
                    f"• Konto: {acc_name}\n"
                    f"• Kurs: {curr_price:.4f} | TP: {tp_price:.4f}\n"
                    f"• Aktion: Gewinn im Broker realisieren und Trade im Journal schließen!"
                ))

        # a) Netto-Break-Even & Scale-Out (+1.0 R bzw. +1.5 R bei Krypto)
        is_crypto = any(ext in sym_upper for ext in ["-USD", "-EUR", "-GBP", "-USDT", "-CHF", "BTC", "ETH", "SOL", "NEAR"])
        be_key = f"{t_id}_be"
        if exit_mode == "DEFENSIVE_EMERGENCY":
            be_threshold = 0.75
        else:
            be_threshold = 1.5 if is_crypto else 1.0
        
        if current_r >= be_threshold and not alert_state.get(be_key):
            scale_pct = 50 if exit_profile in ["prop_guard", "defensive_swing", "apex_commodity_scale", "commodity_scale"] or is_crypto or exit_mode == "DEFENSIVE_EMERGENCY" else 30
            if exit_mode == "DEFENSIVE_EMERGENCY":
                alerts.append((
                    be_key,
                    f"🚨 <b>[DEFENSIVE-EMERGENCY] SLIPPAGE-SCHUTZ & FRÜHES NET-BE: {sym} ({direction})</b>\n"
                    f"• Konto: {acc_name} | Gewinn: +{current_r:.2f} R erreicht (Notfall-Schwelle: +0.75 R)!\n"
                    f"• Aktion: <b>50% Teilverkauf</b> & Stop-Loss sofort auf <b>Netto-Break-Even</b> ziehen (Schutz wegen schlechtem Einstiegs-CRV)!"
                ))
            elif exit_mode == "TARGET_LOCKED" and not is_crypto:
                alerts.append((
                    be_key,
                    f"⚡ <b>[TARGET-LOCKED] APEX TIGHTENING: {sym} ({direction})</b>\n"
                    f"• Konto: {acc_name} | Gewinn: +{current_r:.2f} R\n"
                    f"• Aktion: <b>Keine Teilschließung</b>, SL zieht aggressiv auf 80% des Buchgewinns nach, um Trailing-Drawdown zu schützen!"
                ))
            elif is_crypto:
                alerts.append((
                    be_key,
                    f"🪙 <b>KRYPTO SCALE-OUT & NET-BE: {sym} ({direction})</b>\n"
                    f"• Konto: {acc_name} | Gewinn: +{current_r:.2f} R erreicht!\n"
                    f"• Aktion: <b>{scale_pct}% Teilverkauf</b> empfohlen.\n"
                    f"• Info: SL jetzt auf Netto-Break-Even ziehen & 3.5x ATR Trail starten!"
                ))
            elif exit_mode == "PROP_DEFENSIVE":
                alerts.append((
                    be_key,
                    f"🛡️ <b>[PROP-DEFENSIVE] SCALE-OUT 50% & NET-BE: {sym} ({direction})</b>\n"
                    f"• Konto: {acc_name} | Gewinn: +{current_r:.2f} R erreicht!\n"
                    f"• Aktion: <b>50% Teilverkauf</b> empfohlen.\n"
                    f"• Info: Stop-Loss jetzt auf Netto-Break-Even nachziehen."
                ))
            else: # ALPHA_CASHFLOW & HOME_RUN_TREND
                if exit_mode == "HOME_RUN_TREND":
                    alerts.append((
                        be_key,
                        f"🚀 <b>[HOME-RUN-TREND] NET-BE ERREICHT: {sym} ({direction})</b>\n"
                        f"• Konto: {acc_name} | Gewinn: +{current_r:.2f} R erreicht!\n"
                        f"• Aktion: <b>Kein Teilverkauf</b> (Home-Run Modus).\n"
                        f"• Info: Stop-Loss jetzt auf Netto-Break-Even nachziehen."
                    ))
                else:
                    alerts.append((
                        be_key,
                        f"⚖️ <b>[ALPHA-CASHFLOW] SCALE-OUT 30% & NET-BE: {sym} ({direction})</b>\n"
                        f"• Konto: {acc_name} | Gewinn: +{current_r:.2f} R erreicht!\n"
                        f"• Aktion: <b>30% Teilverkauf</b> empfohlen.\n"
                        f"• Info: Stop-Loss jetzt auf Netto-Break-Even nachziehen."
                    ))

        # b) Trailing-Stop Nachzug (+1.5 R)
        trail_key = f"{t_id}_trail"
        if current_r >= 1.5 and not alert_state.get(trail_key):
            trail_mult = 2.0 if exit_mode == "PROP_DEFENSIVE" else 2.5
            if exit_mode == "DEFENSIVE_EMERGENCY": trail_mult = 1.5
            if exit_mode == "TARGET_LOCKED": trail_mult = 0.5
            if exit_mode == "HOME_RUN_TREND": trail_mult = 3.0
            if is_crypto and exit_mode != "DEFENSIVE_EMERGENCY": trail_mult = 3.5
            
            atr_proxy = (abs(tp_price - entry_price) / max(1.0, atr_days)) if (tp_price > 0 and tp_price < 900000) else (risk_per_share / 1.8)
            new_sl = curr_price - (trail_mult * atr_proxy) if direction == "Long" else curr_price + (trail_mult * atr_proxy)
            
            if exit_mode == "TARGET_LOCKED" and not is_crypto:
                peak_protection = entry_price + (profit_per_share * 0.80) * dir_m
                new_sl = max(new_sl, peak_protection) if direction == "Long" else min(new_sl, peak_protection)
                
            # Harter Monotonie-Schutz (mindestens Break-Even und niemals schlechter als Initial-SL)
            if direction == "Long":
                new_sl = max(new_sl, entry_price, sl_price)
                is_improved = new_sl > sl_price
            else:
                new_sl = min(new_sl, entry_price, sl_price)
                is_improved = new_sl < sl_price
                
            if is_improved:
                if exit_mode == "HOME_RUN_TREND":
                    alerts.append((
                        trail_key,
                        f"🚀 <b>[HOME-RUN-TREND] TRAILING-STOP AKTIV: {sym} ({direction})</b>\n"
                        f"• Konto: {acc_name} | Gewinn: +{current_r:.2f} R erreicht!\n"
                        f"• Empfohlener Trail-SL: <b>{new_sl:.4f}</b> (Faktor: {trail_mult}x ATR)"
                    ))
                else:
                    alerts.append((
                        trail_key,
                        f"📈 <b>TRAILING-STOP AKTIV: {sym} ({direction})</b>\n"
                        f"• Konto: {acc_name} | Modus: {str(exit_mode).replace('_', '-')}\n"
                        f"• Gewinn: +{current_r:.2f} R erreicht!\n"
                        f"• Empfohlener Trail-SL: <b>{new_sl:.4f}</b> (Faktor: {trail_mult}x ATR)"
                    ))

        # b2) Stufen-Chandelier (+2.5 R)
        r25_key = f"{t_id}_r25"
        if current_r >= 2.5 and not alert_state.get(r25_key):
            alerts.append((
                r25_key,
                f"🎯 <b>GEWINNSICHERUNG (+2.5 R): {sym} ({direction})</b>\n"
                f"• Konto: {acc_name} | Gewinn: +{current_r:.2f} R\n"
                f"• Aktion: Stop-Loss jetzt auf mindestens <b>+1.0 R Reingewinn</b> nachziehen!"
            ))

        # b2.5) Parabolik-Schutz / Climax-Trailing (+3.0 R)
        r30_key = f"{t_id}_r30"
        if current_r >= 3.0 and not alert_state.get(r30_key):
            alerts.append((
                r30_key,
                f"⚡ <b>PARABOLIK-SCHUTZ / CLIMAX (+3.0 R): {sym} ({direction})</b>\n"
                f"• Konto: {acc_name} | Gewinn: +{current_r:.2f} R\n"
                f"• Aktion: Parabolische Übertreibung absichern! Stop-Loss aggressiv auf mindestens +2.0 R nachziehen oder Bar-by-Bar eng am Vorperioden-Extremum trailen, um V-Reversals abzufangen."
            ))

        # b3) Traum-Profit Absicherung (+4.0 R)
        r40_key = f"{t_id}_r40"
        if current_r >= 4.0 and not alert_state.get(r40_key):
            alerts.append((
                r40_key,
                f"🔥 <b>TRAUM-PROFIT (+4.0 R): {sym} ({direction})</b>\n"
                f"• Konto: {acc_name} | Gewinn: +{current_r:.2f} R\n"
                f"• Aktion: Stop-Loss auf mindestens <b>+2.5 R</b> anheben (oder 3.0x ATR Trail)!"
            ))

        # b4) Maximal-Profit / Peak Protection (+6.0 R)
        r60_key = f"{t_id}_r60"
        if current_r >= 6.0 and not alert_state.get(r60_key):
            alerts.append((
                r60_key,
                f"👑 <b>MAXIMAL-PROFIT (+6.0 R): {sym} ({direction})</b>\n"
                f"• Konto: {acc_name} | Gewinn: +{current_r:.2f} R\n"
                f"• Aktion: 50% Peak-Protection aktiv! SL strikt auf die Hälfte des offenen Buchgewinns nachziehen!"
            ))

        # c) Time-Stop Warnung
        time_key = f"{t_id}_time"
        if atr_days > 0 and days_held > (atr_days * 1.5) and not alert_state.get(time_key):
            alerts.append((
                time_key,
                f"⏳ <b>TIME-STOP WARNUNG: {sym}</b>\n"
                f"• Konto: {acc_name}\n"
                f"• Haltedauer: {days_held} Tage (Erwartet: {int(atr_days)} Tage)\n"
                f"• Aktion: Trade zeigt keine Dynamik. Schließung prüfen!"
            ))

    if journal_modified:
        df_journal.to_csv(journal_file, index=False)

    return alerts, alert_state, alert_state_file
# RSI Screener (Aktuell stummgeschaltet)
def run_rsi_screener():
    items = load_tickers()

    if not items:
        print("⚠️ Keine Ticker in der Datei gefunden.")
        return

    print(f"🔍 Überprüfe {len(items)} Ticker auf RSI-Signale...\n")
    alerts = []
    table_rows = []
    tickers_only = [item[0] for item in items]

    try:
        batch_df = yf.download(tickers_only, period="2y", interval="1wk", group_by="ticker", progress=False)
        for ticker, name in items:
            try:
                df_single = batch_df.copy() if len(tickers_only) == 1 else (batch_df[ticker].copy() if ticker in batch_df.columns.levels[0] else pd.DataFrame())
                if df_single.empty or "Close" not in df_single: continue
                
                sym_u = str(ticker).upper()
                is_crypto = any(ext in sym_u for ext in ["-USD", "-EUR", "-GBP", "-USDT", "-CHF", "BTC", "ETH", "SOL", "NEAR"])
                if is_crypto:
                    tgt_low = 25.0
                    tgt_high = 75.0
                elif any(x in ticker for x in ["IQQQ", "6II0", "ISPY", "USPY", "PG", "XYL", "ABBN"]):
                    tgt_low = 35.0
                    tgt_high = 70.0
                else:
                    tgt_low = 30.0
                    tgt_high = 70.0
                    
                res = calc_rsi_and_targets(df_single, tgt_low, tgt_high)
                if not res: continue
                close, rsi, p30, p70 = res

                try: curr = yf.Ticker(ticker).fast_info.get("currency", "USD")
                except: curr = "USD"

                status = "NEUTRAL"
                if rsi <= tgt_low:
                    status = "🟢 KAUFEN"
                    tag = "🪙 KRYPTO" if is_crypto else "📈 AKTIE"
                    alerts.append(f"🟢 *KAUFSIGNAL ({tag}): {name} ({ticker})*\n• Aktueller Kurs: {close:.2f} {curr}\n• Weekly RSI: *{rsi:.1f}* (Grenze: ≤ {tgt_low})\n• Ziel (Kaufzone): {p30:.2f} {curr}\n")
                elif rsi >= tgt_high:
                    status = "🔴 VERKAUFEN"
                    alerts.append(f"🔴 *VERKAUFSIGNAL: {name} ({ticker})*\n• Aktueller Kurs: {close:.2f} {curr}\n• Weekly RSI: *{rsi:.1f}* (Grenze: ≥ {tgt_high})\n• Ziel (Verkauf): {p70:.2f} {curr}\n")

                table_rows.append({"Ticker": ticker, "Name": name[:18], "Kurs": f"{close:.2f} {curr}", "Weekly RSI": f"{rsi:.1f}", "Status": status})
            except Exception as e:
                print(f"⚠️ Fehler bei {ticker}: {e}")
    except Exception as e:
        print(f"❌ Batch-Download-Fehler: {e}")

    if table_rows:
        print("=" * 65 + "\n📊 RSI BOT - ÜBERSICHT DER GEPRÜFTEN WERTE\n" + "=" * 65)
        print(pd.DataFrame(table_rows).to_string(index=False))
        print("=" * 65)

    if alerts:
        send_telegram_message("🚨 *RSI ALARM BOT STATUS UPDATE* 🚨\n\n" + "\n---\n".join(alerts))


# Hauptfunktion: Kontinuierlicher Trade-Copilot
def main():
    print(f"🤖 Multi-Tenant Trade-Copilot gestartet (Polling: {POLLING_INTERVAL}s).")
    
    try:
        while True:
            if not os.path.exists(USERS_DIR):
                print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] USERS_DIR nicht gefunden. Warte...")
            else:
                users_checked = 0
                alerts_sent = 0
                
                for user_folder in os.listdir(USERS_DIR):
                    user_dir = os.path.join(USERS_DIR, user_folder)
                    if os.path.isdir(user_dir):
                        users_checked += 1
                        profile_file = os.path.join(user_dir, "user_profile.json")
                        
                        chat_id = ""
                        if os.path.exists(profile_file):
                            with open(profile_file, "r", encoding="utf-8") as pf:
                                chat_id = json.load(pf).get("telegram_chat_id", "")
                                
                        copilot_alerts, alert_state, alert_state_file = check_open_trades(user_dir)
                        if copilot_alerts and chat_id:
                            successful_keys = []
                            for state_key, msg_text in copilot_alerts:
                                full_msg = f"🤖 <b>TRADE COPILOT ALARM ({user_folder.upper()})</b> 🤖\n\n{msg_text}"
                                if send_telegram_message(full_msg, chat_id):
                                    successful_keys.append(state_key)
                                    alerts_sent += 1
                            
                            # Erst NACH erfolgreichem Telegram-Versand den State atomar persistieren
                            if successful_keys:
                                for k in successful_keys:
                                    alert_state[k] = True
                                with open(alert_state_file, "w", encoding="utf-8") as f:
                                    json.dump(alert_state, f, indent=4)
                                
                print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] {users_checked} Mandanten geprüft. {alerts_sent} Alarme gesendet.")
                    
            time.sleep(POLLING_INTERVAL)
            
    except KeyboardInterrupt:
        print("\n🛑 Trade-Copilot sauber beendet (Strg+C).")

if __name__ == "__main__":
    main()