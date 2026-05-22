"""
BBR Bot — Base Break Retest + Market Bias | XAUUSD
Fitur:
- Sinyal BBR (Break & Retest) setiap M5 candle close
- Market Bias (DXY + US10Y + XAUUSD) setiap 1H
- Ringkasan harian jam 07:00 WIB
- Perintah /status dan /bias via Telegram
"""

import os
import json
import time
import logging
import requests
import pandas as pd
from datetime import datetime, timezone, timedelta

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
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN",   "ISI_TOKEN_BOT_KAMU")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "ISI_CHAT_ID_KAMU")
TWELVE_DATA_KEY  = os.environ.get("TWELVE_DATA_KEY",  "ISI_API_KEY_KAMU")

BASE_LEN         = int(os.environ.get("BASE_LEN",        "10"))
BASE_MAX_ATR     = float(os.environ.get("BASE_MAX_ATR",  "2"))
ATR_LEN          = int(os.environ.get("ATR_LEN",         "14"))
LOOKBACK_RETEST  = int(os.environ.get("LOOKBACK_RETEST", "40"))
MA_FAST          = int(os.environ.get("MA_FAST",         "20"))
MA_SLOW          = int(os.environ.get("MA_SLOW",         "50"))

# Ringkasan harian jam 07:00 WIB = 00:00 UTC
DAILY_SUMMARY_HOUR_UTC = int(os.environ.get("DAILY_SUMMARY_HOUR_UTC", "0"))

STATE_FILE = "bbr_state.json"

# ═══════════════════════════════════════════════
#  TELEGRAM — Kirim pesan
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
#  TELEGRAM — Ambil perintah masuk (/status, /bias)
# ═══════════════════════════════════════════════
def get_telegram_updates(offset: int = 0) -> list:
    try:
        url  = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
        resp = requests.get(url, params={"offset": offset, "timeout": 2}, timeout=10)
        if resp.status_code == 200:
            return resp.json().get("result", [])
    except Exception:
        pass
    return []

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
        # BBR
        "base_high":       None,
        "base_low":        None,
        "base_dir":        0,
        "break_bar_idx":   -1,
        "waiting":         False,
        "last_bar_time":   None,
        # Bias
        "last_bias":       None,
        "last_bias_hour":  None,
        "trend_dxy":       None,
        "trend_us10y":     None,
        "trend_xau_1h":    None,
        # Summary
        "last_summary_day": None,
        "daily_break_up":   0,
        "daily_break_dn":   0,
        "daily_retest_bull": 0,
        "daily_retest_bear": 0,
        # Telegram update offset
        "tg_offset":        0
    }

def save_state(state: dict):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        log.error(f"Gagal simpan state: {e}")

# ═══════════════════════════════════════════════
#  FETCH DATA
# ═══════════════════════════════════════════════
def fetch_data_twelvedata(symbol: str, interval: str,
                           outputsize: int = 100, retries: int = 3) -> pd.DataFrame | None:
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(
                "https://api.twelvedata.com/time_series",
                params={
                    "symbol":     symbol,
                    "interval":   interval,
                    "outputsize": outputsize,
                    "apikey":     TWELVE_DATA_KEY,
                    "format":     "JSON"
                },
                timeout=15
            )
            data = resp.json()

            if "values" not in data:
                log.error(f"[{symbol}] Error: {data.get('message', data)}")
                return None

            df = pd.DataFrame(data["values"])
            df = df.rename(columns={
                "datetime": "Datetime",
                "open": "Open", "high": "High",
                "low":  "Low",  "close": "Close"
            })
            df["Datetime"] = pd.to_datetime(df["Datetime"])
            df = df.set_index("Datetime").sort_index()
            df = df[["Open", "High", "Low", "Close"]].astype(float)
            df.dropna(inplace=True)
            return df

        except Exception as e:
            log.warning(f"[{symbol}] Attempt {attempt}/{retries} gagal: {e}")
            if attempt < retries:
                time.sleep(3 * attempt)

    log.error(f"[{symbol}] Semua {retries} percobaan gagal")
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
    if len(df) < MA_SLOW + 2:
        return "unknown"
    close   = df["Close"]
    ma_fast = close.rolling(MA_FAST).mean()
    ma_slow = close.rolling(MA_SLOW).mean()
    v_fast  = ma_fast.iloc[-2]
    v_slow  = ma_slow.iloc[-2]
    if pd.isna(v_fast) or pd.isna(v_slow):
        return "unknown"
    return "bullish" if v_fast > v_slow else "bearish"

def invert_trend(trend: str) -> str:
    if trend == "bullish": return "bearish"
    if trend == "bearish": return "bullish"
    return "unknown"

def trend_icon(trend: str) -> str:
    return "📈" if trend == "bullish" else ("📉" if trend == "bearish" else "❓")

# ═══════════════════════════════════════════════
#  MARKET BIAS CHECK — setiap 1H
# ═══════════════════════════════════════════════
def check_bias(state: dict, force_notify: bool = False):
    log.info("── Mengecek Market Bias ──")

    # Proxy:
    # DXY   → EUR/USD inverse (EUR/USD bear = DXY bull)
    # US10Y → IEF inverse    (IEF bear = yield naik = US10Y bull)
    # XAUUSD → langsung

    df_eurusd = fetch_data_twelvedata("EUR/USD", "1h",  60)
    time.sleep(2)
    df_xau    = fetch_data_twelvedata("XAU/USD", "1h",  60)
    time.sleep(2)
    df_ief    = fetch_data_twelvedata("IEF",     "4h",  60)

    if df_eurusd is None or df_xau is None or df_ief is None:
        log.warning("Data bias tidak lengkap, skip.")
        return state

    trend_dxy   = invert_trend(get_trend(df_eurusd))
    trend_xau   = get_trend(df_xau)
    trend_us10y = invert_trend(get_trend(df_ief))

    log.info(f"DXY: {trend_dxy} | XAUUSD: {trend_xau} | US10Y: {trend_us10y}")

    if (trend_dxy == "bullish" and trend_us10y == "bullish" and trend_xau == "bearish"):
        new_bias = "SELL"
    elif (trend_dxy == "bearish" and trend_us10y == "bearish" and trend_xau == "bullish"):
        new_bias = "BUY"
    else:
        new_bias = "MIXED"

    last_bias = state.get("last_bias")

    # Simpan detail trend ke state untuk /status
    state["trend_dxy"]    = trend_dxy
    state["trend_us10y"]  = trend_us10y
    state["trend_xau_1h"] = trend_xau

    if new_bias != last_bias or force_notify:
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

        now_str = datetime.now(timezone(timedelta(hours=7))).strftime("%d %b %Y %H:%M WIB")

        send_telegram(
            f"{header}\n\n"
            f"{trend_icon(trend_dxy)}  DXY (1H)    : <b>{trend_dxy.upper()}</b>  "
            f"[MA{MA_FAST} {'>' if trend_dxy=='bullish' else '<'} MA{MA_SLOW}]\n"
            f"{trend_icon(trend_us10y)}  US10Y (4H)  : <b>{trend_us10y.upper()}</b>  "
            f"[MA{MA_FAST} {'>' if trend_us10y=='bullish' else '<'} MA{MA_SLOW}]\n"
            f"{trend_icon(trend_xau)}  XAUUSD (1H) : <b>{trend_xau.upper()}</b>  "
            f"[MA{MA_FAST} {'>' if trend_xau=='bullish' else '<'} MA{MA_SLOW}]\n\n"
            f"{footer}\n\n"
            f"🕐 {now_str}"
        )
        log.info(f"Bias: {last_bias} → {new_bias}")
        state["last_bias"] = new_bias

    return state

# ═══════════════════════════════════════════════
#  BBR CHECK — setiap M5 candle close
# ═══════════════════════════════════════════════
def run_bbr_check(state: dict) -> dict:
    log.info("── Menjalankan pengecekan BBR ──")

    df = fetch_data_twelvedata("XAU/USD", "5min", 100)
    if df is None:
        return state

    df["ATR"]             = calc_atr(df, ATR_LEN)
    df["HH"]              = df["High"].rolling(BASE_LEN).max()
    df["LL"]              = df["Low"].rolling(BASE_LEN).min()
    df["Range"]           = df["HH"] - df["LL"]
    df["IsConsolidating"] = df["Range"] <= (df["ATR"] * BASE_MAX_ATR)
    df.dropna(inplace=True)

    if len(df) < 3:
        return state

    bar1      = df.iloc[-2]
    bar2      = df.iloc[-3]
    bar1_time = str(df.index[-2])
    bar1_idx  = len(df) - 2

    if state["last_bar_time"] == bar1_time:
        log.info(f"Bar {bar1_time} sudah diproses, skip.")
        return state

    was_consolidating = bool(bar2["IsConsolidating"])
    hH2  = round(float(bar2["HH"]),  2)
    lL2  = round(float(bar2["LL"]),  2)
    atr1 = round(float(bar1["ATR"]), 2)

    log.info(
        f"Bar: {bar1_time} | Close: {bar1['Close']:.2f} | "
        f"Consolidating: {was_consolidating} | Waiting: {state['waiting']}"
    )

    current_bias = state.get("last_bias", "MIXED")
    now_wib = datetime.now(timezone(timedelta(hours=7))).strftime("%d %b %Y %H:%M WIB")

    def bias_note(expected: str) -> str:
        if current_bias == expected:
            return f"\n✅ Sesuai bias <b>{expected}</b>"
        elif current_bias in ("BUY", "SELL"):
            return f"\n⚠️ <i>Berlawanan dengan bias {current_bias}</i>"
        return ""

    # ── BREAKOUT ────────────────────────────────
    if was_consolidating and not state["waiting"]:
        close = round(float(bar1["Close"]), 2)

        if close > hH2:
            state.update({
                "base_high": hH2, "base_low": lL2,
                "base_dir": 1, "break_bar_idx": bar1_idx, "waiting": True
            })
            state["daily_break_up"] = state.get("daily_break_up", 0) + 1
            send_telegram(
                "🟢 <b>BREAK UP | XAUUSD M5</b>\n"
                f"Base High : <b>{hH2}</b>\n"
                f"Base Low  : <b>{lL2}</b>\n"
                f"ATR       : {atr1}"
                f"{bias_note('BUY')}\n"
                f"🕐 {now_wib}"
            )

        elif close < lL2:
            state.update({
                "base_high": hH2, "base_low": lL2,
                "base_dir": -1, "break_bar_idx": bar1_idx, "waiting": True
            })
            state["daily_break_dn"] = state.get("daily_break_dn", 0) + 1
            send_telegram(
                "🔴 <b>BREAK DOWN | XAUUSD M5</b>\n"
                f"Base High : <b>{hH2}</b>\n"
                f"Base Low  : <b>{lL2}</b>\n"
                f"ATR       : {atr1}"
                f"{bias_note('SELL')}\n"
                f"🕐 {now_wib}"
            )

    # ── RETEST ──────────────────────────────────
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
                state["daily_retest_bull"] = state.get("daily_retest_bull", 0) + 1
                send_telegram(
                    "✅ <b>RETEST Bullish | XAUUSD M5</b>\n"
                    "Harga menyentuh zona Base dari atas\n"
                    f"Base High    : <b>{base_high}</b>\n"
                    f"Base Low     : <b>{base_low}</b>\n"
                    f"Low sekarang : <b>{bar1_low}</b>"
                    f"{bias_note('BUY')}\n"
                    f"🕐 {now_wib}"
                )
                state["waiting"] = False
            elif bar1_close < base_low:
                state["waiting"] = False

        elif base_dir == -1:
            if bar1_high >= base_low and bar1_high <= base_high:
                state["daily_retest_bear"] = state.get("daily_retest_bear", 0) + 1
                send_telegram(
                    "✅ <b>RETEST Bearish | XAUUSD M5</b>\n"
                    "Harga menyentuh zona Base dari bawah\n"
                    f"Base High     : <b>{base_high}</b>\n"
                    f"Base Low      : <b>{base_low}</b>\n"
                    f"High sekarang : <b>{bar1_high}</b>"
                    f"{bias_note('SELL')}\n"
                    f"🕐 {now_wib}"
                )
                state["waiting"] = False
            elif bar1_close > base_high:
                state["waiting"] = False

    state["last_bar_time"] = bar1_time
    return state

# ═══════════════════════════════════════════════
#  RINGKASAN HARIAN — jam 07:00 WIB
# ═══════════════════════════════════════════════
def send_daily_summary(state: dict) -> dict:
    today_wib = datetime.now(timezone(timedelta(hours=7))).strftime("%d %b %Y")
    last_day  = state.get("last_summary_day")

    if last_day == today_wib:
        return state

    b_up    = state.get("daily_break_up",    0)
    b_dn    = state.get("daily_break_dn",    0)
    r_bull  = state.get("daily_retest_bull", 0)
    r_bear  = state.get("daily_retest_bear", 0)
    bias    = state.get("last_bias", "MIXED")

    bias_icon = "🔴" if bias == "SELL" else ("🟢" if bias == "BUY" else "⚪")

    send_telegram(
        f"📋 <b>Ringkasan Harian — {today_wib}</b>\n\n"
        f"Bias terakhir : {bias_icon} <b>{bias}</b>\n\n"
        f"🟢 Break Up      : {b_up}x\n"
        f"🔴 Break Down    : {b_dn}x\n"
        f"✅ Retest Bullish: {r_bull}x\n"
        f"✅ Retest Bearish: {r_bear}x\n"
        f"📊 Total Sinyal  : {b_up + b_dn + r_bull + r_bear}x\n\n"
        f"🕐 Update berikutnya: besok 07:00 WIB"
    )
    log.info("Ringkasan harian terkirim")

    # Reset counter harian
    state.update({
        "last_summary_day":  today_wib,
        "daily_break_up":    0,
        "daily_break_dn":    0,
        "daily_retest_bull": 0,
        "daily_retest_bear": 0
    })
    return state

# ═══════════════════════════════════════════════
#  HANDLE PERINTAH TELEGRAM (/status, /bias)
# ═══════════════════════════════════════════════
def handle_commands(state: dict) -> dict:
    offset   = state.get("tg_offset", 0)
    updates  = get_telegram_updates(offset)

    for update in updates:
        update_id = update.get("update_id", 0)
        state["tg_offset"] = update_id + 1

        msg  = update.get("message", {})
        text = msg.get("text", "").strip().lower()

        if not text:
            continue

        log.info(f"Perintah masuk: {text}")

        # ── /status ─────────────────────────────
        if text.startswith("/status"):
            bias       = state.get("last_bias",    "Belum diketahui")
            trend_dxy  = state.get("trend_dxy",    "?")
            trend_10y  = state.get("trend_us10y",  "?")
            trend_xau  = state.get("trend_xau_1h", "?")
            waiting    = state.get("waiting",      False)
            b_high     = state.get("base_high",    "-")
            b_low      = state.get("base_low",     "-")
            b_dir_raw  = state.get("base_dir",     0)
            b_dir      = "UP ⬆️" if b_dir_raw == 1 else ("DOWN ⬇️" if b_dir_raw == -1 else "-")
            now_wib    = datetime.now(timezone(timedelta(hours=7))).strftime("%d %b %Y %H:%M WIB")

            bias_icon  = "🔴" if bias == "SELL" else ("🟢" if bias == "BUY" else "⚪")

            send_telegram(
                f"📡 <b>Status Bot — {now_wib}</b>\n\n"
                f"<b>Market Bias</b>\n"
                f"{bias_icon} Bias saat ini : <b>{bias}</b>\n"
                f"{trend_icon(trend_dxy)}  DXY (1H)     : {trend_dxy.upper()}\n"
                f"{trend_icon(trend_10y)}  US10Y (4H)   : {trend_10y.upper()}\n"
                f"{trend_icon(trend_xau)}  XAUUSD (1H)  : {trend_xau.upper()}\n\n"
                f"<b>BBR State</b>\n"
                f"Menunggu Retest : {'✅ Ya' if waiting else '❌ Tidak'}\n"
                f"Arah Break      : {b_dir}\n"
                f"Base High       : {b_high}\n"
                f"Base Low        : {b_low}\n\n"
                f"<b>Sinyal Hari Ini</b>\n"
                f"🟢 Break Up       : {state.get('daily_break_up', 0)}x\n"
                f"🔴 Break Down     : {state.get('daily_break_dn', 0)}x\n"
                f"✅ Retest Bullish : {state.get('daily_retest_bull', 0)}x\n"
                f"✅ Retest Bearish : {state.get('daily_retest_bear', 0)}x"
            )

        # ── /bias ────────────────────────────────
        elif text.startswith("/bias"):
            send_telegram("🔄 Mengecek bias market, tunggu sebentar...")
            state = check_bias(state, force_notify=True)

        # ── /help ────────────────────────────────
        elif text.startswith("/help"):
            send_telegram(
                "🤖 <b>BBR Bot — Daftar Perintah</b>\n\n"
                "/status — Lihat kondisi bias dan BBR saat ini\n"
                "/bias   — Paksa cek dan tampilkan bias sekarang\n"
                "/help   — Tampilkan pesan ini"
            )

    return state

# ═══════════════════════════════════════════════
#  SCHEDULER HELPERS
# ═══════════════════════════════════════════════
def seconds_to_next_candle(candle_seconds: int = 300) -> float:
    now     = datetime.now(timezone.utc).timestamp()
    elapsed = now % candle_seconds
    return candle_seconds - elapsed + 10

def current_hour_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d-%H")

def is_daily_summary_time() -> bool:
    now_utc = datetime.now(timezone.utc)
    return now_utc.hour == DAILY_SUMMARY_HOUR_UTC and now_utc.minute < 10

# ═══════════════════════════════════════════════
#  ENTRY POINT
# ═══════════════════════════════════════════════
if __name__ == "__main__":
    log.info("═══════════════════════════════════════")
    log.info("  BBR Bot — XAUUSD M5 + Bias — Start  ")
    log.info("═══════════════════════════════════════")

    send_telegram(
        "🤖 <b>BBR Bot aktif</b>\n"
        "Memantau XAUUSD M5 + Market Bias\n\n"
        f"⚙️ Base Length    : {BASE_LEN} bar\n"
        f"⚙️ ATR Multiplier : {BASE_MAX_ATR}×\n"
        f"⚙️ MA Fast/Slow   : MA{MA_FAST} / MA{MA_SLOW}\n\n"
        "📊 <b>Bias Rule:</b>\n"
        "🟢 BUY  = DXY↓ + US10Y↓ + XAUUSD↑\n"
        "🔴 SELL = DXY↑ + US10Y↑ + XAUUSD↓\n\n"
        "📋 Ringkasan harian: setiap 07:00 WIB\n"
        "💬 Ketik /help untuk daftar perintah"
    )

    state = load_state()

    # Jalankan sekali saat start
    state = run_bbr_check(state)
    state = check_bias(state)
    save_state(state)

    last_bias_hour = current_hour_key()

    # ── Main Loop ───────────────────────────────
    while True:
        # Cek perintah Telegram masuk
        state = handle_commands(state)
        save_state(state)

        # Tunggu sampai M5 candle berikutnya
        wait = seconds_to_next_candle(300)
        log.info(f"Menunggu {wait:.0f} detik sampai candle berikutnya...")
        time.sleep(wait)

        # BBR check setiap M5
        state = run_bbr_check(state)

        # Bias check setiap 1H
        this_hour = current_hour_key()
        if this_hour != last_bias_hour:
            state = check_bias(state)
            last_bias_hour = this_hour

        # Ringkasan harian jam 07:00 WIB
        if is_daily_summary_time():
            state = send_daily_summary(state)

        save_state(state)
