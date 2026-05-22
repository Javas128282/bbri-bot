"""
BBR Bot — Base Break Retest + Market Bias | XAUUSD
Notifikasi Telegram untuk sinyal BBR M5 dan arah market 1H/4H.
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
#  CONFIG
# ═══════════════════════════════════════════════
TELEGRAM_TOKEN    = os.environ.get("TELEGRAM_TOKEN",    "ISI_TOKEN_BOT_KAMU")
TELEGRAM_CHAT_ID  = os.environ.get("TELEGRAM_CHAT_ID",  "ISI_CHAT_ID_KAMU")
TWELVE_DATA_KEY   = os.environ.get("TWELVE_DATA_KEY",   "ISI_API_KEY_KAMU")

# BBR Parameters
BASE_LEN          = int(os.environ.get("BASE_LEN",        "10"))
BASE_MAX_ATR      = float(os.environ.get("BASE_MAX_ATR",  "2.5"))
ATR_LEN           = int(os.environ.get("ATR_LEN",         "14"))
LOOKBACK_RETEST   = int(os.environ.get("LOOKBACK_RETEST", "40"))

# Bias Parameters
MA_FAST           = int(os.environ.get("MA_FAST", "20"))
MA_SLOW           = int(os.environ.get("MA_SLOW", "50"))

STATE_FILE        = "bbr_state.json"

# ═══════════════════════════════════════════════
#  TELEGRAM
# ═══════════════════════════════════════════════
def send_telegram(message: str) -> bool:
    try:
        url  = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        resp = requests.post(
            url,
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"},
            timeout=10
        )
        if resp.status_code == 200:
            log.info(f"✅ Telegram: {message[:60].strip()}...")
            return True
        log.error(f"❌ Telegram {resp.status_code}: {resp.text}")
        return False
    except Exception as e:
        log.error(f"❌ Telegram exception: {e}")
        return False

# ═══════════════════════════════════════════════
#  STATE
# ═══════════════════════════════════════════════
def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        # BBR state
        "base_high":       None,
        "base_low":        None,
        "base_dir":        0,
        "break_bar_idx":   -1,
        "waiting":         False,
        "last_bar_time":   None,
        # Bias state
        "last_bias":       None,
        "last_bias_hour":  None
    }

def save_state(state: dict):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        log.error(f"Gagal simpan state: {e}")

# ═══════════════════════════════════════════════
#  FETCH DATA — Twelve Data
# ═══════════════════════════════════════════════
def fetch_data_twelvedata(symbol: str, interval: str, outputsize: int = 100) -> pd.DataFrame | None:
    try:
        url    = "https://api.twelvedata.com/time_series"
        params = {
            "symbol":     symbol,
            "interval":   interval,
            "outputsize": outputsize,
            "apikey":     TWELVE_DATA_KEY,
            "format":     "JSON"
        }
        resp = requests.get(url, params=params, timeout=15)
        data = resp.json()

        if "values" not in data:
            log.error(f"Twelve Data error [{symbol}]: {data.get('message', data)}")
            return None

        df = pd.DataFrame(data["values"])
        df = df.rename(columns={
            "datetime": "Datetime",
            "open":  "Open", "high": "High",
            "low":   "Low",  "close": "Close"
        })
        df["Datetime"] = pd.to_datetime(df["Datetime"])
        df = df.set_index("Datetime").sort_index()
        df = df[["Open", "High", "Low", "Close"]].astype(float)
        df.dropna(inplace=True)
        return df

    except Exception as e:
        log.error(f"Gagal fetch [{symbol}]: {e}")
        return None

# ═══════════════════════════════════════════════
#  INDIKATOR
# ═══════════════════════════════════════════════
def calc_atr(df: pd.DataFrame, period: int) -> pd.Series:
    h, l, c = df["High"], df["Low"], df["Close"]
    tr = pd.concat([
        h - l,
        (h - c.shift(1)).abs(),
        (l - c.shift(1)).abs()
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()

def get_trend(df: pd.DataFrame) -> str:
    """Tentukan trend: bullish jika MA20 > MA50, bearish jika sebaliknya."""
    if len(df) < MA_SLOW + 2:
        return "unknown"
    close   = df["Close"]
    ma_fast = close.rolling(MA_FAST).mean()
    ma_slow = close.rolling(MA_SLOW).mean()
    # Gunakan bar[-2] (bar yang sudah close)
    v_fast  = ma_fast.iloc[-2]
    v_slow  = ma_slow.iloc[-2]
    if pd.isna(v_fast) or pd.isna(v_slow):
        return "unknown"
    return "bullish" if v_fast > v_slow else "bearish"

# ═══════════════════════════════════════════════
#  MARKET BIAS CHECK
#  Jalankan setiap 1H candle close
# ═══════════════════════════════════════════════
def invert_trend(trend: str) -> str:
    """Balik arah trend untuk instrumen inverse proxy."""
    if trend == "bullish": return "bearish"
    if trend == "bearish": return "bullish"
    return "unknown"

def check_bias():
    """
    Proxy yang digunakan:
      DXY   → EUR/USD (inverse: EUR/USD bear = DXY bull)
      US10Y → TLT ETF (inverse: TLT bear = yield naik = US10Y bull)
      XAUUSD → XAU/USD langsung
    """
    log.info("── Mengecek Market Bias ──")
    state = load_state()

    # Fetch data dengan jeda antar request
    df_eurusd = fetch_data_twelvedata("EUR/USD", "1h", 60)   # proxy DXY
    time.sleep(2)
    df_xau    = fetch_data_twelvedata("XAU/USD", "1h", 60)   # XAUUSD langsung
    time.sleep(2)
    df_tlt = fetch_data_twelvedata("IEF", "4h", 60)   # ← 60 bar cukup untuk MA50   # proxy US10Y

    if df_eurusd is None or df_xau is None or df_tlt is None:
        log.warning("Satu atau lebih data bias gagal diambil, skip bias check.")
        return

    # Tentukan trend — EUR/USD dan TLT dibalik karena inverse proxy
    trend_dxy   = invert_trend(get_trend(df_eurusd))  # inverse EUR/USD
    trend_xau   = get_trend(df_xau)
    trend_us10y = invert_trend(get_trend(df_tlt))     # inverse TLT

    log.info(f"DXY(1H via EURUSD⁻¹): {trend_dxy} | XAUUSD(1H): {trend_xau} | US10Y(4H via TLT⁻¹): {trend_us10y}")

    # ── Tentukan Bias ───────────────────────────
    # SELL : DXY bullish + US10Y bullish + XAUUSD bearish
    # BUY  : DXY bearish + US10Y bearish + XAUUSD bullish
    # MIXED: sinyal tidak selaras
    if (trend_dxy   == "bullish" and
        trend_us10y == "bullish" and
        trend_xau   == "bearish"):
        new_bias = "SELL"

    elif (trend_dxy   == "bearish" and
          trend_us10y == "bearish" and
          trend_xau   == "bullish"):
        new_bias = "BUY"

    else:
        new_bias = "MIXED"

    last_bias = state.get("last_bias")

    # ── Kirim notif hanya jika bias BERUBAH ─────
    if new_bias != last_bias:
        dxy_icon   = "📈" if trend_dxy   == "bullish" else "📉"
        xau_icon   = "📈" if trend_xau   == "bullish" else "📉"
        us10y_icon = "📈" if trend_us10y == "bullish" else "📉"

        if new_bias == "SELL":
            header = "🔴 <b>BIAS BERUBAH: SELL</b>"
            footer = ("⚠️ <b>Kondisi SELL terpenuhi</b>\n"
                      "DXY ↑ + US10Y ↑ + XAUUSD ↓\n"
                      "Waspadai peluang <b>SHORT XAUUSD</b>")
        elif new_bias == "BUY":
            header = "🟢 <b>BIAS BERUBAH: BUY</b>"
            footer = ("⚠️ <b>Kondisi BUY terpenuhi</b>\n"
                      "DXY ↓ + US10Y ↓ + XAUUSD ↑\n"
                      "Waspadai peluang <b>LONG XAUUSD</b>")
        else:
            header = "⚪ <b>BIAS BERUBAH: MIXED</b>"
            footer = ("Sinyal <b>tidak selaras</b> antar market.\n"
                      "Hindari entry sampai ada konfirmasi penuh.")

        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

        msg = (
            f"{header}\n\n"
            f"{dxy_icon}  DXY (1H)         : <b>{trend_dxy.upper()}</b>  "
            f"[via EUR/USD inverse]\n"
            f"{us10y_icon}  US10Y yield (4H) : <b>{trend_us10y.upper()}</b>  "
            f"[via TLT inverse]\n"
            f"{xau_icon}  XAUUSD (1H)      : <b>{trend_xau.upper()}</b>  "
            f"[MA{MA_FAST}/MA{MA_SLOW}]\n\n"
            f"{footer}\n\n"
            f"🕐 {now_str}"
        )
        send_telegram(msg)
        log.info(f"Bias: {last_bias} → {new_bias}")

        state["last_bias"] = new_bias
        save_state(state)
    else:
        log.info(f"Bias tidak berubah: {new_bias}")

# ═══════════════════════════════════════════════
#  BBR CHECK — Jalankan setiap M5 candle close
# ═══════════════════════════════════════════════
def run_bbr_check():
    log.info("── Menjalankan pengecekan BBR ──")
    state = load_state()

    df = fetch_data_twelvedata("XAU/USD", "5min", 100)
    if df is None:
        return

    df["ATR"]             = calc_atr(df, ATR_LEN)
    df["HH"]              = df["High"].rolling(BASE_LEN).max()
    df["LL"]              = df["Low"].rolling(BASE_LEN).min()
    df["Range"]           = df["HH"] - df["LL"]
    df["IsConsolidating"] = df["Range"] <= (df["ATR"] * BASE_MAX_ATR)
    df.dropna(inplace=True)

    if len(df) < 3:
        return

    bar1      = df.iloc[-2]
    bar2      = df.iloc[-3]
    bar1_time = str(df.index[-2])
    bar1_idx  = len(df) - 2

    if state["last_bar_time"] == bar1_time:
        log.info(f"Bar {bar1_time} sudah diproses, skip.")
        return

    was_consolidating = bool(bar2["IsConsolidating"])
    hH2  = round(float(bar2["HH"]),  2)
    lL2  = round(float(bar2["LL"]),  2)
    atr1 = round(float(bar1["ATR"]), 2)

    log.info(
        f"Bar: {bar1_time} | Close: {bar1['Close']:.2f} | "
        f"wasConsolidating: {was_consolidating} | waiting: {state['waiting']}"
    )

    current_bias = state.get("last_bias", "MIXED")

    # ── BREAKOUT DETECTION ──────────────────────
    if was_consolidating and not state["waiting"]:
        close = round(float(bar1["Close"]), 2)

        if close > hH2:
            state.update({
                "base_high": hH2, "base_low": lL2,
                "base_dir": 1, "break_bar_idx": bar1_idx, "waiting": True
            })
            bias_note = "\n⚠️ Sesuai bias <b>BUY</b> ✅" if current_bias == "BUY" else \
                        "\n⚠️ <i>Berlawanan dengan bias SELL</i>" if current_bias == "SELL" else ""
            send_telegram(
                "🟢 <b>BREAK UP | XAUUSD M5</b>\n"
                f"Base High : <b>{hH2}</b>\n"
                f"Base Low  : <b>{lL2}</b>\n"
                f"ATR       : {atr1}"
                f"{bias_note}\n"
                f"🕐 {bar1_time}"
            )

        elif close < lL2:
            state.update({
                "base_high": hH2, "base_low": lL2,
                "base_dir": -1, "break_bar_idx": bar1_idx, "waiting": True
            })
            bias_note = "\n⚠️ Sesuai bias <b>SELL</b> ✅" if current_bias == "SELL" else \
                        "\n⚠️ <i>Berlawanan dengan bias BUY</i>" if current_bias == "BUY" else ""
            send_telegram(
                "🔴 <b>BREAK DOWN | XAUUSD M5</b>\n"
                f"Base High : <b>{hH2}</b>\n"
                f"Base Low  : <b>{lL2}</b>\n"
                f"ATR       : {atr1}"
                f"{bias_note}\n"
                f"🕐 {bar1_time}"
            )

    # ── RETEST DETECTION ────────────────────────
    elif state["waiting"] and state["break_bar_idx"] is not None:
        bars_since = bar1_idx - state["break_bar_idx"]
        base_high  = state["base_high"]
        base_low   = state["base_low"]
        base_dir   = state["base_dir"]
        bar1_low   = round(float(bar1["Low"]),   2)
        bar1_high  = round(float(bar1["High"]),  2)
        bar1_close = round(float(bar1["Close"]), 2)

        if bars_since > LOOKBACK_RETEST:
            state["waiting"] = False
            log.info(f"Setup expired setelah {bars_since} bar")

        elif base_dir == 1:
            if bar1_low <= base_high and bar1_low >= base_low:
                bias_note = "\n⚠️ Sesuai bias <b>BUY</b> ✅" if current_bias == "BUY" else \
                            "\n⚠️ <i>Berlawanan dengan bias SELL</i>" if current_bias == "SELL" else ""
                send_telegram(
                    "✅ <b>RETEST Bullish | XAUUSD M5</b>\n"
                    "Harga menyentuh zona Base dari atas\n"
                    f"Base High    : <b>{base_high}</b>\n"
                    f"Base Low     : <b>{base_low}</b>\n"
                    f"Low sekarang : <b>{bar1_low}</b>"
                    f"{bias_note}\n"
                    f"🕐 {bar1_time}"
                )
                state["waiting"] = False
            elif bar1_close < base_low:
                state["waiting"] = False

        elif base_dir == -1:
            if bar1_high >= base_low and bar1_high <= base_high:
                bias_note = "\n⚠️ Sesuai bias <b>SELL</b> ✅" if current_bias == "SELL" else \
                            "\n⚠️ <i>Berlawanan dengan bias BUY</i>" if current_bias == "BUY" else ""
                send_telegram(
                    "✅ <b>RETEST Bearish | XAUUSD M5</b>\n"
                    "Harga menyentuh zona Base dari bawah\n"
                    f"Base High     : <b>{base_high}</b>\n"
                    f"Base Low      : <b>{base_low}</b>\n"
                    f"High sekarang : <b>{bar1_high}</b>"
                    f"{bias_note}\n"
                    f"🕐 {bar1_time}"
                )
                state["waiting"] = False
            elif bar1_close > base_high:
                state["waiting"] = False

    state["last_bar_time"] = bar1_time
    save_state(state)

# ═══════════════════════════════════════════════
#  SCHEDULER HELPERS
# ═══════════════════════════════════════════════
def seconds_to_next_candle(candle_seconds: int = 300) -> float:
    now     = datetime.now(timezone.utc).timestamp()
    elapsed = now % candle_seconds
    return candle_seconds - elapsed + 10

def current_hour_key() -> str:
    """String unik per jam UTC, contoh: '2026-05-21-14'"""
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%d-%H")

# ═══════════════════════════════════════════════
#  ENTRY POINT
# ═══════════════════════════════════════════════
if __name__ == "__main__":
    log.info("═══════════════════════════════════════")
    log.info("  BBR Bot — XAUUSD M5 + Bias — Start")
    log.info("═══════════════════════════════════════")

    send_telegram(
        "🤖 <b>BBR Bot aktif</b>\n"
        "Memantau XAUUSD M5 + Market Bias\n\n"
        f"⚙️ Base Length    : {BASE_LEN} bar\n"
        f"⚙️ ATR Multiplier : {BASE_MAX_ATR}×\n"
        f"⚙️ MA Fast/Slow   : MA{MA_FAST} / MA{MA_SLOW}\n\n"
        "📊 <b>Bias Rule:</b>\n"
        "🟢 BUY  = DXY↓ + US10Y↓ + XAUUSD↑\n"
        "🔴 SELL = DXY↑ + US10Y↑ + XAUUSD↓"
    )

    # Jalankan sekali saat start
    run_bbr_check()
    check_bias()

    last_bias_hour = current_hour_key()

    # ── Main Loop ───────────────────────────────
    while True:
        wait = seconds_to_next_candle(300)
        log.info(f"Menunggu {wait:.0f} detik sampai candle berikutnya...")
        time.sleep(wait)

        # BBR check setiap M5
        run_bbr_check()

        # Bias check setiap 1H
        this_hour = current_hour_key()
        if this_hour != last_bias_hour:
            check_bias()
            last_bias_hour = this_hour
