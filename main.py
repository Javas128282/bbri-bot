"""
RNM Fade Bot v2 — Round-Number Magnet Fade | XAUUSD
Strategi tervalidasi: IS 2010-2019 | OOS 2020-2026
- WR 62.4% | PF 1.72 | Calmar 13.30 | Sharpe 1.557
- EMA50 H1 + slope → bias long/short/neutral (skip neutral)
- ATR H1 < $11 (bukan ATR M5)
- RSI(2) ≥ 90 / ≤ 10 di candle M5 close
- SL = ujung wick + $1.5
- TP1 = 0.5×ATR H1, TP2 = 1.0×ATR H1 (partial 50/50)
- Time stop: 20 menit jika belum TP1
"""

import os
import json
import math
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
#  CONFIG — semua via environment variable
# ═══════════════════════════════════════════════
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN",   "ISI_TOKEN_BOT_KAMU")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "ISI_CHAT_ID_KAMU")
TWELVE_DATA_KEY  = os.environ.get("TWELVE_DATA_KEY",  "ISI_API_KEY_KAMU")

# RSI
RSI_PERIOD   = int(os.environ.get("RSI_PERIOD",   "2"))
RSI_OB       = float(os.environ.get("RSI_OB",     "90"))   # overbought → SELL
RSI_OS       = float(os.environ.get("RSI_OS",     "10"))   # oversold  → BUY

# EMA50 H1 (bias filter)
EMA_PERIOD   = int(os.environ.get("EMA_PERIOD",   "50"))
EMA_SLOPE_BARS = int(os.environ.get("EMA_SLOPE_BARS", "5"))  # lookback slope
EMA_BAND_PCT = float(os.environ.get("EMA_BAND_PCT", "0.001")) # 0.1% neutral band

# ATR H1 (volatility gate — BUKAN ATR M5)
ATR_PERIOD   = int(os.environ.get("ATR_PERIOD",   "14"))
ATR_MAX_H1   = float(os.environ.get("ATR_MAX_H1", "11.0"))  # skip jika ATR H1 >= $11

# Level $10
LEVEL_STEP   = float(os.environ.get("LEVEL_STEP",  "10"))
LEVEL_RANGE  = int(os.environ.get("LEVEL_RANGE",   "4"))    # garis di atas & bawah

# SL / TP (berbasis ATR H1)
SL_BUFFER    = float(os.environ.get("SL_BUFFER",   "1.5"))  # wick + buffer
TP1_ATR_MULT = float(os.environ.get("TP1_ATR_MULT","0.5"))  # TP1 = 0.5 × ATR H1
TP2_ATR_MULT = float(os.environ.get("TP2_ATR_MULT","1.0"))  # TP2 = 1.0 × ATR H1
TIME_STOP_MIN= int(os.environ.get("TIME_STOP_MIN", "20"))   # menit, info saja

# Wick minimum: wick harus keluar dari level minimal sebesar ini
WICK_MIN     = float(os.environ.get("WICK_MIN",    "0.30"))

# Anti-spam cooldown
COOLDOWN_BARS = int(os.environ.get("COOLDOWN_BARS","6"))

# Big H1 candle filter (anti breakout asli)
BIG_CANDLE_MULT = float(os.environ.get("BIG_CANDLE_MULT", "2.5"))
BIG_CANDLE_LOOK = int(os.environ.get("BIG_CANDLE_LOOK",   "20"))

# Sesi aktif UTC
SESSION_START = int(os.environ.get("SESSION_START", "7"))
SESSION_END   = int(os.environ.get("SESSION_END",   "21"))

# News blackout manual (jam UTC dipisah koma, misal "13:30,20:00")
NEWS_TIMES_UTC  = os.environ.get("NEWS_TIMES_UTC", "")
NEWS_BUFFER_MIN = int(os.environ.get("NEWS_BUFFER_MIN", "30"))

# Ringkasan harian jam 07:00 WIB = 00:00 UTC
DAILY_SUMMARY_HOUR_UTC = int(os.environ.get("DAILY_SUMMARY_HOUR_UTC", "0"))

STATE_FILE = "rnm_state_v2.json"

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
            log.info(f"✅ TG sent: {message[:60].strip()}")
            return True
        log.error(f"❌ TG {resp.status_code}: {resp.text}")
        return False
    except Exception as e:
        log.error(f"❌ TG exception: {e}")
        return False

def get_updates(offset: int = 0) -> list:
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
            with open(STATE_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "last_bar_time":        None,
        "bar_counter":          0,
        "last_alert_key":       None,
        "last_alert_bar":       -999,
        # Snapshot H1
        "h1_bias":              None,
        "h1_close":             None,
        "h1_ema50":             None,
        "h1_atr":               None,
        # Snapshot M5
        "last_close":           None,
        "last_rsi2":            None,
        "last_levels":          [],
        # Counter harian
        "daily_buy":            0,
        "daily_sell":           0,
        "daily_skip_atr":       0,
        "daily_skip_bias":      0,
        "daily_skip_session":   0,
        "daily_skip_bigcandle": 0,
        "last_summary_day":     None,
        # Telegram offset
        "tg_offset":            0,
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
def fetch(symbol: str, interval: str, size: int = 120, retries: int = 3) -> pd.DataFrame | None:
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(
                "https://api.twelvedata.com/time_series",
                params={"symbol": symbol, "interval": interval,
                        "outputsize": size, "apikey": TWELVE_DATA_KEY, "format": "JSON"},
                timeout=15
            )
            data = resp.json()
            if "values" not in data:
                log.error(f"[{symbol}/{interval}] {data.get('message', data)}")
                return None
            df = pd.DataFrame(data["values"])
            df = df.rename(columns={"datetime":"Datetime","open":"Open",
                                     "high":"High","low":"Low","close":"Close"})
            df["Datetime"] = pd.to_datetime(df["Datetime"])
            df = df.set_index("Datetime").sort_index()
            df = df[["Open","High","Low","Close"]].astype(float)
            df.dropna(inplace=True)
            return df
        except Exception as e:
            log.warning(f"[{symbol}/{interval}] attempt {attempt}: {e}")
            if attempt < retries:
                time.sleep(3 * attempt)
    return None

# ═══════════════════════════════════════════════
#  INDIKATOR
# ═══════════════════════════════════════════════
def calc_ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()

def calc_atr(df: pd.DataFrame, period: int) -> pd.Series:
    h, l, c = df["High"], df["Low"], df["Close"]
    tr = pd.concat([h-l, (h-c.shift()).abs(), (l-c.shift()).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1/period, adjust=False).mean()

def calc_rsi(series: pd.Series, period: int) -> pd.Series:
    d = series.diff()
    g = d.clip(lower=0).ewm(alpha=1/period, adjust=False).mean()
    l = (-d).clip(lower=0).ewm(alpha=1/period, adjust=False).mean()
    return 100 - 100/(1 + g/l.replace(0, 1e-10))

def bias_icon(bias: str) -> str:
    return {"long":"📈", "short":"📉", "neutral":"↔️"}.get(bias, "❓")

def find_levels(price: float) -> list:
    base = math.floor(price / LEVEL_STEP) * LEVEL_STEP
    return sorted(round(base + i*LEVEL_STEP, 2) for i in range(-LEVEL_RANGE, LEVEL_RANGE+1))

# ═══════════════════════════════════════════════
#  H1 DATA — bias + ATR + big candle check
# ═══════════════════════════════════════════════
def get_h1_data(state: dict) -> dict | None:
    """
    Ambil H1, hitung:
    - EMA50 + slope → bias (long / short / neutral)
    - ATR(14) H1 → gate volatilitas
    - Big candle flag → anti breakout asli
    Return dict atau None kalau data tidak cukup.
    """
    need = EMA_PERIOD + EMA_SLOPE_BARS + 5
    df = fetch("XAU/USD", "1h", max(need, 80))
    if df is None or len(df) < need:
        log.warning("H1 data tidak cukup")
        return None

    df["EMA50"] = calc_ema(df["Close"], EMA_PERIOD)
    df["ATR14"] = calc_atr(df, ATR_PERIOD)
    df["Body"]  = (df["Close"] - df["Open"]).abs()
    df["AvgBody"] = df["Body"].rolling(BIG_CANDLE_LOOK, min_periods=5).mean()
    df.dropna(inplace=True)

    if len(df) < EMA_SLOPE_BARS + 2:
        return None

    # Gunakan candle H1 terakhir yang sudah CLOSE (iloc[-2])
    last  = df.iloc[-2]
    prev  = df.iloc[-(EMA_SLOPE_BARS+2)]   # untuk slope

    close  = float(last["Close"])
    ema50  = float(last["EMA50"])
    atr14  = float(last["ATR14"])
    slope  = float(last["EMA50"]) - float(prev["EMA50"])
    band   = ema50 * EMA_BAND_PCT

    # Bias
    if close > ema50 + band and slope > 0:
        bias = "long"
    elif close < ema50 - band and slope < 0:
        bias = "short"
    else:
        bias = "neutral"

    # Big candle flag (untuk filter anti-breakout)
    big_bull = (last["Body"] > last["AvgBody"] * BIG_CANDLE_MULT) and (last["Close"] > last["Open"])
    big_bear = (last["Body"] > last["AvgBody"] * BIG_CANDLE_MULT) and (last["Close"] < last["Open"])

    return {
        "close":    round(close, 2),
        "ema50":    round(ema50, 2),
        "atr14":    round(atr14, 2),
        "slope":    round(slope, 4),
        "bias":     bias,
        "big_bull": big_bull,
        "big_bear": big_bear,
    }

# ═══════════════════════════════════════════════
#  FILTER HELPERS
# ═══════════════════════════════════════════════
def is_active_session() -> bool:
    h = datetime.now(timezone.utc).hour
    return SESSION_START <= h < SESSION_END

def is_news_blackout() -> bool:
    if not NEWS_TIMES_UTC.strip():
        return False
    now = datetime.now(timezone.utc)
    for t in NEWS_TIMES_UTC.split(","):
        t = t.strip()
        if not t:
            continue
        try:
            hh, mm = map(int, t.split(":"))
            nt   = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            diff = abs((now - nt).total_seconds()) / 60
            if diff <= NEWS_BUFFER_MIN:
                return True
        except Exception:
            continue
    return False

# ═══════════════════════════════════════════════
#  MAIN SIGNAL CHECK (dipanggil setiap candle M5)
# ═══════════════════════════════════════════════
def run_check(state: dict) -> dict:
    log.info("── M5 check ──")

    # 1. Ambil H1 data
    h1 = get_h1_data(state)
    if h1 is None:
        return state

    bias   = h1["bias"]
    h1_atr = h1["atr14"]

    # Update snapshot H1
    old_bias = state.get("h1_bias")
    state.update({"h1_bias": bias, "h1_close": h1["close"],
                  "h1_ema50": h1["ema50"], "h1_atr": h1_atr})

    # Notifikasi kalau bias H1 berubah
    if old_bias and old_bias != bias:
        now_wib = _wib_now()
        send_telegram(
            f"🔄 <b>Bias H1 Berubah</b>\n\n"
            f"{bias_icon(old_bias)} {old_bias.upper()}  →  {bias_icon(bias)} <b>{bias.upper()}</b>\n\n"
            f"Close H1 : {h1['close']}\n"
            f"EMA50    : {h1['ema50']}\n"
            f"Slope    : {h1['slope']:+.2f}\n"
            f"ATR H1   : {h1_atr}\n\n"
            f"🕐 {now_wib}"
        )

    # 2. Filter ATR H1
    if h1_atr >= ATR_MAX_H1:
        log.info(f"Skip: ATR H1 {h1_atr} ≥ {ATR_MAX_H1}")
        state["daily_skip_atr"] = state.get("daily_skip_atr", 0) + 1
        return state

    # 3. Filter bias neutral — skip semua sinyal
    if bias == "neutral":
        log.info("Skip: bias H1 neutral")
        state["daily_skip_bias"] = state.get("daily_skip_bias", 0) + 1
        return state

    # 4. Ambil M5 data
    df = fetch("XAU/USD", "5min", 120)
    if df is None or len(df) < 10:
        return state

    df["RSI2"] = calc_rsi(df["Close"], RSI_PERIOD)
    df.dropna(inplace=True)

    bar      = df.iloc[-2]       # candle M5 terakhir yang sudah CLOSE
    bar_time = str(df.index[-2])

    if state["last_bar_time"] == bar_time:
        log.info(f"Bar {bar_time} sudah diproses, skip")
        return state

    state["bar_counter"] = state.get("bar_counter", 0) + 1
    bar_idx = state["bar_counter"]

    close = round(float(bar["Close"]), 2)
    high  = round(float(bar["High"]),  2)
    low   = round(float(bar["Low"]),   2)
    rsi   = round(float(bar["RSI2"]),  1)

    levels = find_levels(close)
    state.update({"last_close": close, "last_rsi2": rsi,
                  "last_levels": levels, "last_bar_time": bar_time})

    log.info(f"Bar: {bar_time} | Close:{close} | RSI2:{rsi} | "
             f"Bias:{bias} | ATR H1:{h1_atr}")

    # 5. Filter sesi & news
    if not is_active_session():
        log.info("Skip: luar sesi")
        state["daily_skip_session"] = state.get("daily_skip_session", 0) + 1
        return state

    if is_news_blackout():
        log.info("Skip: news blackout")
        state["daily_skip_session"] = state.get("daily_skip_session", 0) + 1
        return state

    # 6. Deteksi sinyal fade di level $10
    direction = None
    level_hit = None
    wick_extreme = None  # ujung wick untuk SL

    for lvl in levels:
        # ── SELL: wick sweep di atas level, close di bawah level ───────────
        if (rsi >= RSI_OB
                and high > lvl + WICK_MIN      # wick tembus atas level
                and close < lvl                # close kembali ke bawah
                and bias != "long"):           # jangan fade melawan uptrend kuat
            # Filter big bull candle H1 (anti breakout asli)
            if h1["big_bull"]:
                log.info(f"Skip SELL: big bull H1 candle di level {lvl}")
                state["daily_skip_bigcandle"] = state.get("daily_skip_bigcandle", 0) + 1
                continue
            direction    = "SELL"
            level_hit    = lvl
            wick_extreme = high
            break

        # ── BUY: wick sweep di bawah level, close di atas level ────────────
        if (rsi <= RSI_OS
                and low < lvl - WICK_MIN       # wick tembus bawah level
                and close > lvl                # close kembali ke atas
                and bias != "short"):          # jangan fade melawan downtrend kuat
            # Filter big bear candle H1
            if h1["big_bear"]:
                log.info(f"Skip BUY: big bear H1 candle di level {lvl}")
                state["daily_skip_bigcandle"] = state.get("daily_skip_bigcandle", 0) + 1
                continue
            direction    = "BUY"
            level_hit    = lvl
            wick_extreme = low
            break

    if direction is None:
        return state

    # 7. Anti-spam cooldown
    alert_key = f"{direction}_{level_hit}"
    if (state.get("last_alert_key") == alert_key
            and bar_idx - state.get("last_alert_bar", -999) < COOLDOWN_BARS):
        log.info(f"Skip cooldown: {alert_key}")
        return state

    # 8. Hitung SL / TP1 / TP2
    if direction == "SELL":
        sl  = round(wick_extreme + SL_BUFFER, 2)
        tp1 = round(close - h1_atr * TP1_ATR_MULT, 2)
        tp2 = round(close - h1_atr * TP2_ATR_MULT, 2)
        risk = round(sl - close, 2)
        rr1  = round((close - tp1) / risk, 2) if risk > 0 else 0
        rr2  = round((close - tp2) / risk, 2) if risk > 0 else 0
        state["daily_sell"] = state.get("daily_sell", 0) + 1
        header = "🔴 <b>SELL — Round-Number Magnet Fade</b>"
    else:
        sl  = round(wick_extreme - SL_BUFFER, 2)
        tp1 = round(close + h1_atr * TP1_ATR_MULT, 2)
        tp2 = round(close + h1_atr * TP2_ATR_MULT, 2)
        risk = round(close - sl, 2)
        rr1  = round((tp1 - close) / risk, 2) if risk > 0 else 0
        rr2  = round((tp2 - close) / risk, 2) if risk > 0 else 0
        state["daily_buy"] = state.get("daily_buy", 0) + 1
        header = "🟢 <b>BUY — Round-Number Magnet Fade</b>"

    now_wib = _wib_now()

    send_telegram(
        f"{header}\n\n"
        f"{'─'*30}\n"
        f"Level Magnet : <b>${level_hit}</b>\n"
        f"Entry        : <b>{close}</b>\n"
        f"SL           : <b>{sl}</b>  (risk: {risk} pts)\n\n"
        f"TP1 (50%)    : <b>{tp1}</b>  → RR 1:{rr1}\n"
        f"TP2 (50%)    : <b>{tp2}</b>  → RR 1:{rr2}\n"
        f"Time stop    : <b>{TIME_STOP_MIN} menit</b> jika belum TP1\n\n"
        f"{'─'*30}\n"
        f"RSI(2) M5    : {rsi}\n"
        f"ATR H1       : {h1_atr}\n"
        f"Bias H1      : {bias_icon(bias)} {bias}\n"
        f"EMA50 H1     : {h1['ema50']}\n\n"
        f"⚠️ <i>Partial close: tutup 50% saat TP1, biarkan 50% ke TP2</i>\n"
        f"⚠️ <i>Bukan rekomendasi investasi</i>\n\n"
        f"🕐 {now_wib}"
    )

    state["last_alert_key"] = alert_key
    state["last_alert_bar"] = bar_idx
    return state

# ═══════════════════════════════════════════════
#  RINGKASAN HARIAN
# ═══════════════════════════════════════════════
def send_daily_summary(state: dict) -> dict:
    today = datetime.now(timezone(timedelta(hours=7))).strftime("%d %b %Y")
    if state.get("last_summary_day") == today:
        return state

    buy      = state.get("daily_buy",            0)
    sell     = state.get("daily_sell",           0)
    s_atr    = state.get("daily_skip_atr",       0)
    s_bias   = state.get("daily_skip_bias",      0)
    s_sess   = state.get("daily_skip_session",   0)
    s_big    = state.get("daily_skip_bigcandle", 0)

    send_telegram(
        f"📋 <b>Ringkasan Harian — {today}</b>\n\n"
        f"🟢 Sinyal BUY  : {buy}x\n"
        f"🔴 Sinyal SELL : {sell}x\n"
        f"📊 Total Sinyal: {buy + sell}x\n\n"
        f"⏭️ Skip ATR H1 ≥ ${ATR_MAX_H1} : {s_atr}x\n"
        f"⏭️ Skip Bias Neutral         : {s_bias}x\n"
        f"⏭️ Skip Sesi/News            : {s_sess}x\n"
        f"⏭️ Skip Big H1 Candle        : {s_big}x\n\n"
        f"🕐 Update berikutnya: besok 07:00 WIB"
    )

    state.update({
        "last_summary_day":     today,
        "daily_buy":            0,
        "daily_sell":           0,
        "daily_skip_atr":       0,
        "daily_skip_bias":      0,
        "daily_skip_session":   0,
        "daily_skip_bigcandle": 0,
    })
    return state

# ═══════════════════════════════════════════════
#  HANDLE PERINTAH TELEGRAM
# ═══════════════════════════════════════════════
def handle_commands(state: dict) -> dict:
    offset  = state.get("tg_offset", 0)
    updates = get_updates(offset)

    for upd in updates:
        state["tg_offset"] = upd.get("update_id", 0) + 1
        text = upd.get("message", {}).get("text", "").strip().lower()
        if not text:
            continue
        log.info(f"Perintah: {text}")

        if text.startswith("/status"):
            now_wib = _wib_now()
            bias    = state.get("h1_bias", "-")
            send_telegram(
                f"📡 <b>Status Bot — {now_wib}</b>\n\n"
                f"<b>H1</b>\n"
                f"Close  : {state.get('h1_close', '-')}\n"
                f"EMA50  : {state.get('h1_ema50', '-')}\n"
                f"ATR H1 : {state.get('h1_atr', '-')}  (max: ${ATR_MAX_H1})\n"
                f"Bias   : {bias_icon(bias)} <b>{bias}</b>\n\n"
                f"<b>M5 Terakhir</b>\n"
                f"Close  : {state.get('last_close', '-')}\n"
                f"RSI(2) : {state.get('last_rsi2', '-')}\n"
                f"Sesi aktif : {'✅' if is_active_session() else '❌'}\n\n"
                f"<b>Sinyal Hari Ini</b>\n"
                f"🟢 BUY  : {state.get('daily_buy', 0)}x\n"
                f"🔴 SELL : {state.get('daily_sell', 0)}x\n"
                f"⏭️ Skip : ATR={state.get('daily_skip_atr',0)} "
                f"Bias={state.get('daily_skip_bias',0)} "
                f"Sesi={state.get('daily_skip_session',0)} "
                f"BigCandle={state.get('daily_skip_bigcandle',0)}"
            )

        elif text.startswith("/bias"):
            lvls = state.get("last_levels", [])
            bias = state.get("h1_bias", "-")
            send_telegram(
                f"📐 <b>Kondisi Market</b>\n\n"
                f"Bias H1  : {bias_icon(bias)} <b>{bias}</b>\n"
                f"EMA50 H1 : {state.get('h1_ema50', '-')}\n"
                f"ATR H1   : {state.get('h1_atr', '-')}\n\n"
                f"RSI(2) M5 : {state.get('last_rsi2', '-')}\n"
                f"Close M5  : {state.get('last_close', '-')}\n\n"
                f"Level $10 terdekat:\n"
                + "\n".join(f"  • {l}" for l in lvls) if lvls else "  -"
            )

        elif text.startswith("/help"):
            send_telegram(
                "🤖 <b>RNM Fade Bot v2</b>\n\n"
                "<b>Perintah:</b>\n"
                "/status — Snapshot lengkap bot\n"
                "/bias   — Bias H1, ATR, RSI, level terdekat\n"
                "/help   — Pesan ini\n\n"
                "<b>Strategi:</b>\n"
                "• RSI(2) ≥ 90/≤ 10 di M5\n"
                "• Level $10 + wick rejection\n"
                "• Bias H1: EMA50 + slope (skip neutral)\n"
                "• ATR H1 < $11\n"
                "• SL: ujung wick + $1.5\n"
                "• TP1 (50%): 0.5×ATR H1\n"
                "• TP2 (50%): 1.0×ATR H1\n"
                "• Time stop: 20 menit jika belum TP1"
            )

    return state

# ═══════════════════════════════════════════════
#  UTIL
# ═══════════════════════════════════════════════
def _wib_now() -> str:
    return datetime.now(timezone(timedelta(hours=7))).strftime("%d %b %Y %H:%M WIB")

def seconds_to_next_m5() -> float:
    now     = datetime.now(timezone.utc).timestamp()
    elapsed = now % 300
    return 300 - elapsed + 10   # +10 detik buffer agar candle sudah closed di API

def is_daily_summary_time() -> bool:
    n = datetime.now(timezone.utc)
    return n.hour == DAILY_SUMMARY_HOUR_UTC and n.minute < 10

# ═══════════════════════════════════════════════
#  ENTRY POINT
# ═══════════════════════════════════════════════
if __name__ == "__main__":
    log.info("══════════════════════════════════════════")
    log.info("  RNM Fade Bot v2 — XAUUSD M5 — Start   ")
    log.info("══════════════════════════════════════════")

    send_telegram(
        "🤖 <b>RNM Fade Bot v2 — Aktif</b>\n\n"
        "Strategi tervalidasi (IS 2010–2019 | OOS 2020–2026)\n"
        f"WR 62.4% | PF 1.72 | Calmar 13.30 | Sharpe 1.56\n\n"
        f"⚙️ RSI({RSI_PERIOD}) OB/OS : {RSI_OB}/{RSI_OS}\n"
        f"⚙️ EMA{EMA_PERIOD} H1 + slope  : bias filter\n"
        f"⚙️ ATR H1 max    : ${ATR_MAX_H1}\n"
        f"⚙️ Level step    : ${LEVEL_STEP:g}\n"
        f"⚙️ SL buffer     : $  {SL_BUFFER}\n"
        f"⚙️ TP1 / TP2     : {TP1_ATR_MULT}×ATR / {TP2_ATR_MULT}×ATR\n"
        f"⚙️ Partial close : 50% TP1 + 50% TP2\n\n"
        "💬 Ketik /help untuk daftar perintah"
    )

    state = load_state()
    state = run_check(state)
    save_state(state)

    while True:
        state = handle_commands(state)
        save_state(state)

        wait = seconds_to_next_m5()
        log.info(f"Tunggu {wait:.0f} detik ke candle berikutnya...")
        time.sleep(wait)

        state = run_check(state)

        if is_daily_summary_time():
            state = send_daily_summary(state)

        save_state(state)
log = logging.getLogger(__name__)

# ═══════════════════════════════════════════════
#  CONFIG
# ═══════════════════════════════════════════════
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN",   "ISI_TOKEN_BOT_KAMU")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "ISI_CHAT_ID_KAMU")
TWELVE_DATA_KEY  = os.environ.get("TWELVE_DATA_KEY",  "ISI_API_KEY_KAMU")

# Indikator
RSI_PERIOD        = int(os.environ.get("RSI_PERIOD",        "2"))
RSI_OVERBOUGHT     = float(os.environ.get("RSI_OVERBOUGHT",  "90"))
RSI_OVERSOLD       = float(os.environ.get("RSI_OVERSOLD",    "10"))
MA_FAST            = int(os.environ.get("MA_FAST",           "20"))
MA_SLOW            = int(os.environ.get("MA_SLOW",           "50"))
ATR_LEN            = int(os.environ.get("ATR_LEN",           "14"))
ATR_MAX            = float(os.environ.get("ATR_MAX",         "10"))

# Level kelipatan $10
LEVEL_STEP         = float(os.environ.get("LEVEL_STEP",      "10"))
LEVEL_RANGE        = int(os.environ.get("LEVEL_RANGE",       "4"))   # jumlah garis di atas & bawah
REJECTION_MIN_ATR_FRACTION = float(os.environ.get("REJECTION_MIN_ATR_FRACTION", "0.3"))

# Filter momentum H1 (anti breakout asli)
MOMENTUM_LOOKBACK         = int(os.environ.get("MOMENTUM_LOOKBACK", "20"))
MOMENTUM_BODY_MULTIPLIER  = float(os.environ.get("MOMENTUM_BODY_MULTIPLIER", "1.5"))

# Risk management
SL_ATR_MULTIPLIER  = float(os.environ.get("SL_ATR_MULTIPLIER", "1.1"))
RR                 = float(os.environ.get("RR", "2.0"))

# Anti-spam: jangan kirim sinyal sama berulang di bar2 berurutan
COOLDOWN_BARS       = int(os.environ.get("COOLDOWN_BARS", "6"))

# Sideways H1: harga dianggap "menyentuh/dekat MA" atau "di antara MA20 & MA50"
# kalau jaraknya ke salah satu MA <= buffer ini (dalam USD)
H1_TREND_BUFFER     = float(os.environ.get("H1_TREND_BUFFER", "2.0"))

# Heads-up: alert kalau harga mendekati level $10 yang searah trend (sebelum RSI/rejection muncul)
NEAR_LEVEL_USD            = float(os.environ.get("NEAR_LEVEL_USD", "3.0"))
NEAR_LEVEL_COOLDOWN_BARS  = int(os.environ.get("NEAR_LEVEL_COOLDOWN_BARS", "6"))

# Sesi aktif (UTC). Default kira-kira menutupi London + NY.
SESSION_START_UTC  = int(os.environ.get("SESSION_START_UTC", "7"))
SESSION_END_UTC    = int(os.environ.get("SESSION_END_UTC",   "21"))

# Blackout news manual: isi jam UTC rilis news besar hari ini, pisahkan koma.
# Contoh: "13:30,15:00,18:00". Bot akan skip sinyal ±NEWS_BUFFER_MIN menit dari jam2 itu.
# (Tidak ada API kalender ekonomi otomatis di sini — ini perlu diisi manual atau
#  dihubungkan ke API kalender ekonomi terpisah kalau mau full-otomatis)
NEWS_TIMES_UTC      = os.environ.get("NEWS_TIMES_UTC", "")
NEWS_BUFFER_MIN      = int(os.environ.get("NEWS_BUFFER_MIN", "20"))

# Ringkasan harian jam 07:00 WIB = 00:00 UTC
DAILY_SUMMARY_HOUR_UTC = int(os.environ.get("DAILY_SUMMARY_HOUR_UTC", "0"))

STATE_FILE = "rnm_state.json"

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
#  TELEGRAM — Ambil perintah masuk
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
        "last_bar_time":   None,
        "bar_counter":     0,
        "last_alert_key":  None,
        "last_alert_bar":  -999,
        "last_near_alert_key": None,
        "last_near_alert_bar": -999,
        # snapshot terakhir untuk /status & /trend
        "last_close":      None,
        "last_atr":        None,
        "last_rsi2":       None,
        "last_levels":     [],
        # trend H1 (MA20 & MA50)
        "h1_trend":        None,
        "h1_close":        None,
        "h1_ma_fast":      None,
        "h1_ma_slow":      None,
        # counter harian
        "daily_buy":            0,
        "daily_sell":            0,
        "daily_skip_atr":        0,
        "daily_skip_momentum":   0,
        "daily_skip_session":    0,
        "daily_near_alert":      0,
        "last_summary_day":      None,
        # telegram offset
        "tg_offset":            0
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

def calc_rsi(series: pd.Series, period: int) -> pd.Series:
    delta    = series.diff()
    gain     = delta.clip(lower=0)
    loss     = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs       = avg_gain / avg_loss.replace(0, 1e-10)
    return 100 - (100 / (1 + rs))

def trend_icon(trend: str) -> str:
    return "📈" if trend == "bullish" else ("📉" if trend == "bearish" else "↔️")

def find_round_levels(price: float, step: float, count: int) -> list:
    base = math.floor(price / step) * step
    return sorted(round(base + i * step, 2) for i in range(-count, count + 1))

# ═══════════════════════════════════════════════
#  FILTER SESI & NEWS BLACKOUT
# ═══════════════════════════════════════════════
def is_active_session() -> bool:
    hour = datetime.now(timezone.utc).hour
    if SESSION_START_UTC <= SESSION_END_UTC:
        return SESSION_START_UTC <= hour < SESSION_END_UTC
    return hour >= SESSION_START_UTC or hour < SESSION_END_UTC

def is_news_blackout() -> bool:
    if not NEWS_TIMES_UTC.strip():
        return False
    now = datetime.now(timezone.utc)
    for t in NEWS_TIMES_UTC.split(","):
        t = t.strip()
        if not t:
            continue
        try:
            hh, mm = map(int, t.split(":"))
            news_time = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            diff_min = abs((now - news_time).total_seconds()) / 60
            if diff_min <= NEWS_BUFFER_MIN:
                return True
        except Exception:
            continue
    return False

# ═══════════════════════════════════════════════
#  FILTER MOMENTUM H1 (anti breakout asli)
# ═══════════════════════════════════════════════
def check_h1_momentum_block(direction: str, level: float) -> bool:
    """True artinya signal harus DISKIP karena ada momentum H1 kuat searah level."""
    df_h1 = fetch_data_twelvedata("XAU/USD", "1h", MOMENTUM_LOOKBACK + 5)
    if df_h1 is None or len(df_h1) < MOMENTUM_LOOKBACK + 2:
        return False  # data tidak cukup -> jangan blokir, biarkan signal lewat

    df_h1["Body"] = (df_h1["Close"] - df_h1["Open"]).abs()
    last      = df_h1.iloc[-2]                       # H1 candle terakhir yg sudah close
    avg_body  = df_h1["Body"].iloc[-(MOMENTUM_LOOKBACK + 2):-2].mean()

    if pd.isna(avg_body) or avg_body == 0:
        return False

    is_big_body = last["Body"] > avg_body * MOMENTUM_BODY_MULTIPLIER

    if not is_big_body:
        return False

    if direction == "SELL":
        # bahaya: candle H1 bullish besar yang breakout ke atas level
        return bool(last["Close"] > last["Open"] and last["Close"] > level)
    else:
        # bahaya: candle H1 bearish besar yang breakout ke bawah level
        return bool(last["Close"] < last["Open"] and last["Close"] < level)

# ═══════════════════════════════════════════════
#  TREND H1 — wajib pakai MA20 & MA50 di H1
#  Kirim alert tiap kali ada pergantian tren
# ═══════════════════════════════════════════════
def check_h1_trend(state: dict, force_notify: bool = False) -> dict:
    log.info("── Mengecek Trend H1 (MA20 & MA50) ──")

    df_h1 = fetch_data_twelvedata("XAU/USD", "1h", MA_SLOW + 15)
    if df_h1 is None:
        return state

    df_h1["MA_fast"] = df_h1["Close"].rolling(MA_FAST).mean()
    df_h1["MA_slow"] = df_h1["Close"].rolling(MA_SLOW).mean()
    df_h1.dropna(inplace=True)

    if len(df_h1) < 2:
        return state

    bar1    = df_h1.iloc[-2]                 # candle H1 terakhir yang sudah close
    close   = round(float(bar1["Close"]),   2)
    ma_fast = round(float(bar1["MA_fast"]), 2)
    ma_slow = round(float(bar1["MA_slow"]), 2)

    # Sideways = harga di antara MA20 & MA50, ATAU harga menyentuh/dekat salah satu MA
    # (jarak ke MA <= H1_TREND_BUFFER) — selama itu tidak dianggap bullish/bearish penuh.
    if close > ma_fast + H1_TREND_BUFFER and close > ma_slow + H1_TREND_BUFFER:
        trend = "bullish"
    elif close < ma_fast - H1_TREND_BUFFER and close < ma_slow - H1_TREND_BUFFER:
        trend = "bearish"
    else:
        trend = "sideways"

    old_trend = state.get("h1_trend")

    state["h1_trend"]    = trend
    state["h1_close"]    = close
    state["h1_ma_fast"]  = ma_fast
    state["h1_ma_slow"]  = ma_slow

    log.info(f"Trend H1: {old_trend} → {trend} | Close {close} | MA{MA_FAST} {ma_fast} | MA{MA_SLOW} {ma_slow}")

    if trend != old_trend or force_notify:
        now_wib = datetime.now(timezone(timedelta(hours=7))).strftime("%d %b %Y %H:%M WIB")
        send_telegram(
            "🔄 <b>Pergantian Trend H1</b>\n\n"
            f"{trend_icon(old_trend)} Trend lama : <b>{(old_trend or 'unknown').upper()}</b>\n"
            f"{trend_icon(trend)} Trend baru : <b>{trend.upper()}</b>\n\n"
            f"Close H1   : {close}\n"
            f"MA{MA_FAST}      : {ma_fast}\n"
            f"MA{MA_SLOW}      : {ma_slow}\n\n"
            f"🕐 {now_wib}"
        )

    return state

# ═══════════════════════════════════════════════
#  HEADS-UP: harga mendekati level $10 yang SEARAH trend H1
#  (alert lebih dini, sebelum RSI(2) ekstrem & rejection muncul)
# ═══════════════════════════════════════════════
def check_near_level_heads_up(state: dict, trend: str, close: float,
                               levels: list, bar_idx: int) -> dict:
    if trend not in ("bullish", "bearish"):
        return state  # sideways tidak punya arah buat dicocokkan

    if trend == "bullish":
        candidates = [l for l in levels if l > close]
        if not candidates:
            return state
        nearest  = min(candidates)
        distance = round(nearest - close, 2)
    else:
        candidates = [l for l in levels if l < close]
        if not candidates:
            return state
        nearest  = max(candidates)
        distance = round(close - nearest, 2)

    if distance > NEAR_LEVEL_USD:
        return state

    key = f"NEAR_{trend}_{nearest}"
    if state.get("last_near_alert_key") == key and \
       (bar_idx - state.get("last_near_alert_bar", -999)) < NEAR_LEVEL_COOLDOWN_BARS:
        return state

    if not is_active_session() or is_news_blackout():
        return state

    now_wib = datetime.now(timezone(timedelta(hours=7))).strftime("%d %b %Y %H:%M WIB")
    send_telegram(
        "📍 <b>Harga Mendekati Level $10 (searah trend)</b>\n\n"
        f"Trend H1 : {trend_icon(trend)} {trend}\n"
        f"Level    : <b>{nearest}</b>\n"
        f"Close    : <b>{close}</b>\n"
        f"Jarak    : {distance} (≤ {NEAR_LEVEL_USD})\n\n"
        f"👀 Pantau RSI(2) M5 & candle rejection di level ini untuk entry fade.\n\n"
        f"🕐 {now_wib}"
    )

    state["last_near_alert_key"] = key
    state["last_near_alert_bar"] = bar_idx
    state["daily_near_alert"]    = state.get("daily_near_alert", 0) + 1
    return state

# ═══════════════════════════════════════════════
#  RNM FADE CHECK — setiap M5 candle close
# ═══════════════════════════════════════════════
def run_rnm_check(state: dict) -> dict:
    log.info("── Menjalankan pengecekan Round-Number Magnet Fade ──")

    df = fetch_data_twelvedata("XAU/USD", "5min", 150)
    if df is None:
        return state

    df["ATR"]       = calc_atr(df, ATR_LEN)
    df["RSI2"]      = calc_rsi(df["Close"], RSI_PERIOD)   # RSI umum (buat /status & /trend)
    df["RSI2_high"] = calc_rsi(df["High"],  RSI_PERIOD)   # jenuh-beli tepat saat wick atas
    df["RSI2_low"]  = calc_rsi(df["Low"],   RSI_PERIOD)   # jenuh-jual tepat saat wick bawah
    df.dropna(inplace=True)

    if len(df) < 3:
        return state

    bar1      = df.iloc[-2]                 # candle M5 terakhir yang sudah close
    bar1_time = str(df.index[-2])

    if state["last_bar_time"] == bar1_time:
        log.info(f"Bar {bar1_time} sudah diproses, skip.")
        return state

    state["bar_counter"] = state.get("bar_counter", 0) + 1
    bar_idx = state["bar_counter"]

    close    = round(float(bar1["Close"]),     2)
    high     = round(float(bar1["High"]),      2)
    low      = round(float(bar1["Low"]),       2)
    atr      = round(float(bar1["ATR"]),       2)
    rsi      = round(float(bar1["RSI2"]),      1)
    rsi_high = round(float(bar1["RSI2_high"]), 1)
    rsi_low  = round(float(bar1["RSI2_low"]),  1)

    # Trend wajib dari MA20 & MA50 di H1 (di-update terpisah oleh check_h1_trend)
    trend = state.get("h1_trend", "unknown")

    levels = find_round_levels(close, LEVEL_STEP, LEVEL_RANGE)

    # Simpan snapshot untuk /status & /trend
    state.update({
        "last_close":  close,
        "last_atr":    atr,
        "last_rsi2":   rsi,
        "last_levels": levels,
    })

    log.info(f"Bar: {bar1_time} | Close: {close} | ATR: {atr} | RSI2: {rsi} | Trend H1: {trend}")

    # ── Heads-up: harga mendekati level $10 searah trend (independen dari filter ATR) ──
    state = check_near_level_heads_up(state, trend, close, levels, bar_idx)

    # ── Filter 1: ATR terlalu tinggi → market terlalu volatile untuk fade ──
    if atr > ATR_MAX:
        log.info(f"Skip: ATR({atr}) > ATR_MAX({ATR_MAX})")
        state["daily_skip_atr"] = state.get("daily_skip_atr", 0) + 1
        state["last_bar_time"] = bar1_time
        return state

    # ── Cari level yang ditolak (rejection) + RSI jenuh ──
    direction = None
    level_hit = None
    min_margin = atr * REJECTION_MIN_ATR_FRACTION

    for lvl in levels:
        # Rejection dari atas → potensi SELL (RSI High jenuh-beli persis saat wick menyentuh level)
        if high >= lvl and close < lvl and (lvl - close) >= min_margin and rsi_high >= RSI_OVERBOUGHT:
            direction, level_hit = "SELL", lvl
            break
        # Rejection dari bawah → potensi BUY (RSI Low jenuh-jual persis saat wick menyentuh level)
        if low <= lvl and close > lvl and (close - lvl) >= min_margin and rsi_low <= RSI_OVERSOLD:
            direction, level_hit = "BUY", lvl
            break

    if direction is None:
        state["last_bar_time"] = bar1_time
        return state

    # ── Anti-spam: jangan ulangi sinyal level+arah yang sama dalam beberapa bar ──
    alert_key = f"{direction}_{level_hit}"
    if state.get("last_alert_key") == alert_key and (bar_idx - state.get("last_alert_bar", -999)) < COOLDOWN_BARS:
        log.info(f"Skip: cooldown untuk {alert_key}")
        state["last_bar_time"] = bar1_time
        return state

    # ── Filter 2: sesi & news blackout ──
    if not is_active_session():
        log.info("Skip: di luar jam sesi London/NY")
        state["daily_skip_session"] = state.get("daily_skip_session", 0) + 1
        state["last_bar_time"] = bar1_time
        return state

    if is_news_blackout():
        log.info("Skip: blackout window news besar")
        state["daily_skip_session"] = state.get("daily_skip_session", 0) + 1
        state["last_bar_time"] = bar1_time
        return state

    # ── Filter 3: momentum H1 (anti breakout asli) ──
    if check_h1_momentum_block(direction, level_hit):
        log.info(f"Skip: momentum H1 kuat searah breakout di level {level_hit}")
        state["daily_skip_momentum"] = state.get("daily_skip_momentum", 0) + 1
        state["last_bar_time"] = bar1_time
        return state

    # ── Hitung Entry / SL / TP ──
    sl_dist = round(atr * SL_ATR_MULTIPLIER, 2)
    tp_dist = round(sl_dist * RR, 2)
    entry   = close

    if direction == "SELL":
        sl = round(entry + sl_dist, 2)
        tp = round(entry - tp_dist, 2)
        state["daily_sell"] = state.get("daily_sell", 0) + 1
        header = "🔴 <b>SELL — Round-Number Magnet Fade</b>"
        rsi_label = f"RSI(2) High : {rsi_high} (jenuh beli saat wick)"
    else:
        sl = round(entry - sl_dist, 2)
        tp = round(entry + tp_dist, 2)
        state["daily_buy"] = state.get("daily_buy", 0) + 1
        header = "🟢 <b>BUY — Round-Number Magnet Fade</b>"
        rsi_label = f"RSI(2) Low  : {rsi_low} (jenuh jual saat wick)"

    now_wib = datetime.now(timezone(timedelta(hours=7))).strftime("%d %b %Y %H:%M WIB")

    send_telegram(
        f"{header}\n\n"
        f"Level Magnet : <b>{level_hit}</b>\n"
        f"Entry        : <b>{entry}</b>\n"
        f"SL           : <b>{sl}</b>\n"
        f"TP (RR 1:{RR:g}) : <b>{tp}</b>\n\n"
        f"{rsi_label}\n"
        f"ATR(14) : {atr}\n"
        f"Trend H1: {trend_icon(trend)} {trend}\n\n"
        f"🕐 {now_wib}"
    )

    state["last_alert_key"] = alert_key
    state["last_alert_bar"] = bar_idx
    state["last_bar_time"]  = bar1_time
    return state

# ═══════════════════════════════════════════════
#  RINGKASAN HARIAN — jam 07:00 WIB
# ═══════════════════════════════════════════════
def send_daily_summary(state: dict) -> dict:
    today_wib = datetime.now(timezone(timedelta(hours=7))).strftime("%d %b %Y")
    last_day  = state.get("last_summary_day")

    if last_day == today_wib:
        return state

    buy        = state.get("daily_buy",          0)
    sell       = state.get("daily_sell",         0)
    near       = state.get("daily_near_alert",   0)
    skip_atr   = state.get("daily_skip_atr",     0)
    skip_mom   = state.get("daily_skip_momentum",0)
    skip_sess  = state.get("daily_skip_session", 0)

    send_telegram(
        f"📋 <b>Ringkasan Harian — {today_wib}</b>\n\n"
        f"🟢 Sinyal BUY  : {buy}x\n"
        f"🔴 Sinyal SELL : {sell}x\n"
        f"📍 Heads-up dekat level : {near}x\n"
        f"📊 Total Sinyal: {buy + sell}x\n\n"
        f"⏭️ Skip (ATR > {ATR_MAX})      : {skip_atr}x\n"
        f"⏭️ Skip (momentum H1)     : {skip_mom}x\n"
        f"⏭️ Skip (sesi/news)       : {skip_sess}x\n\n"
        f"🕐 Update berikutnya: besok 07:00 WIB"
    )
    log.info("Ringkasan harian terkirim")

    state.update({
        "last_summary_day":    today_wib,
        "daily_buy":           0,
        "daily_sell":          0,
        "daily_near_alert":    0,
        "daily_skip_atr":      0,
        "daily_skip_momentum": 0,
        "daily_skip_session":  0,
    })
    return state

# ═══════════════════════════════════════════════
#  HANDLE PERINTAH TELEGRAM (/status, /trend, /help)
# ═══════════════════════════════════════════════
def handle_commands(state: dict) -> dict:
    offset  = state.get("tg_offset", 0)
    updates = get_telegram_updates(offset)

    for update in updates:
        update_id = update.get("update_id", 0)
        state["tg_offset"] = update_id + 1

        msg  = update.get("message", {})
        text = msg.get("text", "").strip().lower()

        if not text:
            continue

        log.info(f"Perintah masuk: {text}")

        if text.startswith("/status"):
            now_wib = datetime.now(timezone(timedelta(hours=7))).strftime("%d %b %Y %H:%M WIB")
            send_telegram(
                f"📡 <b>Status Bot — {now_wib}</b>\n\n"
                f"Close terakhir : {state.get('last_close', '-')}\n"
                f"ATR(14)        : {state.get('last_atr', '-')}\n"
                f"RSI(2)         : {state.get('last_rsi2', '-')}\n"
                f"Trend H1       : {trend_icon(state.get('h1_trend','unknown'))} {state.get('h1_trend','unknown')}\n"
                f"Sesi aktif     : {'✅ Ya' if is_active_session() else '❌ Tidak'}\n\n"
                f"<b>Sinyal Hari Ini</b>\n"
                f"🟢 BUY  : {state.get('daily_buy', 0)}x\n"
                f"🔴 SELL : {state.get('daily_sell', 0)}x\n"
                f"📍 Heads-up dekat level : {state.get('daily_near_alert', 0)}x\n"
                f"⏭️ Skip ATR/Momentum/Sesi : "
                f"{state.get('daily_skip_atr',0)}/{state.get('daily_skip_momentum',0)}/{state.get('daily_skip_session',0)}"
            )

        elif text.startswith("/trend"):
            levels = state.get("last_levels", [])
            send_telegram(
                "📐 <b>Kondisi Market Saat Ini</b>\n\n"
                f"Trend H1 : {trend_icon(state.get('h1_trend','unknown'))} {state.get('h1_trend','unknown')}\n"
                f"Close H1 : {state.get('h1_close', '-')}\n"
                f"MA{MA_FAST} (H1) : {state.get('h1_ma_fast', '-')}\n"
                f"MA{MA_SLOW} (H1) : {state.get('h1_ma_slow', '-')}\n\n"
                f"ATR(14) M5 : {state.get('last_atr', '-')}\n"
                f"RSI(2) M5  : {state.get('last_rsi2', '-')}\n"
                f"Level terdekat: {', '.join(str(l) for l in levels) if levels else '-'}"
            )

        elif text.startswith("/help"):
            send_telegram(
                "🤖 <b>RNM Fade Bot — Daftar Perintah</b>\n\n"
                "/status — Lihat kondisi & ringkasan sinyal hari ini\n"
                "/trend  — Lihat trend, ATR, RSI, level terdekat saat ini\n"
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

def is_daily_summary_time() -> bool:
    now_utc = datetime.now(timezone.utc)
    return now_utc.hour == DAILY_SUMMARY_HOUR_UTC and now_utc.minute < 10

def current_hour_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d-%H")

# ═══════════════════════════════════════════════
#  ENTRY POINT
# ═══════════════════════════════════════════════
if __name__ == "__main__":
    log.info("═══════════════════════════════════════")
    log.info("  RNM Fade Bot — XAUUSD M5 — Start  ")
    log.info("═══════════════════════════════════════")

    send_telegram(
        "🤖 <b>RNM Fade Bot aktif</b>\n"
        "Strategi: Round-Number Magnet Fade (RSI-2)\n\n"
        f"⚙️ RSI({RSI_PERIOD})  : OB {RSI_OVERBOUGHT} / OS {RSI_OVERSOLD}\n"
        f"⚙️ MA Fast/Slow : MA{MA_FAST} / MA{MA_SLOW} (H1, untuk trend)\n"
        f"⚙️ ATR Max      : {ATR_MAX}\n"
        f"⚙️ Level Step   : ${LEVEL_STEP:g}\n"
        f"⚙️ SL/RR        : {SL_ATR_MULTIPLIER}×ATR / 1:{RR:g}\n\n"
        "💬 Ketik /help untuk daftar perintah"
    )

    state = load_state()

    # Jalankan sekali saat start
    state = check_h1_trend(state)
    state = run_rnm_check(state)
    save_state(state)

    # ── Main Loop ───────────────────────────────
    while True:
        state = handle_commands(state)
        save_state(state)

        wait = seconds_to_next_candle(300)
        log.info(f"Menunggu {wait:.0f} detik sampai candle berikutnya...")
        time.sleep(wait)

        # Cek trend H1 setiap M5 (bukan cuma sekali per jam) — begitu candle H1
        # baru saja close, pergantian trend langsung kedeteksi maksimal dalam 5 menit.
        state = check_h1_trend(state)

        state = run_rnm_check(state)

        if is_daily_summary_time():
            state = send_daily_summary(state)

        save_state(state)
