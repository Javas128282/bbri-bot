"""
RNM Fade Bot — Round-Number Magnet Fade (RSI-2) | XAUUSD
Fitur:
- Sinyal fade di level kelipatan $10 dengan RSI(2) jenuh + candle rejection (M5)
- Trend wajib dari MA20 & MA50 di H1, alert tiap kali trend H1 berubah
- Sideways = harga di antara MA20 & MA50 / menyentuh-dekat salah satu MA
- Heads-up alert saat harga mendekati level $10 yang searah trend (sebelum entry signal)
- Filter ATR(14) M5 (skip kalau market terlalu volatile)
- Filter momentum H1 (skip kalau candle H1 terakhir breakout kuat searah level)
- Filter sesi London/NY + blackout manual sekitar news besar
- Ringkasan harian jam 07:00 WIB
- Perintah /status, /trend, /help via Telegram
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

    last_h1_hour = current_hour_key()

    # ── Main Loop ───────────────────────────────
    while True:
        state = handle_commands(state)
        save_state(state)

        wait = seconds_to_next_candle(300)
        log.info(f"Menunggu {wait:.0f} detik sampai candle berikutnya...")
        time.sleep(wait)

        # Cek pergantian trend H1 (sekali per jam, sebelum cek sinyal M5)
        this_hour = current_hour_key()
        if this_hour != last_h1_hour:
            state = check_h1_trend(state)
            last_h1_hour = this_hour

        state = run_rnm_check(state)

        if is_daily_summary_time():
            state = send_daily_summary(state)

        save_state(state)
