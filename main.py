"""
BBR Bot — Base Break Retest | XAUUSD M5
Kirim notifikasi Telegram saat Base, Break, atau Retest terbentuk.
"""

import os
import json
import time
import logging
import requests
import pandas as pd
from datetime import datetime, timezone

# ═══════════════════════════════════════════════
#  LOGGING
# ═══════════════════════════════════════════════
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

# ═══════════════════════════════════════════════
#  CONFIG — ambil dari environment variable
# ═══════════════════════════════════════════════
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN",   "ISI_TOKEN_BOT_KAMU")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "ISI_CHAT_ID_KAMU")

BASE_LEN         = int(os.environ.get("BASE_LEN",        "10"))
BASE_MAX_ATR     = float(os.environ.get("BASE_MAX_ATR",  "2.5"))
ATR_LEN          = int(os.environ.get("ATR_LEN",         "14"))
LOOKBACK_RETEST  = int(os.environ.get("LOOKBACK_RETEST", "40"))

SYMBOL           = "GC=F"          # Gold Futures — proxy XAUUSD
STATE_FILE       = "bbr_state.json"

# ═══════════════════════════════════════════════
#  TELEGRAM
# ═══════════════════════════════════════════════
def send_telegram(message: str) -> bool:
    try:
        url  = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        resp = requests.post(
            url,
            json={
                "chat_id":    TELEGRAM_CHAT_ID,
                "text":       message,
                "parse_mode": "HTML"
            },
            timeout=10
        )
        if resp.status_code == 200:
            log.info(f"✅ Telegram terkirim: {message[:60].strip()}...")
            return True
        else:
            log.error(f"❌ Telegram error {resp.status_code}: {resp.text}")
            return False
    except Exception as e:
        log.error(f"❌ Telegram exception: {e}")
        return False

# ═══════════════════════════════════════════════
#  STATE — simpan & muat dari file JSON
#  agar state tidak hilang jika script restart
# ═══════════════════════════════════════════════
def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "base_high":       None,
        "base_low":        None,
        "base_dir":        0,
        "break_bar_idx":   -1,
        "waiting":         False,
        "last_bar_time":   None
    }

def save_state(state: dict):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        log.error(f"Gagal simpan state: {e}")

# ═══════════════════════════════════════════════
#  INDIKATOR
# ═══════════════════════════════════════════════
def calc_atr(df: pd.DataFrame, period: int) -> pd.Series:
    """ATR menggunakan Wilder's smoothing (sama seperti Pine Script ta.atr)"""
    h, l, c = df["High"], df["Low"], df["Close"]
    tr = pd.concat([
        h - l,
        (h - c.shift(1)).abs(),
        (l - c.shift(1)).abs()
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()

# ═══════════════════════════════════════════════
#  FETCH DATA
# ═══════════════════════════════════════════════
TWELVE_DATA_KEY = os.environ.get("TWELVE_DATA_KEY", "ISI_API_KEY_KAMU")

def fetch_data() -> pd.DataFrame | None:
    try:
        url = "https://api.twelvedata.com/time_series"
        params = {
            "symbol":     "XAU/USD",
            "interval":   "5min",
            "outputsize": 100,        # ambil 100 bar terakhir
            "apikey":     TWELVE_DATA_KEY,
            "format":     "JSON"
        }
        resp = requests.get(url, params=params, timeout=15)
        data = resp.json()

        if "values" not in data:
            log.error(f"Twelve Data error: {data.get('message', data)}")
            return None

        df = pd.DataFrame(data["values"])
        df = df.rename(columns={
            "datetime": "Datetime",
            "open":     "Open",
            "high":     "High",
            "low":      "Low",
            "close":    "Close",
            "volume":   "Volume"
        })
        df["Datetime"] = pd.to_datetime(df["Datetime"])
        df = df.set_index("Datetime").sort_index()
        df = df[["Open", "High", "Low", "Close"]].astype(float)
        df.dropna(inplace=True)

        min_bars = BASE_LEN + ATR_LEN + 5
        if len(df) < min_bars:
            log.warning(f"Data kurang: {len(df)} bar")
            return None

        log.info(f"Data OK: {len(df)} bar | Last close: {df['Close'].iloc[-1]:.2f}")
        return df

    except Exception as e:
        log.error(f"Gagal fetch data: {e}")
        return None
# ═══════════════════════════════════════════════
#  LOGIKA BBR UTAMA
# ═══════════════════════════════════════════════
def run_check():
    log.info("── Menjalankan pengecekan BBR ──")

    state = load_state()

    # ── Ambil data ──────────────────────────────
    df = fetch_data()
    if df is None:
        return

    # ── Kalkulasi indikator ─────────────────────
    df["ATR"]             = calc_atr(df, ATR_LEN)
    df["HH"]              = df["High"].rolling(BASE_LEN).max()
    df["LL"]              = df["Low"].rolling(BASE_LEN).min()
    df["Range"]           = df["HH"] - df["LL"]
    df["IsConsolidating"] = df["Range"] <= (df["ATR"] * BASE_MAX_ATR)
    df.dropna(inplace=True)

    if len(df) < 3:
        log.warning("Data setelah dropna kurang dari 3 bar")
        return

    # ── Ambil bar yang relevan ──────────────────
    # iloc[-1] = bar yang sedang berjalan (belum close) → skip
    # iloc[-2] = bar yang baru close      → "shift 1" di MQL4
    # iloc[-3] = bar sebelumnya           → "shift 2" di MQL4
    bar1      = df.iloc[-2]  # bar baru close
    bar2      = df.iloc[-3]  # bar sebelumnya
    bar1_time = str(df.index[-2])
    bar1_idx  = len(df) - 2

    # ── Skip jika bar sudah diproses ───────────
    if state["last_bar_time"] == bar1_time:
        log.info(f"Bar {bar1_time} sudah diproses, skip.")
        return

    was_consolidating = bool(bar2["IsConsolidating"])
    hH2  = round(float(bar2["HH"]),  2)
    lL2  = round(float(bar2["LL"]),  2)
    atr1 = round(float(bar1["ATR"]), 2)

    log.info(
        f"Bar: {bar1_time} | "
        f"Close: {bar1['Close']:.2f} | "
        f"wasConsolidating: {was_consolidating} | "
        f"waiting: {state['waiting']}"
    )

    # ═══════════════════════════════════════════
    #  BREAKOUT DETECTION
    # ═══════════════════════════════════════════
    if was_consolidating and not state["waiting"]:

        close = round(float(bar1["Close"]), 2)

        # BULLISH BREAK
        if close > hH2:
            state.update({
                "base_high":     hH2,
                "base_low":      lL2,
                "base_dir":      1,
                "break_bar_idx": bar1_idx,
                "waiting":       True
            })
            send_telegram(
                "🟢 <b>BREAK UP | XAUUSD M5</b>\n"
                f"Base High : <b>{hH2}</b>\n"
                f"Base Low  : <b>{lL2}</b>\n"
                f"ATR       : {atr1}\n"
                f"🕐 {bar1_time}"
            )
            log.info("→ BREAK UP terdeteksi")

        # BEARISH BREAK
        elif close < lL2:
            state.update({
                "base_high":     hH2,
                "base_low":      lL2,
                "base_dir":      -1,
                "break_bar_idx": bar1_idx,
                "waiting":       True
            })
            send_telegram(
                "🔴 <b>BREAK DOWN | XAUUSD M5</b>\n"
                f"Base High : <b>{hH2}</b>\n"
                f"Base Low  : <b>{lL2}</b>\n"
                f"ATR       : {atr1}\n"
                f"🕐 {bar1_time}"
            )
            log.info("→ BREAK DOWN terdeteksi")

    # ═══════════════════════════════════════════
    #  RETEST DETECTION
    # ═══════════════════════════════════════════
    elif state["waiting"] and state["break_bar_idx"] is not None:

        bars_since = bar1_idx - state["break_bar_idx"]
        base_high  = state["base_high"]
        base_low   = state["base_low"]
        base_dir   = state["base_dir"]
        bar1_low   = round(float(bar1["Low"]),   2)
        bar1_high  = round(float(bar1["High"]),  2)
        bar1_close = round(float(bar1["Close"]), 2)

        # Setup EXPIRED
        if bars_since > LOOKBACK_RETEST:
            state["waiting"] = False
            log.info(f"→ Setup expired setelah {bars_since} bar")

        # RETEST BULLISH
        elif base_dir == 1:
            if bar1_low <= base_high and bar1_low >= base_low:
                send_telegram(
                    "✅ <b>RETEST Bullish | XAUUSD M5</b>\n"
                    "Harga menyentuh zona Base dari atas\n"
                    f"Base High    : <b>{base_high}</b>\n"
                    f"Base Low     : <b>{base_low}</b>\n"
                    f"Low sekarang : <b>{bar1_low}</b>\n"
                    f"🕐 {bar1_time}"
                )
                state["waiting"] = False
                log.info("→ RETEST Bullish terdeteksi")
            elif bar1_close < base_low:
                state["waiting"] = False
                log.info("→ Setup invalid (close < base low)")

        # RETEST BEARISH
        elif base_dir == -1:
            if bar1_high >= base_low and bar1_high <= base_high:
                send_telegram(
                    "✅ <b>RETEST Bearish | XAUUSD M5</b>\n"
                    "Harga menyentuh zona Base dari bawah\n"
                    f"Base High     : <b>{base_high}</b>\n"
                    f"Base Low      : <b>{base_low}</b>\n"
                    f"High sekarang : <b>{bar1_high}</b>\n"
                    f"🕐 {bar1_time}"
                )
                state["waiting"] = False
                log.info("→ RETEST Bearish terdeteksi")
            elif bar1_close > base_high:
                state["waiting"] = False
                log.info("→ Setup invalid (close > base high)")

    # ── Simpan state ────────────────────────────
    state["last_bar_time"] = bar1_time
    save_state(state)

# ═══════════════════════════════════════════════
#  SCHEDULER — tunggu sampai M5 candle close
#  sebelum jalankan pengecekan
# ═══════════════════════════════════════════════
def seconds_to_next_candle(candle_seconds: int = 300) -> float:
    """Hitung detik sampai M5 candle berikutnya close (+10 detik buffer)"""
    now     = datetime.now(timezone.utc).timestamp()
    elapsed = now % candle_seconds
    return candle_seconds - elapsed + 10

# ═══════════════════════════════════════════════
#  ENTRY POINT
# ═══════════════════════════════════════════════
if __name__ == "__main__":
    log.info("═══════════════════════════════════════")
    log.info("  BBR Bot — XAUUSD M5 — Starting...")
    log.info("═══════════════════════════════════════")

    # Kirim test message saat bot pertama kali nyala
    send_telegram(
        "🤖 <b>BBR Bot aktif</b>\n"
        "Memantau XAUUSD M5 setiap candle close...\n\n"
        f"⚙️ Base Length    : {BASE_LEN} bar\n"
        f"⚙️ ATR Multiplier : {BASE_MAX_ATR}×\n"
        f"⚙️ ATR Length     : {ATR_LEN}\n"
        f"⚙️ Retest Lookback: {LOOKBACK_RETEST} bar"
    )

    # Jalankan sekali langsung saat start
    run_check()

    # Loop utama: jalankan tepat setelah setiap M5 candle close
    while True:
        wait = seconds_to_next_candle()
        log.info(f"Menunggu {wait:.0f} detik sampai candle berikutnya...")
        time.sleep(wait)
        run_check()
