import streamlit as st
import pandas as pd
import numpy as np
import yfinance as yf
from ta.momentum import RSIIndicator
from ta.trend import EMAIndicator, MACD, ADXIndicator

st.set_page_config(page_title="Quotex Signal Bot", page_icon="⚡", layout="centered")

pairs = {
    "EUR/USD": "EURUSD=X", "GBP/USD": "GBPUSD=X", "USD/JPY": "JPY=X",
    "AUD/USD": "AUDUSD=X", "USD/CAD": "CAD=X", "EUR/JPY": "EURJPY=X",
    "GBP/JPY": "GBPJPY=X", "USD/CHF": "CHF=X"
}

st.title("⚡ Quotex Signal Bot")
st.caption("Fast 1-minute trend + momentum signal. No fake/random data.")

col1, col2 = st.columns(2)
with col1:
    selected_pair = st.selectbox("Select Pair", list(pairs))
with col2:
    expiry = st.selectbox("Expiry / Horizon", [1, 2, 5], format_func=lambda x: f"{x} Minute{'s' if x > 1 else ''}")

@st.cache_data(ttl=15)
def load_data(ticker, period="7d", interval="1m"):
    try:
        df = yf.download(ticker, period=period, interval=interval, progress=False, auto_adjust=False)
    except Exception:
        return pd.DataFrame()

    if df is None or df.empty:
        return pd.DataFrame()

    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    required = ["Open", "High", "Low", "Close"]
    if not all(c in df.columns for c in required):
        return pd.DataFrame()

    df = df[required].copy()
    df = df.replace([np.inf, -np.inf], np.nan).dropna(subset=required)
    return df


def indicators(df):
    if df is None or df.empty or len(df) < 60:
        return pd.DataFrame()

    x = df.copy()
    close = x["Close"].astype(float)
    high = x["High"].astype(float)
    low = x["Low"].astype(float)
    open_ = x["Open"].astype(float)

    x["ema9"] = EMAIndicator(close, 9).ema_indicator()
    x["ema21"] = EMAIndicator(close, 21).ema_indicator()
    x["ema50"] = EMAIndicator(close, 50).ema_indicator()
    x["rsi"] = RSIIndicator(close, 14).rsi()

    m = MACD(close, 12, 26, 9)
    x["macd"] = m.macd()
    x["macd_signal"] = m.macd_signal()
    x["macd_diff"] = m.macd_diff()
    x["adx"] = ADXIndicator(high, low, close, 14).adx()

    x["range"] = high - low
    x["body"] = (close - open_).abs()
    x["body_ratio"] = x["body"] / x["range"].replace(0, np.nan)

    needed = ["Close", "Open", "ema9", "ema21", "ema50", "rsi", "macd", "macd_signal", "macd_diff", "adx", "body_ratio"]
    return x.replace([np.inf, -np.inf], np.nan).dropna(subset=needed)


def get_signal(x, horizon=1):
    if x is None or x.empty or len(x) < 60:
        return "NO SIGNAL", 0, "Not enough valid candles"

    # Use the last completed candle where possible; this reduces flicker from a live candle.
    r = x.iloc[-2] if len(x) >= 62 else x.iloc[-1]
    prev = x.iloc[-3] if len(x) >= 62 else x.iloc[-2]
    p = float(r["Close"])

    up = 0.0
    down = 0.0

    # Trend: strongest weight.
    if p > r.ema9 > r.ema21 > r.ema50:
        up += 3
    elif p < r.ema9 < r.ema21 < r.ema50:
        down += 3
    elif p > r.ema21:
        up += 1.5
    elif p < r.ema21:
        down += 1.5

    # RSI momentum.
    if r.rsi > prev.rsi and r.rsi >= 50 and r.rsi < 72:
        up += 1.5
    elif r.rsi < prev.rsi and r.rsi <= 50 and r.rsi > 28:
        down += 1.5

    # MACD direction.
    if r.macd_diff > 0:
        up += 2
    elif r.macd_diff < 0:
        down += 2

    # ADX filters only very weak markets; do not block most usable setups.
    if r.adx < 12:
        return "NO SIGNAL", 0, "Market too weak/sideways"

    # Candle confirmation.
    if r.Close > r.Open and r.body_ratio >= 0.35:
        up += 1
    elif r.Close < r.Open and r.body_ratio >= 0.35:
        down += 1

    # Small horizon adjustment: for 1m prefer current momentum; longer expiry needs trend alignment.
    if horizon >= 2:
        if p > r.ema21:
            up += 0.5
        elif p < r.ema21:
            down += 0.5

    total = up + down
    edge = abs(up - down)
    confidence = round(min(95, 50 + (edge / max(total, 1)) * 45), 1)

    # More responsive than the previous version: NO SIGNAL is reserved for genuinely mixed/weak setups.
    if up >= 4 and edge >= 1.25 and up > down:
        return "UP", confidence, "Bullish trend/momentum setup"
    if down >= 4 and edge >= 1.25 and down > up:
        return "DOWN", confidence, "Bearish trend/momentum setup"

    return "NO SIGNAL", confidence, "Mixed setup — wait for cleaner direction"


def backtest(x, horizon):
    if x is None or x.empty or len(x) <= horizon + 80:
        return None

    wins = losses = skipped = 0
    for i in range(62, len(x) - horizon):
        window = x.iloc[: i + 1]
        sig, _, _ = get_signal(window, horizon)
        if sig == "NO SIGNAL":
            skipped += 1
            continue

        entry = float(x["Close"].iloc[i])
        future = float(x["Close"].iloc[i + horizon])

        if sig == "UP":
            wins += int(future > entry)
            losses += int(future <= entry)
        else:
            wins += int(future < entry)
            losses += int(future >= entry)

    total = wins + losses
    return {
        "wins": wins,
        "losses": losses,
        "skipped": skipped,
        "trades": total,
        "win_rate": round(wins / total * 100, 2) if total else 0
    }


if st.button("🚀 GET SIGNAL", use_container_width=True):
    with st.spinner("Getting fresh market data..."):
        raw = load_data(pairs[selected_pair])
        if raw.empty:
            st.error("Market data unavailable. Try again in a few seconds.")
            st.stop()

        data = indicators(raw)
        if data.empty or len(data) < 60:
            st.warning("Not enough valid candles returned. Try again shortly or select another pair.")
            st.stop()

        signal, confidence, reason = get_signal(data, expiry)

    if signal == "UP":
        st.success(f"🟢 UP — confidence {confidence}%")
    elif signal == "DOWN":
        st.error(f"🔴 DOWN — confidence {confidence}%")
    else:
        st.warning(f"🟡 NO SIGNAL — {reason}")

    c1, c2, c3 = st.columns(3)
    r = data.iloc[-2] if len(data) >= 62 else data.iloc[-1]
    c1.metric("Price", f"{float(r.Close):.5f}")
    c2.metric("RSI", f"{float(r.rsi):.1f}")
    c3.metric("ADX", f"{float(r.adx):.1f}")
    st.caption(reason)

st.markdown("---")
st.subheader("Historical backtest")
if st.button("📊 RUN BACKTEST", use_container_width=True):
    raw = load_data(pairs[selected_pair])
    if raw.empty:
        st.error("Market data unavailable.")
        st.stop()

    data = indicators(raw)
    result = backtest(data, expiry)
    if not result or result["trades"] == 0:
        st.warning("Not enough usable historical signals for this test.")
    else:
        a, b, c, d = st.columns(4)
        a.metric("Win rate", f"{result['win_rate']}%")
        b.metric("Trades", result["trades"])
        c.metric("Wins", result["wins"])
        d.metric("Skipped", result["skipped"])
        st.info("Historical backtest only. It cannot guarantee future/live accuracy, and broker candles may differ from Yahoo Finance.")
