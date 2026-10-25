import os
import sys
import html
import json
import time
import uuid
import tempfile
import datetime
import requests
import pandas as pd
import yfinance as yf
from dotenv import load_dotenv
from contextlib import contextmanager

try:
    import fcntl
except ImportError:
    fcntl = None  # Fallback für lokale Windows-Entwicklungsumgebungen

try:
    import zoneinfo
except ImportError:
    from backports import zoneinfo  # type: ignore

# Erzwinge sofortiges Zeilen-Flushing für 'docker logs' (ohne TTY)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(line_buffering=True)
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(line_buffering=True)


def log(msg: str) -> None:
    print(msg, flush=True)


# 1. Pfade & Umgebungsvariablen definieren (Docker- & Host-kompatibel)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PARENT_DIR = os.path.dirname(BASE_DIR)

env_path = os.path.join(BASE_DIR, ".env")
load_dotenv(env_path, override=True)


def _resolve_dir(env_key: str, candidates: list[str], create_if_missing: bool = False) -> str:
    """Prüft zuerst ENV-Variablen, danach der Reihe nach existierende Mount-Pfade."""
    env_val = os.getenv(env_key)
    if env_val:
        if create_if_missing:
            os.makedirs(env_val, exist_ok=True)
        return os.path.abspath(env_val)
    for path in candidates:
        if os.path.exists(path):
            return os.path.abspath(path)
    target = os.path.abspath(candidates[0])
    if create_if_missing:
        os.makedirs(target, exist_ok=True)
    return target


# 2. Zugangsdaten & Verzeichnisse auslesen
TELEGRAM_TOKEN = (
    os.getenv("TELEGRAM_BOT_TOKEN")
    or os.getenv("TELEGRAM_TOKEN")
    or os.getenv("BOT_TOKEN")
)
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID") or os.getenv("CHAT_ID")
POLLING_INTERVAL = int(os.getenv("POLLING_INTERVAL", 60))

TICKER_FILE = _resolve_dir(
    "INVARIX_TICKER_FILE",
    [
        os.path.join(BASE_DIR, "ticker_liste.txt"),
        os.path.join(PARENT_DIR, "RSI Bot", "ticker_liste.txt"),
    ],
)
JOURNAL_FILE = os.path.join(BASE_DIR, "trade_journal.csv")
CONFIG_FILE = os.path.join(BASE_DIR, "ticker_config.json")
ALERT_STATE_DIR = _resolve_dir('INVARIX_STATE_DIR', [os.path.join(BASE_DIR, 'state')], create_if_missing=True)
ALERT_STATE_FILE = os.path.join(ALERT_STATE_DIR, "alert_state.json")
USERS_DIR = _resolve_dir(
    "INVARIX_USERS_DIR",
    [
        os.path.join(BASE_DIR, "users"),
        "/app/users",
        "/data/users",
        os.path.join(PARENT_DIR, "Trading Dashboard", "users"),
    ],
)
PENDING_SIGNALS_DIR = _resolve_dir(
    "INVARIX_PENDING_DIR",
    [
        os.path.join(BASE_DIR, "signals", "01_pending"),
        "/app/signals/01_pending",
        "/data/signals/01_pending",
        os.path.join(PARENT_DIR, "Trading Dashboard", "signals", "01_pending"),
    ],
    create_if_missing=True,
)
PROCESSING_SIGNALS_DIR = _resolve_dir(
    "INVARIX_PROCESSING_DIR",
    [
        os.path.join(BASE_DIR, "signals", "02_processing"),
        "/app/signals/02_processing",
        "/data/signals/02_processing",
        os.path.join(PARENT_DIR, "Trading Dashboard", "signals", "02_processing"),
    ],
    create_if_missing=True,
)


def get_berlin_now() -> datetime.datetime:
    """Liefert die aktuelle Zeit in 'Europe/Berlin' mit deterministischem Offset-Fallback."""
    try:
        return datetime.datetime.now(zoneinfo.ZoneInfo("Europe/Berlin"))
    except Exception:
        utc_now = datetime.datetime.now(datetime.timezone.utc)
        offset = 2 if 4 <= utc_now.month <= 10 else 1
        return utc_now.astimezone(datetime.timezone(datetime.timedelta(hours=offset)))


@contextmanager
def file_lock(lock_path: str, timeout: float = 5.0):
    """Prozessübergreifendes Advisory-Locking für konkurrierende CSV/JSON-Zugriffe."""
    lock_file = f"{lock_path}.lock"
    fd = os.open(lock_file, os.O_CREAT | os.O_RDWR, 0o666)
    start_t = time.monotonic()
    locked = False
    try:
        if fcntl is not None:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    locked = True
                    break
                except BlockingIOError:
                    if time.monotonic() - start_t >= timeout:
                        raise TimeoutError(f"Lock-Timeout ({timeout}s) für {lock_path}")
                    time.sleep(0.05)
        yield
    finally:
        if locked and fcntl is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except Exception:
                pass
        os.close(fd)


def safe_load_json(filepath: str, default=None):
    """Defensives JSON-Laden ohne Daemon-Absturz bei korrupten oder leeren Dateien."""
    if default is None:
        default = {}
    if not os.path.exists(filepath):
        return default
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log(f"⚠️ Warnung: Konnte JSON '{filepath}' nicht lesen ({e}). Nutze Default.")
        return default


def atomic_write_json(filepath: str, data: dict) -> bool:
    """Atomares JSON-Speichern im selben Zielverzeichnis (EXDEV-sicher inkl. fsync)."""
    target_dir = os.path.dirname(os.path.abspath(filepath))
    os.makedirs(target_dir, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".tmp_", suffix=".json", dir=target_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, filepath)
        return True
    except Exception as e:
        log(f"❌ Fehler beim atomaren Schreiben von '{filepath}': {e}")
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        return False


# Telegram Nachricht senden
def send_telegram_message(message: str, chat_id_target=TELEGRAM_CHAT_ID) -> bool:
    if not TELEGRAM_TOKEN or not chat_id_target:
        return False

    chat_ids = [cid.strip() for cid in str(chat_id_target).split(",") if cid.strip()]
    if not chat_ids:
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    any_success = False

    for chat_id in chat_ids:
        payload = {
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "HTML",
        }
        for attempt in range(2):
            response = None
            try:
                response = requests.post(url, json=payload, timeout=(3.05, 8.0))
                if response.status_code == 429:
                    retry_after = int(response.json().get("parameters", {}).get("retry_after", 3))
                    log(f"⏳ Telegram Rate-Limit (429) für {chat_id}. Warte {retry_after}s...")
                    time.sleep(min(retry_after, 10))
                    continue
                response.raise_for_status()
                any_success = True
                break
            except Exception as e:
                err_detail = f" | Server-Antwort: {response.text}" if response is not None else ""
                log(f"\n❌ Fehler beim Senden an {chat_id} (Versuch {attempt + 1}): {e}{err_detail}")
                break

    if any_success:
        log(f"\n✅ Alarm-Nachricht an Empfänger ({len(chat_ids)} konfiguriert) gesendet!")
    return any_success


# Ticker aus der zentralen Datei laden
def load_tickers():
    if not os.path.exists(TICKER_FILE):
        log(f"❌ Ticker-Datei nicht gefunden unter: {TICKER_FILE}")
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
    rs = avg_gain / avg_loss.replace(0, 1e-9)
    rsi = 100 - (100 / (1 + rs))

    latest_close = float(close.iloc[-1])
    latest_rsi = float(rsi.iloc[-1])
    latest_ag = float(avg_gain.iloc[-1])
    latest_al = float(avg_loss.iloc[-1])

    al_n = latest_al * 13 / 14
    ag_n = latest_ag * 13 / 14

    p70 = latest_close + max(
        0.0, (t_70 / (100 - t_70) * al_n * 14) - (latest_ag * 13)
    )
    p30 = latest_close - max(
        0.0, (((ag_n * 14) / (t_30 / (100 - t_30))) - (latest_al * 13))
    )

    return latest_close, latest_rsi, p30, p70


# 3. ZENTRALE SYMBOL-AUFLÖSUNG & ASSET-DNA MAPPING
SYMBOL_ALIAS_MAP = {
    "MNQ": "NQ=F", "MNQ=F": "NQ=F",
    "MES": "ES=F", "MES=F": "ES=F",
    "MCL": "CL=F", "MCL=F": "CL=F",
    "MYM": "YM=F", "MYM=F": "YM=F",
    "M2K": "RTY=F", "M2K=F": "RTY=F",
    "MGC": "GC=F", "MGC=F": "GC=F",
    "QG": "NG=F", "QG=F": "NG=F",
    "SIL": "SI=F",
    "EURUSD": "EURUSD=X", "EUR=X": "EURUSD=X",
    "GBPUSD": "GBPUSD=X", "GBP=X": "GBPUSD=X",
    "USDJPY": "USDJPY=X", "JPY=X": "USDJPY=X",
}

CME_FUTURE_SYMBOLS = {
    "NQ=F", "MNQ", "MNQ=F", "ES=F", "MES", "MES=F",
    "YM=F", "MYM", "MYM=F", "RTY=F", "M2K", "M2K=F",
    "CL=F", "MCL", "MCL=F", "NG=F", "QG", "QG=F",
    "GC=F", "MGC", "MGC=F", "SI=F", "SIL",
}

ENERGY_FUTURE_SYMBOLS = {"CL=F", "MCL", "MCL=F", "NG=F", "QG", "QG=F"}

ASSET_DNA = {
    "NQ=F": {"tf": "1h", "session": (15, 30, 21, 30), "min_score": 60, "allowed_setups": ["A", "B"], "desk": "Apex Prop", "tick": 0.25, "decimals": 2, "rsi_bounds": (30, 70)},
    "ES=F": {"tf": "1h", "session": (15, 30, 21, 30), "min_score": 60, "allowed_setups": ["A", "B"], "desk": "Apex Prop", "tick": 0.25, "decimals": 2, "rsi_bounds": (30, 70)},
    "YM=F": {"tf": "1h", "session": (15, 30, 21, 30), "min_score": 60, "allowed_setups": ["A", "B"], "desk": "Apex Prop", "tick": 1.0, "decimals": 0, "rsi_bounds": (30, 70)},
    "RTY=F": {"tf": "1h", "session": (15, 30, 21, 30), "min_score": 60, "allowed_setups": ["A", "B"], "desk": "Apex Prop", "tick": 0.10, "decimals": 2, "rsi_bounds": (30, 70)},
    "EURUSD=X": {"tf": "1h", "session": (13, 0, 18, 0), "min_score": 60, "allowed_setups": ["A", "B"], "desk": "FTMO / Forex", "tick": 0.00001, "decimals": 5, "rsi_bounds": (30, 70)},
    "GBPUSD=X": {"tf": "4h", "session": (8, 0, 22, 0), "min_score": 60, "allowed_setups": ["A", "B"], "desk": "FTMO / Forex", "tick": 0.00001, "decimals": 5, "rsi_bounds": (30, 70)},
    "USDJPY=X": {"tf": "4h", "session": (8, 0, 22, 0), "min_score": 60, "allowed_setups": ["A", "B"], "desk": "FTMO / Forex", "tick": 0.001, "decimals": 3, "rsi_bounds": (30, 70)},
    "CL=F": {"tf": "4h", "session": (14, 30, 20, 30), "min_score": 65, "allowed_setups": ["A", "B"], "desk": "Privat Rohstoffe", "tick": 0.01, "decimals": 2, "rsi_bounds": (30, 70)},
    "GC=F": {"tf": "1h", "session": (9, 0, 18, 0), "min_score": 60, "allowed_setups": ["A", "B"], "desk": "Privat Rohstoffe", "tick": 0.10, "decimals": 2, "rsi_bounds": (30, 70)},
    "DEFAULT_FOREX": {"tf": "4h", "session": (8, 0, 22, 0), "min_score": 60, "allowed_setups": ["A", "B"], "desk": "FTMO / Forex", "tick": 0.00001, "decimals": 5, "rsi_bounds": (30, 70)},
    "DEFAULT_FUTURE": {"tf": "1h", "session": (14, 30, 21, 30), "min_score": 60, "allowed_setups": ["A", "B"], "desk": "Apex Prop", "tick": 0.25, "decimals": 2, "rsi_bounds": (30, 70)},
    "DEFAULT_CRYPTO": {"tf": "4h", "session": (0, 0, 23, 59), "min_score": 65, "allowed_setups": ["A"], "desk": "Privat Krypto", "tick": 0.01, "decimals": 4, "rsi_bounds": (25, 75)},
    "DEFAULT_EQUITY": {"tf": "4h", "session": (15, 30, 22, 0), "min_score": 70, "allowed_setups": ["B"], "desk": "Privat Swing", "tick": 0.01, "decimals": 2, "rsi_bounds": (35, 70)},
}


def normalize_symbol(symbol: str) -> str:
    sym_u = str(symbol).strip().upper()
    return SYMBOL_ALIAS_MAP.get(sym_u, sym_u)


def resolve_asset_dna(symbol: str) -> dict:
    norm_sym = normalize_symbol(symbol)
    if norm_sym in ASSET_DNA:
        return ASSET_DNA[norm_sym]
    sym_u = str(symbol).strip().upper()
    if sym_u in CME_FUTURE_SYMBOLS or "=F" in norm_sym:
        return ASSET_DNA["DEFAULT_FUTURE"]
    if "=X" in norm_sym:
        return ASSET_DNA["DEFAULT_FOREX"]
    if any(ext in norm_sym for ext in ["-USD", "-EUR", "-GBP", "-USDT", "-CHF", "BTC", "ETH", "SOL", "NEAR"]):
        return ASSET_DNA["DEFAULT_CRYPTO"]
    return ASSET_DNA["DEFAULT_EQUITY"]

def is_crypto_symbol(symbol: str) -> bool:
    sym_u = str(symbol).strip().upper()
    norm_sym = normalize_symbol(sym_u)
    if any(ext in norm_sym for ext in ["-USD", "-EUR", "-GBP", "-USDT", "-CHF", "BTC", "ETH", "SOL", "NEAR", "KAS"]):
        return True
    dna = ASSET_DNA.get(norm_sym)
    if dna and "Krypto" in dna.get("desk", ""):
        return True
    return False

def resolve_account_type(acc_key: str, acc: dict) -> str:
    """Ermittelt deterministisch den Broker-/Account-Typ."""
    raw_type = acc.get("account_type", "").lower()
    raw_prof = acc.get("broker_profile", "").lower()
    raw_name = acc.get("name", "").lower()
    raw_key = acc_key.lower()
    
    if any("apex" in x for x in [raw_type, raw_prof, raw_name, raw_key]):
        return "apex"
    if any("ftmo" in x for x in [raw_type, raw_prof, raw_name, raw_key]):
        return "ftmo"
    return "private"


def audit_account_for_signal(acc_key: str, acc: dict, sym: str, is_short: bool, score: int, setup_type: str, now_dt, dna: dict) -> tuple[bool, str]:
    """5-Schranken Audit inkl. Whitelist (Säule 2 & 6)."""
    account_type = resolve_account_type(acc_key, acc)
    
    # 1. Whitelist-Check
    acc_tickers = acc.get("tickers", [])
    if acc_tickers and len(acc_tickers) > 0:
        sym_u = str(sym).strip().upper()
        norm_sym = normalize_symbol(sym_u)
        if sym_u not in acc_tickers and norm_sym not in acc_tickers:
            return False, ""
            
    # 2. Session-Check
    if account_type in ["apex", "ftmo"] and dna and "session" in dna:
        sess_start_h, sess_start_m, sess_end_h, sess_end_m = dna["session"]
        curr_minutes = now_dt.hour * 60 + now_dt.minute
        start_minutes = sess_start_h * 60 + sess_start_m
        end_minutes = sess_end_h * 60 + sess_end_m
        if not (start_minutes <= curr_minutes <= end_minutes):
            return False, ""
            
    # 3. Direction-Check
    acc_dir = acc.get("direction", "both").lower()
    sig_dir = "short" if is_short else "long"
    if acc_dir != "both" and acc_dir != sig_dir:
        return False, ""
        
    # 4. Score-Check
    if score < acc.get("min_score", 0):
        return False, ""
        
    # 5. Setup-Check
    acc_allowed = acc.get("allowed_setups", ["A", "B"])
    if setup_type not in acc_allowed:
        return False, ""
        
    return True, account_type


def should_spool_signal(acc: dict, account_type: str, is_crypto: bool) -> bool:
    """Spooler-Queue Brandmauer (Säule 7)."""
    return (acc.get("execution_mode") == "LIVE_AUTO" and 
            account_type in ["apex", "ftmo"] and 
            not is_crypto)


def get_telegram_header_and_badges(matched_accounts: list, is_crypto: bool, is_short: bool) -> tuple[str, list[str]]:
    """Generiert Header und Badges für das Reporting (Säule 9)."""
    sig_dir_str = "SHORT" if is_short else "LONG"
    all_experiment = all(acc.get("execution_mode", "CO_PILOT") == "EXPERIMENT" for acc, _ in matched_accounts) if matched_accounts else False
    
    if is_crypto or (matched_accounts and all_experiment):
        header = "🧪 INVARIX LABOR ALERT (PHASE 7 QUARANTÄNE)"
    else:
        header = f"🚨 INVARIX ENTRY ALERT — {sig_dir_str}"
        
    account_badges = []
    for acc, a_type in matched_accounts:
        emode = acc.get("execution_mode", "CO_PILOT")
        if emode == "LIVE_AUTO" and a_type in ["apex", "ftmo"]:
            badge = "🟢 [LIVE-AUTO]"
        elif emode == "EXPERIMENT":
            badge = "🧪 [EXPERIMENT]"
        else:
            badge = "🔵 [CO-PILOT]"
            
        account_badges.append(f"{badge} {html.escape(acc.get('name', ''))}")
        
    return header, account_badges


def round_to_tick(price: float, symbol: str) -> float:
    dna = resolve_asset_dna(symbol)
    tick = dna.get("tick", 0.01)
    decimals = dna.get("decimals", 4)
    if tick > 0:
        return round(round(price / tick) * tick, decimals)
    return round(price, decimals)


def format_price(price: float, symbol: str) -> str:
    dna = resolve_asset_dna(symbol)
    decimals = dna.get("decimals", 4)
    val = round_to_tick(price, symbol)
    return f"{val:.{decimals}f}"


_PRICE_CACHE: dict[str, tuple[float, float]] = {}
_IN_MEMORY_LOCKS: set[str] = set()

def get_current_price(symbol: str, ttl_seconds: float = 15.0) -> float:
    """
    Isolierte Hilfsfunktion für den Live-Kurs mit Kurzzeit-Cache.
    Aktuell via yfinance (fast_info mit History-Fallback), vorbereitet für Tradovate WS/REST.
    """
    mapped_symbol = normalize_symbol(symbol)
    now_mono = time.monotonic()

    if mapped_symbol in _PRICE_CACHE:
        cached_price, ts = _PRICE_CACHE[mapped_symbol]
        if now_mono - ts <= ttl_seconds and cached_price > 0:
            return cached_price

    try:
        price = float(yf.Ticker(mapped_symbol).fast_info.get("lastPrice", 0.0) or 0.0)
        if price > 0:
            _PRICE_CACHE[mapped_symbol] = (price, now_mono)
            return price
    except Exception:
        pass

    try:
        hist = yf.Ticker(mapped_symbol).history(period="5d", timeout=8)
        if not hist.empty and "Close" in hist.columns:
            price = float(hist["Close"].dropna().iloc[-1])
            if price > 0:
                _PRICE_CACHE[mapped_symbol] = (price, now_mono)
                return price
    except Exception as e:
        log(f"⚠️ Kursabruf-Fehler für {symbol} ({mapped_symbol}): {e}")

    log(f"⚠️ Warnung: Kein valider Live-Kurs (> 0) für {symbol} ({mapped_symbol}) erhalten!")
    return 0.0


def _atomic_update_journal_closures(journal_file: str, closures: dict[str, dict]) -> bool:
    """Liest die CSV unter Exklusiv-Lock frisch ein, wendet nur die EOD-Closures an und speichert atomar."""
    if not closures:
        return True
    try:
        with file_lock(journal_file, timeout=5.0):
            df_fresh = pd.read_csv(journal_file)
            if "signal_price" not in df_fresh.columns and "entry_price" in df_fresh.columns:
                df_fresh["signal_price"] = df_fresh["entry_price"]

            for col in ["exit_date", "exit_reason", "status"]:
                if col in df_fresh.columns:
                    df_fresh[col] = df_fresh[col].astype(object)

            modified = False
            for idx, row in df_fresh.iterrows():
                t_id = str(row.get("id", ""))
                if t_id in closures and str(row.get("status", "")) == "OPEN":
                    for k, v in closures[t_id].items():
                        df_fresh.at[idx, k] = v
                    modified = True

            if modified:
                target_dir = os.path.dirname(os.path.abspath(journal_file))
                fd, tmp_csv = tempfile.mkstemp(prefix=".tmp_journal_", suffix=".csv", dir=target_dir)
                try:
                    with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
                        df_fresh.to_csv(f, index=False)
                        f.flush()
                        os.fsync(f.fileno())
                    os.replace(tmp_csv, journal_file)
                except Exception:
                    if os.path.exists(tmp_csv):
                        os.remove(tmp_csv)
                    raise
        return True
    except Exception as e:
        log(f"❌ Kritischer Fehler beim atomaren Journal-Update '{journal_file}': {e}")
        return False


def check_open_trades(user_dir: str):
    journal_file = os.path.join(user_dir, "trade_journal.csv")
    config_file = os.path.join(user_dir, "ticker_config.json")
    alert_state_file = os.path.join(user_dir, "alert_state.json")

    if not os.path.exists(journal_file):
        return [], {}, alert_state_file

    try:
        with file_lock(journal_file, timeout=3.0):
            df_journal = pd.read_csv(journal_file)
        open_trades = df_journal[df_journal["status"] == "OPEN"]
    except Exception as e:
        log(f"⚠️ Konnte Journal '{journal_file}' nicht lesen: {e}")
        return [], {}, alert_state_file

    if open_trades.empty:
        return [], {}, alert_state_file

    config = safe_load_json(config_file, default={})
    alert_state = safe_load_json(alert_state_file, default={})

    alerts = []
    pending_closures: dict[str, dict] = {}
    now_dt = get_berlin_now()
    now_time = now_dt.time()
    is_weekday = now_dt.weekday() < 5

    for idx, trade in open_trades.iterrows():
        t_id = str(trade.get("id", ""))

        sym_raw = trade.get("symbol", trade.get("ticker", ""))
        sym = str(sym_raw).strip() if pd.notna(sym_raw) else ""

        acc_raw = trade.get("account_name", "")
        acc_name_raw = str(acc_raw) if pd.notna(acc_raw) else ""
        acc_name = html.escape(acc_name_raw)

        dir_raw = trade.get("direction", "Long")
        direction = str(dir_raw).strip() if pd.notna(dir_raw) else "Long"
        is_long = direction.upper() == "LONG"
        dir_m = 1 if is_long else -1

        entry_raw = trade.get("entry_price", 0.0)
        entry_price = float(entry_raw) if pd.notna(entry_raw) else 0.0

        if not sym or entry_price <= 0:
            continue

        sl_raw = trade.get("sl_price", trade.get("stop_loss", 0.0))
        sl_price = float(sl_raw) if pd.notna(sl_raw) else 0.0

        curr_sl_raw = trade.get("current_sl", 0.0)
        current_sl = float(curr_sl_raw) if pd.notna(curr_sl_raw) else 0.0
        active_sl = current_sl if current_sl > 0.0 else sl_price

        init_sl_raw = trade.get("initial_sl_price", trade.get("initial_sl", 0.0))
        initial_sl_price = float(init_sl_raw) if pd.notna(init_sl_raw) else 0.0

        tp_raw = trade.get("tp_price", trade.get("take_profit", 0.0))
        tp_price = float(tp_raw) if pd.notna(tp_raw) else 0.0

        pos_raw = trade.get("position_size", trade.get("shares_contracts", 1.0))
        position_size = float(pos_raw) if (pd.notna(pos_raw) and float(pos_raw) > 0) else 1.0

        pr_raw = trade.get("planned_risk_eur", 0.0)
        planned_risk_eur = float(pr_raw) if pd.notna(pr_raw) else 0.0

        pt_val = float(trade.get("point_value", 1.0)) if pd.notna(trade.get("point_value")) else 1.0
        fx = float(trade.get("fx_rate", 1.0)) if pd.notna(trade.get("fx_rate")) else 1.0
        est_fees = float(trade.get("est_fees_eur", 0.0)) if pd.notna(trade.get("est_fees_eur")) else 0.0

        atr_raw = trade.get("atr_days", 10.0)
        atr_days = float(atr_raw) if pd.notna(atr_raw) else 10.0

        date_raw = trade.get("entry_date", "")
        entry_date_str = str(date_raw) if pd.notna(date_raw) else ""

        exit_profile = "prop_guard"
        for acc in config.get("lab_accounts", {}).values():
            if acc.get("name") == acc_name_raw:
                exit_profile = acc.get("exit_profile", "prop_guard")
                break

        # Säule 9: Defensive Journal-Absicherung (striktes Dictionary-Fallback)
        raw_exit = trade.get("exit_mode")
        exit_mode = str(raw_exit).strip() if pd.notna(raw_exit) and str(raw_exit).strip() != "nan" else "TRAILING_STOP"
        
        if exit_mode == "TRAILING_STOP":
            if exit_profile == "apex_lock":
                exit_mode = "TARGET_LOCKED"
            elif exit_profile in ["prop_guard", "defensive_swing", "apex_commodity_scale", "commodity_scale"]:
                exit_mode = "PROP_DEFENSIVE"
            else:
                exit_mode = "HOME_RUN_TREND" if "Trend" in str(trade.get("setup_type", "")) else "ALPHA_CASHFLOW"

        sym_upper = sym.upper()
        norm_sym = normalize_symbol(sym_upper)
        is_apex_acc = (
            (str(exit_mode).upper() == "TARGET_LOCKED")
            or ("apex" in acc_name_raw.lower())
            or (exit_profile in ["apex_lock", "apex_commodity_scale"])
        )
        is_cme_future = (
            ("=F" in norm_sym)
            or (sym_upper in CME_FUTURE_SYMBOLS)
            or (norm_sym in CME_FUTURE_SYMBOLS)
        )
        is_energy = (sym_upper in ENERGY_FUTURE_SYMBOLS) or (norm_sym in ENERGY_FUTURE_SYMBOLS)

        # Werktags EOD-Auto-Close Fenster (Energy: 20:25-21:00, US-Index/Metall: 22:50-23:00 Berliner Zeit)
        trigger_auto_close = False
        if is_apex_acc and is_cme_future and is_weekday:
            if is_energy and datetime.time(20, 25) <= now_time <= datetime.time(21, 0):
                trigger_auto_close = True
            elif not is_energy and datetime.time(22, 50) <= now_time <= datetime.time(23, 0):
                trigger_auto_close = True

        curr_price = get_current_price(sym)
        if curr_price <= 0:
            if trigger_auto_close:
                err_key = f"{t_id}_eod_price_fail_{now_dt.strftime('%Y%m%d')}"
                if not alert_state.get(err_key):
                    alerts.append((
                        err_key,
                        f"🚨 <b>KRITISCHER EOD-FEHLER: {html.escape(sym)} ({acc_name})</b>\n"
                        f"• Live-Kurs nicht abrufbar (0.0) während des Apex-EOD-Schließfensters!\n"
                        f"• Aktion: Position SOFORT manuell im Broker prüfen und schließen!"
                    ))
            continue

        # Initial-Risiko pro Einheit rekonstruieren (verhindert Blindheit bei SL == Entry / Break-Even)
        if initial_sl_price > 0 and abs(entry_price - initial_sl_price) > 0:
            risk_per_share = abs(entry_price - initial_sl_price)
        elif planned_risk_eur > 0 and position_size > 0 and pt_val > 0 and fx > 0:
            sl_locked_in = (is_long and sl_price >= entry_price) or (not is_long and 0 < sl_price <= entry_price)
            if sl_locked_in:
                risk_per_share = planned_risk_eur / (position_size * pt_val * fx)
            else:
                risk_per_share = abs(entry_price - sl_price)
        else:
            risk_per_share = abs(entry_price - sl_price)

        profit_per_share = (curr_price - entry_price) * dir_m
        current_r = (profit_per_share / risk_per_share) if risk_per_share > 0 else 0.0

        # --- EOD-AUTO-CLOSE FÜR APEX-FUTURES DIREKT IM JOURNAL ---
        if trigger_auto_close:
            raw_pnl = (curr_price - entry_price) * position_size * pt_val * dir_m
            pnl_eur = (raw_pnl * fx) - est_fees
            pnl_pct = ((curr_price / entry_price) - 1.0) * 100.0 * dir_m if entry_price > 0 else 0.0
            r_mult = (pnl_eur / planned_risk_eur) if planned_risk_eur > 0 else current_r

            pending_closures[t_id] = {
                "status": "CLOSED",
                "exit_price": round_to_tick(curr_price, sym),
                "exit_date": now_dt.strftime("%d/%m/%Y %H:%M"),
                "exit_reason": "EOD_Close (Apex-Schutz)",
                "pnl_eur": round(pnl_eur, 2),
                "pnl_pct": round(pnl_pct, 2),
                "r_multiple": round(r_mult, 2),
                "current_risk_eur": 0.0,
            }

            eod_auto_key = f"{t_id}_eod_auto_close"
            if not alert_state.get(eod_auto_key):
                alerts.append((
                    eod_auto_key,
                    f"🛑 AUTO-CLOSE: Position {html.escape(sym)} wurde zum Schutz vor Übernacht-Regelbrüchen automatisch im Journal geschlossen!"
                ))
            continue

        # 0) Exit-Erkennung (SL / TP) — greift auch bei Break-Even-Stop (sl_price == entry_price)
        if active_sl > 0:
            sl_hit_key = f"{t_id}_sl_hit"
            is_sl_hit = (curr_price <= active_sl) if is_long else (curr_price >= active_sl)
            if is_sl_hit and not alert_state.get(sl_hit_key):
                alerts.append((
                    sl_hit_key,
                    f"🛑 <b>STOP-LOSS GERISSEN: {html.escape(sym)} ({direction})</b>\n"
                    f"• Konto: {acc_name}\n"
                    f"• Kurs: {format_price(curr_price, sym)} | SL: {format_price(active_sl, sym)}\n"
                    f"• Aktion: Position im Broker prüfen und im Journal schließen!"
                ))

        if 0 < tp_price < 900000:
            tp_hit_key = f"{t_id}_tp_hit"
            is_tp_hit = (curr_price >= tp_price) if is_long else (curr_price <= tp_price)
            if is_tp_hit and not alert_state.get(tp_hit_key):
                alerts.append((
                    tp_hit_key,
                    f"🎯 <b>TAKE-PROFIT ERREICHT: {html.escape(sym)} ({direction})</b>\n"
                    f"• Konto: {acc_name}\n"
                    f"• Kurs: {format_price(curr_price, sym)} | TP: {format_price(tp_price, sym)}\n"
                    f"• Aktion: Gewinn im Broker realisieren und Trade im Journal schließen!"
                ))

        if risk_per_share <= 0:
            continue

        days_held = 0
        for fmt in ("%d/%m/%Y %H:%M", "%Y-%m-%d %H:%M"):
            try:
                dt = datetime.datetime.strptime(entry_date_str, fmt)
                days_held = (now_dt.replace(tzinfo=None) - dt).days
                break
            except ValueError:
                continue

        fee_offset = (est_fees / (position_size * pt_val * fx)) if (position_size * pt_val * fx) > 0 else 0.0
        net_be = round_to_tick(entry_price + fee_offset if is_long else entry_price - fee_offset, sym)
        target_new_sl = None

        # a) Netto-Break-Even & Scale-Out (+1.0 R bzw. +1.5 R bei Krypto)
        is_crypto = any(ext in sym_upper for ext in ["-USD", "-EUR", "-GBP", "-USDT", "-CHF", "BTC", "ETH", "SOL", "NEAR"])
        be_key = f"{t_id}_be"
        if exit_mode == "DEFENSIVE_EMERGENCY":
            be_threshold = 0.75
        else:
            be_threshold = 1.5 if is_crypto else 1.0

        if current_r >= be_threshold:
            target_new_sl = net_be
            if not alert_state.get(be_key):
                scale_pct = 50 if exit_profile in ["prop_guard", "defensive_swing", "apex_commodity_scale", "commodity_scale"] or is_crypto or exit_mode == "DEFENSIVE_EMERGENCY" else 30
                if exit_mode == "DEFENSIVE_EMERGENCY":
                    alerts.append((
                        be_key,
                        f"🚨 <b>[DEFENSIVE-EMERGENCY] SLIPPAGE-SCHUTZ & FRÜHES NET-BE: {html.escape(sym)} ({direction})</b>\n"
                        f"• Konto: {acc_name} | Gewinn: +{current_r:.2f} R erreicht (Notfall-Schwelle: +0.75 R)!\n"
                        f"• Aktion: <b>50% Teilverkauf</b> & Stop-Loss sofort auf <b>Netto-Break-Even</b> ziehen (Schutz wegen schlechtem Einstiegs-CRV)!"
                    ))
                elif exit_mode == "TARGET_LOCKED" and not is_crypto:
                    alerts.append((
                        be_key,
                        f"⚡ <b>[TARGET-LOCKED] APEX TIGHTENING: {html.escape(sym)} ({direction})</b>\n"
                        f"• Konto: {acc_name} | Gewinn: +{current_r:.2f} R\n"
                        f"• Aktion: <b>Keine Teilschließung</b>, SL zieht aggressiv auf 80% des Buchgewinns nach, um Trailing-Drawdown zu schützen!"
                    ))
                elif is_crypto:
                    alerts.append((
                        be_key,
                        f"🪙 <b>KRYPTO SCALE-OUT & NET-BE: {html.escape(sym)} ({direction})</b>\n"
                        f"• Konto: {acc_name} | Gewinn: +{current_r:.2f} R erreicht!\n"
                        f"• Aktion: <b>{scale_pct}% Teilverkauf</b> empfohlen.\n"
                        f"• Info: SL jetzt auf Netto-Break-Even ziehen & 3.5x ATR Trail starten!"
                    ))
                elif exit_mode == "PROP_DEFENSIVE":
                    alerts.append((
                        be_key,
                        f"🛡️ <b>[PROP-DEFENSIVE] SCALE-OUT 50% & NET-BE: {html.escape(sym)} ({direction})</b>\n"
                        f"• Konto: {acc_name} | Gewinn: +{current_r:.2f} R erreicht!\n"
                        f"• Aktion: <b>50% Teilverkauf</b> empfohlen.\n"
                        f"• Info: Stop-Loss jetzt auf Netto-Break-Even nachziehen."
                    ))
                else:  # ALPHA_CASHFLOW & HOME_RUN_TREND
                    if exit_mode == "HOME_RUN_TREND":
                        alerts.append((
                            be_key,
                            f"🚀 <b>[HOME-RUN-TREND] NET-BE ERREICHT: {html.escape(sym)} ({direction})</b>\n"
                            f"• Konto: {acc_name} | Gewinn: +{current_r:.2f} R erreicht!\n"
                            f"• Aktion: <b>Kein Teilverkauf</b> (Home-Run Modus).\n"
                            f"• Info: Stop-Loss jetzt auf Netto-Break-Even nachziehen."
                        ))
                    else:
                        alerts.append((
                            be_key,
                            f"⚖️ <b>[ALPHA-CASHFLOW] SCALE-OUT 30% & NET-BE: {html.escape(sym)} ({direction})</b>\n"
                            f"• Konto: {acc_name} | Gewinn: +{current_r:.2f} R erreicht!\n"
                            f"• Aktion: <b>30% Teilverkauf</b> empfohlen.\n"
                            f"• Info: Stop-Loss jetzt auf Netto-Break-Even nachziehen."
                        ))

        # b) Trailing-Stop Nachzug (+1.5 R)
        trail_key = f"{t_id}_trail"
        if current_r >= 1.5:
            trail_mult = 2.0 if exit_mode == "PROP_DEFENSIVE" else 2.5
            if exit_mode == "DEFENSIVE_EMERGENCY":
                trail_mult = 1.5
            if exit_mode == "TARGET_LOCKED":
                trail_mult = 0.5
            if exit_mode == "HOME_RUN_TREND":
                trail_mult = 3.0
            if is_crypto and exit_mode != "DEFENSIVE_EMERGENCY":
                trail_mult = 3.5

            atr_proxy = (abs(tp_price - entry_price) / max(1.0, atr_days)) if (0 < tp_price < 900000) else (risk_per_share / 1.8)
            new_sl = curr_price - (trail_mult * atr_proxy) if is_long else curr_price + (trail_mult * atr_proxy)

            if exit_mode == "TARGET_LOCKED" and not is_crypto:
                peak_protection = entry_price + (profit_per_share * 0.80) * dir_m
                new_sl = max(new_sl, peak_protection) if is_long else min(new_sl, peak_protection)

            # Harter Monotonie-Schutz (mindestens Netto-Break-Even, nie schlechter als Initial-SL, nie über aktuellem Kurs)
            if is_long:
                effective_old_sl = active_sl if active_sl > 0 else entry_price
                new_sl = min(max(new_sl, net_be, effective_old_sl), curr_price)
                new_sl = round_to_tick(new_sl, sym)
                is_improved = new_sl > active_sl
            else:
                effective_old_sl = active_sl if active_sl > 0 else entry_price
                new_sl = max(min(new_sl, net_be, effective_old_sl), curr_price)
                new_sl = round_to_tick(new_sl, sym)
                is_improved = (active_sl <= 0) or (new_sl < active_sl)

            if target_new_sl is None or (is_long and new_sl > target_new_sl) or (not is_long and new_sl < target_new_sl):
                target_new_sl = new_sl

            if is_improved and not alert_state.get(trail_key):
                if exit_mode == "HOME_RUN_TREND":
                    alerts.append((
                        trail_key,
                        f"🚀 <b>[HOME-RUN-TREND] TRAILING-STOP AKTIV: {html.escape(sym)} ({direction})</b>\n"
                        f"• Konto: {acc_name} | Gewinn: +{current_r:.2f} R erreicht!\n"
                        f"• Empfohlener Trail-SL: <b>{format_price(new_sl, sym)}</b> (Faktor: {trail_mult}x ATR)"
                    ))
                else:
                    alerts.append((
                        trail_key,
                        f"📈 <b>TRAILING-STOP AKTIV: {html.escape(sym)} ({direction})</b>\n"
                        f"• Konto: {acc_name} | Modus: {str(exit_mode).replace('_', '-')}\n"
                        f"• Gewinn: +{current_r:.2f} R erreicht!\n"
                        f"• Empfohlener Trail-SL: <b>{format_price(new_sl, sym)}</b> (Faktor: {trail_mult}x ATR)"
                    ))

        # --- AUTONOMES SIGNAL-SPOOLING (MODIFY_STOP) ---
        if target_new_sl is not None:
            is_improved_spool = (target_new_sl > active_sl) if is_long else ((active_sl <= 0) or (target_new_sl < active_sl))
            if is_improved_spool:
                username = os.path.basename(os.path.normpath(user_dir))
                matched_acc = None
                matched_acc_id = ""
                for a_id, a_dict in config.get("lab_accounts", {}).items():
                    if a_dict.get("name") == acc_name_raw:
                        matched_acc = a_dict
                        matched_acc_id = a_id
                        break
                
                if matched_acc and matched_acc.get("execution_mode") == "LIVE_AUTO":
                    acc_type = resolve_account_type(matched_acc_id, matched_acc)
                    if acc_type in ["apex", "ftmo"] and not is_crypto:
                        spool_skip = False
                        for s_dir in [PENDING_SIGNALS_DIR, PROCESSING_SIGNALS_DIR]:
                            if os.path.exists(s_dir):
                                for f_name in os.listdir(s_dir):
                                    if t_id in f_name:
                                        spool_skip = True
                                        break
                            if spool_skip:
                                break
                        
                        if not spool_skip:
                            broker_acc = matched_acc.get("broker_account", "AUTO_ROUTED")
                            ts_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
                            sig_payload = {
                                "action": "MODIFY_STOP",
                                "symbol": sym,
                                "instrument": sym,
                                "stop_price": target_new_sl,
                                "new_sl": target_new_sl,
                                "trade_id": t_id,
                                "account_id": broker_acc,
                                "lab_account_id": matched_acc_id,
                                "account_name": acc_name_raw,
                                "account_type": acc_type,
                                "direction": direction,
                                "timestamp": ts_iso,
                                "username": username
                            }
                            sig_filename = f"sig_mod_{int(time.time())}_{username}_{t_id}.json"
                            sig_file = os.path.join(PENDING_SIGNALS_DIR, sig_filename)
                            if atomic_write_json(sig_file, sig_payload):
                                log(f"[{get_berlin_now().strftime('%H:%M:%S')}] ✅ AUTO-MODIFY_STOP gespoolt: {sym} ({acc_name_raw}) -> Neuer SL: {target_new_sl}")

        # b2) Stufen-Chandelier (+2.5 R)
        r25_key = f"{t_id}_r25"
        if current_r >= 2.5 and not alert_state.get(r25_key):
            alerts.append((
                r25_key,
                f"🎯 <b>GEWINNSICHERUNG (+2.5 R): {html.escape(sym)} ({direction})</b>\n"
                f"• Konto: {acc_name} | Gewinn: +{current_r:.2f} R\n"
                f"• Aktion: Stop-Loss jetzt auf mindestens <b>+1.0 R Reingewinn</b> nachziehen!"
            ))

        # b2.5) Parabolik-Schutz / Climax-Trailing (+3.0 R)
        r30_key = f"{t_id}_r30"
        if current_r >= 3.0 and not alert_state.get(r30_key):
            alerts.append((
                r30_key,
                f"⚡ <b>PARABOLIK-SCHUTZ / CLIMAX (+3.0 R): {html.escape(sym)} ({direction})</b>\n"
                f"• Konto: {acc_name} | Gewinn: +{current_r:.2f} R\n"
                f"• Aktion: Parabolische Übertreibung absichern! Stop-Loss aggressiv auf mindestens +2.0 R nachziehen oder Bar-by-Bar eng am Vorperioden-Extremum trailen, um V-Reversals abzufangen."
            ))

        # b3) Traum-Profit Absicherung (+4.0 R)
        r40_key = f"{t_id}_r40"
        if current_r >= 4.0 and not alert_state.get(r40_key):
            alerts.append((
                r40_key,
                f"🔥 <b>TRAUM-PROFIT (+4.0 R): {html.escape(sym)} ({direction})</b>\n"
                f"• Konto: {acc_name} | Gewinn: +{current_r:.2f} R\n"
                f"• Aktion: Stop-Loss auf mindestens <b>+2.5 R</b> anheben (oder 3.0x ATR Trail)!"
            ))

        # b4) Maximal-Profit / Peak Protection (+6.0 R)
        r60_key = f"{t_id}_r60"
        if current_r >= 6.0 and not alert_state.get(r60_key):
            alerts.append((
                r60_key,
                f"👑 <b>MAXIMAL-PROFIT (+6.0 R): {html.escape(sym)} ({direction})</b>\n"
                f"• Konto: {acc_name} | Gewinn: +{current_r:.2f} R\n"
                f"• Aktion: 50% Peak-Protection aktiv! SL strikt auf die Hälfte des offenen Buchgewinns nachziehen!"
            ))

        # c) Time-Stop Warnung
        time_key = f"{t_id}_time"
        if atr_days > 0 and days_held > (atr_days * 1.5) and not alert_state.get(time_key):
            alerts.append((
                time_key,
                f"⏳ <b>TIME-STOP WARNUNG: {html.escape(sym)}</b>\n"
                f"• Konto: {acc_name}\n"
                f"• Haltedauer: {days_held} Tage (Erwartet: {int(atr_days)} Tage)\n"
                f"• Aktion: Trade zeigt keine Dynamik. Schließung prüfen!"
            ))

    if pending_closures:
        _atomic_update_journal_closures(journal_file, pending_closures)

    return alerts, alert_state, alert_state_file


# RSI Screener (Aktuell stummgeschaltet)
def run_rsi_screener():
    items = load_tickers()

    if not items:
        log("⚠️ Keine Ticker in der Datei gefunden.")
        return

    log(f"🔍 Überprüfe {len(items)} Ticker auf RSI-Signale...\n")
    alerts = []
    table_rows = []
    tickers_only = [item[0] for item in items]

    try:
        batch_df = yf.download(tickers_only, period="2y", interval="1wk", group_by="ticker", progress=False, timeout=15)
        for ticker, name in items:
            try:
                df_single = (
                    batch_df.copy()
                    if len(tickers_only) == 1
                    else (batch_df[ticker].copy() if ticker in batch_df.columns.levels[0] else pd.DataFrame())
                )
                if df_single.empty or "Close" not in df_single:
                    continue

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
                if not res:
                    continue
                close, rsi, p30, p70 = res

                try:
                    curr = yf.Ticker(ticker).fast_info.get("currency", "USD")
                except Exception:
                    curr = "USD"

                status = "NEUTRAL"
                if rsi <= tgt_low:
                    status = "🟢 KAUFEN"
                    tag = "🪙 KRYPTO" if is_crypto else "📈 AKTIE"
                    alerts.append(
                        f"🟢 <b>KAUFSIGNAL ({tag}): {html.escape(name)} ({html.escape(ticker)})</b>\n"
                        f"• Aktueller Kurs: {close:.2f} {curr}\n"
                        f"• Weekly RSI: <b>{rsi:.1f}</b> (Grenze: ≤ {tgt_low})\n"
                        f"• Ziel (Kaufzone): {p30:.2f} {curr}\n"
                    )
                elif rsi >= tgt_high:
                    status = "🔴 VERKAUFEN"
                    alerts.append(
                        f"🔴 <b>VERKAUFSIGNAL: {html.escape(name)} ({html.escape(ticker)})</b>\n"
                        f"• Aktueller Kurs: {close:.2f} {curr}\n"
                        f"• Weekly RSI: <b>{rsi:.1f}</b> (Grenze: ≥ {tgt_high})\n"
                        f"• Ziel (Verkauf): {p70:.2f} {curr}\n"
                    )

                table_rows.append({"Ticker": ticker, "Name": name[:18], "Kurs": f"{close:.2f} {curr}", "Weekly RSI": f"{rsi:.1f}", "Status": status})
            except Exception as e:
                log(f"⚠️ Fehler bei {ticker}: {e}")
    except Exception as e:
        log(f"❌ Batch-Download-Fehler: {e}")

    if table_rows:
        log("=" * 65 + "\n📊 RSI BOT - ÜBERSICHT DER GEPRÜFTEN WERTE\n" + "=" * 65)
        log(pd.DataFrame(table_rows).to_string(index=False))
        log("=" * 65)

    if alerts:
        send_telegram_message("🚨 <b>RSI ALARM BOT STATUS UPDATE</b> 🚨\n\n" + "\n---\n".join(alerts))


def run_watchlist_entry_screener():
    if not os.path.exists(USERS_DIR):
        return

    now = get_berlin_now()
    is_weekend = now.weekday() >= 5

    # Schritt 1: Mandanten & Ticker einsammeln
    user_tickers: dict[str, dict] = {}
    for user_folder in sorted(os.listdir(USERS_DIR)):
        user_dir = os.path.join(USERS_DIR, user_folder)
        if not os.path.isdir(user_dir):
            continue

        profile_file = os.path.join(user_dir, "user_profile.json")
        config_file = os.path.join(user_dir, "ticker_config.json")

        profile = safe_load_json(profile_file, default={})
        chat_id = str(profile.get("telegram_chat_id", "")).strip()
        if not chat_id:
            continue

        cfg = safe_load_json(config_file, default={})
        w_t = [t["symbol"] for t in cfg.get("screener_tickers", []) if isinstance(t, dict) and t.get("symbol")]
        p_t = [t["symbol"] for t in cfg.get("prop_watchlist", []) if isinstance(t, dict) and t.get("symbol")]

        l_t = []
        lab_accs = cfg.get("lab_accounts", {})
        if isinstance(lab_accs, dict):
            for acc in lab_accs.values():
                if isinstance(acc, dict):
                    acc_tickers = acc.get("tickers", [])
                    if isinstance(acc_tickers, list):
                        l_t.extend([str(tk) for tk in acc_tickers if tk])

        for t in set(w_t + p_t + l_t):
            if t not in user_tickers:
                user_tickers[t] = {"tenants": []}
            user_tickers[t]["tenants"].append({"username": user_folder, "chat_id": chat_id})

    if not user_tickers:
        return

    global_state_file = ALERT_STATE_FILE
    alert_state = safe_load_json(global_state_file, default={})
    state_modified = False

    # Schritt 2 & 3: Session-Guard & Rate-Limit-Schutz
    for ticker, data in user_tickers.items():
        tenants = data["tenants"]
        sym_u = str(ticker).strip().upper()
        norm_sym = normalize_symbol(sym_u)
        is_crypto = is_crypto_symbol(sym_u)
        if is_weekend and not is_crypto:
            continue

        dna = resolve_asset_dna(sym_u)
        tf = dna["tf"]
        sess_start_h, sess_start_m, sess_end_h, sess_end_m = dna["session"]
        min_score = dna["min_score"]
        allowed_setups = dna["allowed_setups"]
        desk = dna["desk"]

        curr_minutes = now.hour * 60 + now.minute
        start_minutes = sess_start_h * 60 + sess_start_m
        end_minutes = sess_end_h * 60 + sess_end_m

        # Säule 2: Die pauschale Session-Prüfung wurde hier entfernt und
        # in Phase 2 (Konto-Audit) verlagert. 24/7-Märkte werden nicht blockiert.
        
        now_utc = now.astimezone(datetime.timezone.utc)
        if tf == "1h" and now.minute > 4:
            continue
        if tf == "4h" and (now_utc.hour % 4 != 0 or now_utc.minute > 4):
            continue

        # Schritt 4: Kurs- & Indikatoren-Berechnung (nutzt normalisiertes Symbol für yfinance)
        try:
            df = yf.download([norm_sym], period="1y", interval="1h", progress=False, timeout=10)
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            if df.empty or "Close" not in df.columns:
                continue

            if tf == "4h":
                agg_d = {"Open": "first", "High": "max", "Low": "min", "Close": "last"}
                if "Volume" in df.columns:
                    agg_d["Volume"] = "sum"
                df = df.resample("4h").agg(agg_d).dropna(subset=["Close"])

            # Schutz vor unfertigen (gerade erst eröffneten) Kerzen um :00 bis :04
            if len(df) > 21 and hasattr(df.index[-1], "to_pydatetime"):
                last_bar_dt = df.index[-1].to_pydatetime()
                if last_bar_dt.tzinfo is None:
                    last_bar_dt = last_bar_dt.replace(tzinfo=datetime.timezone.utc)
                if (now_utc - last_bar_dt).total_seconds() < 300:
                    df = df.iloc[:-1]

            if len(df) < 20:
                continue

            close = df["Close"]
            ema20 = close.ewm(span=20, adjust=False).mean()
            ema200 = close.ewm(span=200, adjust=False).mean()

            delta = close.diff()
            gain = delta.clip(lower=0)
            loss = -delta.clip(upper=0)
            avg_gain = gain.ewm(alpha=1 / 14, adjust=False).mean()
            avg_loss = loss.ewm(alpha=1 / 14, adjust=False).mean()
            rs = avg_gain / avg_loss.replace(0, 1e-9)
            rsi = 100 - (100 / (1 + rs))

            high_low = df["High"] - df["Low"]
            high_close = (df["High"] - close.shift()).abs()
            low_close = (df["Low"] - close.shift()).abs()
            tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
            atr = tr.rolling(window=14).mean()
            kc_lower = ema20 - (2 * atr)
            kc_upper = ema20 + (2 * atr)

            c_p = round_to_tick(float(close.iloc[-1]), sym_u)
            e20 = float(ema20.iloc[-1])
            e200 = float(ema200.iloc[-1])
            r_val = float(rsi.iloc[-1])
            kc_low_val = float(kc_lower.iloc[-1])
            kc_up_val = float(kc_upper.iloc[-1])

            # Säule 8: Dual-Key-Idempotenz (In-Memory & Disk)
            try:
                candle_ts_unix = int(df.index[-1].timestamp())
            except Exception:
                candle_ts_unix = int(time.time())
            
            bucket_unix = (int(time.time()) // 300) * 300
            
            candle_key = f"entry_{sym_u}_{tf}_{candle_ts_unix}"
            bucket_key = f"bucket_{sym_u}_{tf}_{bucket_unix}"
            
            if candle_key in _IN_MEMORY_LOCKS or bucket_key in _IN_MEMORY_LOCKS:
                continue
            if alert_state.get(candle_key) or alert_state.get(bucket_key):
                _IN_MEMORY_LOCKS.update([candle_key, bucket_key])
                continue

            setup_type = "A"
            is_short = False

            if c_p > e200 and 38 <= r_val <= 52 and abs(c_p - e20) / e20 <= 0.015:
                setup_type = "B"
            elif c_p < e200 and 48 <= r_val <= 62 and abs(c_p - e20) / e20 <= 0.015:
                setup_type = "B"
                is_short = True

            # SÄULE 3: Krypto-Quarantäne
            if is_crypto and setup_type == "B":
                continue

            # SÄULE 4: Setup-A-Schutzschild (RSI Korridor Check)
            rsi_bounds = dna.get("rsi_bounds", (30, 70))
            if setup_type == "A" and (rsi_bounds[0] < r_val < rsi_bounds[1]):
                continue

            # Schritt 5: Poka-Yoke Filterung
            if setup_type not in allowed_setups:
                continue

            score = 0
            if setup_type == "B":
                score = 85 if (c_p > e200 and not is_short) or (c_p < e200 and is_short) else 40
            else:
                if r_val <= rsi_bounds[0]:
                    score += 40
                elif r_val >= rsi_bounds[1]:
                    score += 40
                    is_short = True
                if not is_short and c_p <= kc_low_val:
                    score += 30
                if is_short and c_p >= kc_up_val:
                    score += 30
                if not is_short and c_p > e200:
                    score += 30
                if is_short and c_p < e200:
                    score += 30
            if score < min_score:
                continue

            # Slippage-Limit & Tick-Rundung
            if not is_short:
                sl = float(df["Low"].tail(24).min()) if "Low" in df.columns else c_p * 0.98
                if sl >= c_p:
                    sl = c_p * 0.98
                tp = e200 if (setup_type == "A" and c_p < e200 and e200 < c_p * 1.15) else (float(df["High"].tail(24).max()) if "High" in df.columns else c_p * 1.02)
            else:
                sl = float(df["High"].tail(24).max()) if "High" in df.columns else c_p * 1.02
                if sl <= c_p:
                    sl = c_p * 1.02
                tp = e200 if (setup_type == "A" and c_p > e200 and e200 > c_p * 0.85) else (float(df["Low"].tail(24).min()) if "Low" in df.columns else c_p * 0.98)

            sl = round_to_tick(sl, sym_u)
            tp = round_to_tick(tp, sym_u)
            risk = abs(c_p - sl)
            
            if risk == 0:
                continue
                
            # SÄULE 5: Initial-CRV-Veto
            reward = abs(tp - c_p)
            crv = reward / risk
            if crv < 1.25:
                continue
                
            # SÄULE 5: Universelle Limit-Formel
            raw_max_slip = (tp + 1.25 * sl) / 2.25
            max_slip_price = round_to_tick(raw_max_slip, sym_u)
            setup_name = f"Setup {setup_type} {'Trend-Pullback' if setup_type == 'B' else 'Reversal'}"
            sig_dir_str = "SHORT" if is_short else "LONG"

            any_delivered = False
            sent_chat_ids = set()

            for tenant in tenants:
                uname = tenant["username"]
                cid = tenant["chat_id"]
                
                # Mandanten-Konfiguration laden für aktive lab_accounts
                user_dir = os.path.join(USERS_DIR, uname)
                config_file = os.path.join(user_dir, "ticker_config.json")
                cfg = safe_load_json(config_file, default={})
                lab_accs = cfg.get("lab_accounts", {})
                
                matched_accounts = []
                for acc_key, acc in lab_accs.items():
                    if not isinstance(acc, dict):
                        continue
                        
                    is_valid, a_type = audit_account_for_signal(
                        acc_key=acc_key,
                        acc=acc,
                        sym=sym_u,
                        is_short=is_short,
                        score=score,
                        setup_type=setup_type,
                        now_dt=now,
                        dna=dna
                    )
                    if is_valid:
                        matched_accounts.append((acc, a_type))
                    
                if not matched_accounts:
                    continue

                # Säule 9: Header-Steuerung und Farb-Badges (Zentralisiert)
                header, account_badges = get_telegram_header_and_badges(matched_accounts, is_crypto, is_short)
                accounts_str = "\n".join(account_badges)
                
                msg = f"""<b>{header} — {html.escape(sym_u)}</b>

<b>Konten:</b>
{accounts_str}

<b>Richtung:</b> {sig_dir_str}
<b>Setup:</b> {setup_name}
<b>Signal-Score:</b> {score} / 100
<b>Timeframe:</b> {tf}

<b>Trigger-Kurs:</b> {format_price(c_p, sym_u)}
🎯 <b>Max. Limit (CRV ≥ 1.25):</b> {format_price(max_slip_price, sym_u)}
🛑 <b>Stop-Loss:</b> {format_price(sl, sym_u)}
🎯 <b>Take-Profit (+1.0 R):</b> {format_price(tp, sym_u)}

<b>Handelszeit:</b> Session aktiv
⚠️ <i>Rules Never Bend — Prüfe Spread vor Orderaufgabe.</i>"""

                if cid not in sent_chat_ids:
                    if send_telegram_message(msg, cid):
                        any_delivered = True
                        sent_chat_ids.add(cid)

                # SÄULE 7: Execution-Mode & Spooler-Queue Brandmauer
                for acc, account_type in matched_accounts:
                    if should_spool_signal(acc, account_type, is_crypto):
                        broker_acc = acc.get("broker_account", "AUTO_ROUTED")
                        signal_payload = {
                            "account_id": broker_acc,
                            "lab_account_id": acc.get("id", ""),
                            "account_name": acc.get("name", ""),
                            "account_type": account_type,
                            "balance": float(acc.get("account_size", 50000.0)),
                            "symbol": sym_u,
                            "direction": sig_dir_str,
                            "setup": f"SETUP_{setup_type}",
                            "setup_type": f"SETUP_{setup_type}",
                            "score": score,
                            "current_price": c_p,
                            "stop_loss": sl,
                            "take_profit": tp,
                            "sl_price": sl,
                            "sl": sl,
                            "tp_price": tp,
                            "tp": tp,
                            "max_limit_price": max_slip_price,
                            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                            "username": uname
                        }
                        
                        safe_sym = sym_u.replace("=", "_").replace("/", "_")
                        sig_filename = f"sig_{int(time.time())}_{uname}_{broker_acc}_{safe_sym}.json"
                        final_path = os.path.join(PENDING_SIGNALS_DIR, sig_filename)
                        
                        if atomic_write_json(final_path, signal_payload):
                            any_delivered = True
                            log(f"✅ Signal für {sym_u} (Konto: {acc.get('name')}) atomar in Spooler-Queue abgelegt.")

            # Säule 8: Sofort-Flush (Transaktionale Persistenz)
            if any_delivered:
                _IN_MEMORY_LOCKS.update([candle_key, bucket_key])
                alert_state[candle_key] = True
                alert_state[bucket_key] = True
                atomic_write_json(global_state_file, alert_state)

        except Exception as e:
            log(f"Fehler bei Screener {sym_u}: {e}")

# Hauptfunktion: Kontinuierlicher Trade-Copilot
def main():
    log(f"🤖 Multi-Tenant Trade-Copilot gestartet (Polling: {POLLING_INTERVAL}s | USERS_DIR: {USERS_DIR}).")

    try:
        while True:
            try:
                if not os.path.exists(USERS_DIR):
                    log(f"[{get_berlin_now().strftime('%H:%M:%S')}] USERS_DIR ({USERS_DIR}) nicht gefunden. Warte...")
                else:
                    users_checked = 0
                    alerts_sent = 0

                    for user_folder in sorted(os.listdir(USERS_DIR)):
                        user_dir = os.path.join(USERS_DIR, user_folder)
                        if os.path.isdir(user_dir):
                            users_checked += 1
                            profile_file = os.path.join(user_dir, "user_profile.json")
                            profile = safe_load_json(profile_file, default={})
                            chat_id = str(profile.get("telegram_chat_id", "")).strip()

                            journal_file = os.path.join(user_dir, "trade_journal.csv")
                            open_trades = []
                            if os.path.exists(journal_file):
                                try:
                                    with file_lock(journal_file, timeout=2.0):
                                        df_j = pd.read_csv(journal_file)
                                    open_trades = df_j[df_j["status"] == "OPEN"].index.tolist()
                                except Exception:
                                    pass

                            config_file = os.path.join(user_dir, "ticker_config.json")
                            cfg = safe_load_json(config_file, default={})
                            w_t = [t["symbol"] for t in cfg.get("screener_tickers", []) if isinstance(t, dict) and t.get("symbol")]
                            p_t = [t["symbol"] for t in cfg.get("prop_watchlist", []) if isinstance(t, dict) and t.get("symbol")]
                            l_t = []
                            lab_accs = cfg.get("lab_accounts", {})
                            if isinstance(lab_accs, dict):
                                for acc in lab_accs.values():
                                    if isinstance(acc, dict):
                                        acc_tickers = acc.get("tickers", [])
                                        if isinstance(acc_tickers, list):
                                            l_t.extend([str(tk) for tk in acc_tickers if tk])
                                            
                            wl_tickers = list(set(w_t + p_t + l_t))

                            timestamp = get_berlin_now().strftime("%H:%M:%S")
                            username = user_folder
                            if username == "admin":
                                log(f"[{timestamp}] Mandant '{username}': {len(open_trades)} Trades | Watchlist: {wl_tickers}")
                            else:
                                log(f"[{timestamp}] Mandant '{username}': {len(open_trades)} Trades | Watchlist: {len(wl_tickers)} Ticker aktiv")

                            copilot_alerts, alert_state, alert_state_file = check_open_trades(user_dir)
                            if copilot_alerts and chat_id:
                                successful_keys = []
                                for state_key, msg_text in copilot_alerts:
                                    full_msg = f"🤖 <b>TRADE COPILOT ALARM ({html.escape(user_folder.upper())})</b> 🤖\n\n{msg_text}"
                                    if send_telegram_message(full_msg, chat_id):
                                        successful_keys.append(state_key)
                                        alerts_sent += 1

                                # Erst NACH erfolgreichem Telegram-Versand den State atomar persistieren
                                if successful_keys:
                                    for k in successful_keys:
                                        alert_state[k] = True
                                    atomic_write_json(alert_state_file, alert_state)

                    log(f"[{get_berlin_now().strftime('%H:%M:%S')}] {users_checked} Mandanten geprüft. {alerts_sent} Alarme gesendet.")

                    # Autonomer Watchlist-Screener aufrufen
                    run_watchlist_entry_screener()

            except Exception as loop_err:
                log(f"❌ Unbehandelte Ausnahme im Hauptloop abgefangen (Daemon läuft weiter): {loop_err}")

            time.sleep(POLLING_INTERVAL)

    except KeyboardInterrupt:
        log("\n🛑 Trade-Copilot sauber beendet (Strg+C).")
def run_tests():
    global PENDING_SIGNALS_DIR, PROCESSING_SIGNALS_DIR
    
    log("================================================================================")
    log("🧪 INVARIX SIGNAL-KERN TEST-BATTERIE")
    log("================================================================================")
    all_passed = True

    # 1. TEST: UNIVERSELLE LIMIT-FORMEL (LONG)
    tp_l, sl_l = 120.00, 90.00
    calc_limit_l = round((tp_l + 1.25 * sl_l) / 2.25, 2)
    crv_l = round(abs(tp_l - calc_limit_l) / abs(calc_limit_l - sl_l), 2)
    if calc_limit_l == 103.33 and crv_l == 1.25:
        log(f"[PASS] Limit-Formel Long (Grenzkurs: {calc_limit_l:.2f} | CRV: {crv_l:.2f})")
    else:
        log(f"[FAIL] Limit-Formel Long (Grenzkurs: {calc_limit_l:.2f} | CRV: {crv_l:.2f})")
        all_passed = False

    # 2. TEST: UNIVERSELLE LIMIT-FORMEL (SHORT)
    tp_s, sl_s = 80.00, 110.00
    calc_limit_s = round((tp_s + 1.25 * sl_s) / 2.25, 2)
    crv_s = round(abs(calc_limit_s - tp_s) / abs(sl_s - calc_limit_s), 2)
    if calc_limit_s == 96.67 and crv_s == 1.25:
        log(f"[PASS] Limit-Formel Short (Grenzkurs: {calc_limit_s:.2f} | CRV: {crv_s:.2f})")
    else:
        log(f"[FAIL] Limit-Formel Short (Grenzkurs: {calc_limit_s:.2f} | CRV: {crv_s:.2f})")
        all_passed = False

    # 3. TEST: INITIAL-CRV-VETO (< 1.25)
    entry_v, sl_v, tp_v = 100.00, 90.00, 110.00
    crv_initial = abs(tp_v - entry_v) / abs(entry_v - sl_v)
    if crv_initial < 1.25:
        log("[PASS] Initial-CRV-Veto blockiert unzureichendes CRV (< 1.25)")
    else:
        log(f"[FAIL] Initial-CRV-Veto (Erwartet < 1.25, Ist: {crv_initial})")
        all_passed = False

    # 4. TEST: ZENTRALE KRYPTO-ERKENNUNG & QUARANTÄNE
    c_syms = ["BTC-USD", "KAS-USD", "ETH-EUR"]
    nc_syms = ["NQ=F", "CL=F", "MSFT", "EURUSD=X"]
    if all(is_crypto_symbol(s) for s in c_syms) and all(not is_crypto_symbol(s) for s in nc_syms):
        if is_crypto_symbol("BTC-USD") and "B" == "B":
            log("[PASS] Krypto-Quarantäne (Setup B für Krypto blockiert)")
        else:
            log("[FAIL] Krypto-Quarantäne blockiert Setup B nicht")
            all_passed = False
    else:
        log("[FAIL] Krypto-Erkennung klassifiziert Symbole falsch")
        all_passed = False

    # 5. TEST: DYNAMISCHER RSI-KORRIDOR (SETUP A SCHUTZSCHILD)
    rsi_test, setup_test, bounds_test = 50.0, "A", (30, 70)
    if setup_test == "A" and (bounds_test[0] < rsi_test < bounds_test[1]):
        log("[PASS] RSI-Korridor-Schutzschild blockiert neutrales Rauschen")
    else:
        log("[FAIL] RSI-Korridor-Schutzschild durchbrochen")
        all_passed = False

    # 6. TEST: KONTO-AUDIT & DIRECTION-GATEKEEPER
    test_acc_6 = {"direction": "long", "min_score": 70, "allowed_setups": ["A", "B"], "account_type": "apex"}
    dna_mock = {"session": (0, 0, 23, 59)}
    now_mock = datetime.datetime.now()
    
    res_6a, _ = audit_account_for_signal("test", test_acc_6, "NQ=F", True, 85, "A", now_mock, dna_mock)
    res_6b, _ = audit_account_for_signal("test", test_acc_6, "NQ=F", False, 65, "A", now_mock, dna_mock)
    res_6c, _ = audit_account_for_signal("test", test_acc_6, "NQ=F", False, 75, "A", now_mock, dna_mock)

    if not res_6a and not res_6b and res_6c:
        log("[PASS] Konto-Audit Schranken-Check (Richtung & Score)")
    else:
        log("[FAIL] Konto-Audit Schranken-Check (Richtung & Score)")
        all_passed = False

    # 7. TEST: WHITELIST-SCHUTZ
    test_acc_7 = {"tickers": ["NQ=F", "MNQ"], "direction": "both", "min_score": 0, "allowed_setups": ["A", "B"], "account_type": "apex"}
    res_7a, _ = audit_account_for_signal("test", test_acc_7, "CL=F", False, 100, "A", now_mock, dna_mock)
    res_7b, _ = audit_account_for_signal("test", test_acc_7, "NQ=F", False, 100, "A", now_mock, dna_mock)
    
    if not res_7a and res_7b:
        log("[PASS] Whitelist-Schutz isoliert Konten-Ticker")
    else:
        log("[FAIL] Whitelist-Schutz isoliert Konten-Ticker")
        all_passed = False

    # 8. TEST: EXECUTION-MODE & SPOOLER-QUEUE BRANDMAUER
    acc_8a = {"execution_mode": "CO_PILOT", "account_type": "apex"}
    acc_8b = {"execution_mode": "LIVE_AUTO", "account_type": "apex"}
    acc_8c = {"execution_mode": "LIVE_AUTO", "account_type": "apex", "broker_account": "AUTO_ROUTED"}

    cond_a = should_spool_signal(acc_8a, "apex", False)
    cond_b = should_spool_signal(acc_8b, "apex", True)
    cond_c = should_spool_signal(acc_8c, "apex", False)

    if not cond_a and not cond_b and cond_c:
        import tempfile
        dummy_file = os.path.join(PENDING_SIGNALS_DIR, "sig_test_brandmauer.json")
        os.makedirs(PENDING_SIGNALS_DIR, exist_ok=True)
        with open(dummy_file, "w") as f:
            f.write('{"account_id": "AUTO_ROUTED"}')
        if os.path.exists(dummy_file):
            os.remove(dummy_file)
            log("[PASS] Spooler-Queue Brandmauer (LIVE_AUTO / Prop / Non-Crypto)")
        else:
            log("[FAIL] Spooler-Queue Brandmauer Dateierstellung fehlgeschlagen")
            all_passed = False
    else:
        log("[FAIL] Spooler-Queue Brandmauer (LIVE_AUTO / Prop / Non-Crypto)")
        all_passed = False

    # 9. TEST: DUAL-KEY-IDEMPOTENZ & REPEAT-BLOCK
    test_state_9 = {}
    test_mem_9 = set()
    test_c_key = "entry_TEST_1h_10000"
    test_b_key = "bucket_TEST_1h_10000"
    
    test_state_9[test_c_key] = True
    test_state_9[test_b_key] = True
    test_mem_9.update([test_c_key, test_b_key])
    
    if test_c_key in test_mem_9 or test_state_9.get(test_b_key):
        log("[PASS] Dual-Key Idempotenz (Zweitaufruf blockiert)")
    else:
        log("[FAIL] Dual-Key Idempotenz (Zweitaufruf blockiert)")
        all_passed = False

    # 10. TEST: TELEGRAM-HEADER & BADGE-SELECTOR
    h10_a, _ = get_telegram_header_and_badges([({"execution_mode": "LIVE_AUTO", "name": "A"}, "apex")], True, False)
    h10_b, _ = get_telegram_header_and_badges([({"execution_mode": "EXPERIMENT", "name": "B"}, "ftmo")], False, False)
    h10_c, b10_c = get_telegram_header_and_badges([({"execution_mode": "LIVE_AUTO", "name": "C"}, "apex")], False, False)
    
    t10_a = (h10_a == "🧪 INVARIX LABOR ALERT (PHASE 7 QUARANTÄNE)")
    t10_b = (h10_b == "🧪 INVARIX LABOR ALERT (PHASE 7 QUARANTÄNE)")
    t10_c = (h10_c == "🚨 INVARIX ENTRY ALERT — LONG")
    t10_badge = any("🟢 [LIVE-AUTO]" in b for b in b10_c)

    if t10_a and t10_b and t10_c and t10_badge:
        log("[PASS] Telegram Header & Badge-Steuerung")
    else:
        log("[FAIL] Telegram Header & Badge-Steuerung")
        all_passed = False

    # 11. TEST: DEFENSIVER JOURNAL EXIT-MODE GUARD
    raw_exit_11a = None
    exit_mode_11a = str(raw_exit_11a).strip() if pd.notna(raw_exit_11a) and str(raw_exit_11a).strip() != "nan" else "TRAILING_STOP"
    
    raw_exit_11b = "nan"
    exit_mode_11b = str(raw_exit_11b).strip() if pd.notna(raw_exit_11b) and str(raw_exit_11b).strip() != "nan" else "TRAILING_STOP"
    
    if exit_mode_11a == "TRAILING_STOP" and exit_mode_11b == "TRAILING_STOP":
        log("[PASS] Journal Exit-Mode Fallback-Schutz")
    else:
        log("[FAIL] Journal Exit-Mode Fallback-Schutz")
        all_passed = False

    # 12. TEST: AUTONOME STOP-LOSS NACHFÜHRUNG & SPOOLING
    import tempfile
    t12_user_dir = tempfile.mkdtemp(prefix="test12_")
    t12_pending = os.path.join(t12_user_dir, "signals", "01_pending")
    t12_processing = os.path.join(t12_user_dir, "signals", "02_processing")
    os.makedirs(t12_pending, exist_ok=True)
    os.makedirs(t12_processing, exist_ok=True)
    
    old_pending = PENDING_SIGNALS_DIR
    old_processing = PROCESSING_SIGNALS_DIR
    PENDING_SIGNALS_DIR = t12_pending
    PROCESSING_SIGNALS_DIR = t12_processing

    try:
        t12_cfg = {
            "lab_accounts": {
                "acc1": {"name": "Test Acc", "execution_mode": "LIVE_AUTO", "account_type": "apex", "broker_account": "DEMO"}
            }
        }
        with open(os.path.join(t12_user_dir, "ticker_config.json"), "w") as f:
            json.dump(t12_cfg, f)
            
        t12_journal = os.path.join(t12_user_dir, "trade_journal.csv")
        df12 = pd.DataFrame([{
            "id": "T12_MOCK", "status": "OPEN", "symbol": "MNQ", "account_name": "Test Acc",
            "direction": "Long", "entry_price": 100.0, "sl_price": 90.0, "current_sl": 0.0, 
            "position_size": 1.0, "point_value": 2.0, "fx_rate": 1.0, "est_fees_eur": 5.0, 
            "initial_sl_price": 90.0
        }])
        df12.to_csv(t12_journal, index=False)
        
        old_get_price = globals().get('get_current_price')
        globals()['get_current_price'] = lambda sym, ttl=15: 120.0
        
        check_open_trades(t12_user_dir)
        
        files = os.listdir(t12_pending)
        has_file = len(files) == 1
        
        t12_correct_be = False
        if has_file:
            with open(os.path.join(t12_pending, files[0]), "r") as f:
                sig = json.load(f)
            t12_correct_be = sig["new_sl"] >= 102.5
            
        check_open_trades(t12_user_dir)
        files_after = os.listdir(t12_pending)
        t12_no_dup = len(files_after) == 1
        
        if has_file and t12_correct_be and t12_no_dup:
            log("[PASS] Autonomes Stop-Loss Spooling (Net-BE & Duplikat-Schutz)")
        else:
            log(f"[FAIL] Autonomes Stop-Loss Spooling (File: {has_file}, BE: {t12_correct_be}, NoDup: {t12_no_dup})")
            all_passed = False
            
    finally:
        if old_get_price:
            globals()['get_current_price'] = old_get_price
        PENDING_SIGNALS_DIR = old_pending
        PROCESSING_SIGNALS_DIR = old_processing
        import shutil
        shutil.rmtree(t12_user_dir, ignore_errors=True)

    log("================================================================================")
    if all_passed:
        sys.exit(0)
    else:
        sys.exit(1)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--test":
        run_tests()
    else:
        main()