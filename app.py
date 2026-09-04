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
st.caption("Fast directional signal from live market data. Every valid click returns UP or DOWN.")

col1, col2 = st.columns(2)
with col1:
    selected_pair = st.selectbox("Select Pair", list(pairs))
with col2:
    expiry = st.selectbox("Expiry / Horizon", [1, 2, 5], format_func=lambda x: f"{x} Minute{'s' if x > 1 else ''}")

@st.cache_data(ttl=10)
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
    x["body_ratio"] = (close - open_).abs() / x["range"].replace(0, np.nan)
    x["ema9_slope"] = x["ema9"].diff()
    x["ema21_slope"] = x["ema21"].diff()

    needed = ["Close", "Open", "ema9", "ema21", "ema50", "rsi", "macd", "macd_signal", "macd_diff", "adx", "body_ratio", "ema9_slope", "ema21_slope"]
    return x.replace([np.inf, -np.inf], np.nan).dropna(subset=needed)


def get_signal(x, horizon=1):
    """Always choose a direction when valid candles exist.
    This is a directional model, not a guarantee of the next candle."""
    if x is None or x.empty or len(x) < 60:
        return None, 0, "Data unavailable"

    # Last completed candle when possible.
    idx = -2 if len(x) >= 62 else -1
    prev_idx = -3 if len(x) >= 62 else -2
    r = x.iloc[idx]
    prev = x.iloc[prev_idx]
    p = float(r["Close"])

    up = 0.0
    down = 0.0

    # Trend structure.
    if p > r.ema9 > r.ema21 > r.ema50:
        up += 4
    elif p < r.ema9 < r.ema21 < r.ema50:
        down += 4
    else:
        up += 1.5 if p > r.ema21 else 0
        down += 1.5 if p < r.ema21 else 0

    # EMA slope gives direction even when averages are crossing.
    up += 1.5 if r.ema9_slope > 0 else 0
    down += 1.5 if r.ema9_slope < 0 else 0
    up += 1 if r.ema21_slope > 0 else 0
    down += 1 if r.ema21_slope < 0 else 0

    # RSI momentum.
    if r.rsi > prev.rsi:
        up += 1.5
    elif r.rsi < prev.rsi:
        down += 1.5
    if r.rsi >= 55:
        up += 0.75
    elif r.rsi <= 45:
        down += 0.75

    # MACD direction and crossover bias.
    if r.macd_diff > 0:
        up += 2
    else:
        down += 2
    if r.macd > r.macd_signal:
        up += 0.75
    else:
        down += 0.75

    # Candle momentum.
    if r.Close > r.Open:
        up += 1
    elif r.Close < r.Open:
        down += 1
    if r.body_ratio >= 0.5:
        up += 0.5 if r.Close > r.Open else 0
        down += 0.5 if r.Close < r.Open else 0

    # For longer expiry, give more weight to the medium trend.
    if horizon >= 2:
        if p > r.ema21:
            up += 1
        else:
            down += 1
    if horizon >= 5:
        if p > r.ema50:
            up += 1
        else:
            down += 1

    # Final tie-breaker: recent price momentum. This prevents NO SIGNAL.
    recent_move = float(x["Close"].iloc[idx] - x["Close"].iloc[idx - 3])
    if up == down:
        if recent_move >= 0:
            up += 0.1
        else:
            down += 0.1

    total = up + down
    edge = abs(up - down)
    confidence = round(min(94, 50 + (edge / max(total, 1)) * 44), 1)

    if up >= down:
        return "UP", confidence, "Bullish side selected by trend + momentum score"
    return "DOWN", confidence, "Bearish side selected by trend + momentum score"


def backtest(x, horizon):
    if x is None or x.empty or len(x) <= horizon + 80:
        return None
    wins = losses = 0
    for i in range(62, len(x) - horizon):
        window = x.iloc[: i + 1]
        sig, _, _ = get_signal(window, horizon)
        if sig is None:
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
    return {"wins": wins, "losses": losses, "trades": total, "win_rate": round(wins / total * 100, 2) if total else 0}


if st.button("🚀 GET SIGNAL", use_container_width=True):
    with st.spinner("Getting fresh market data..."):
        raw = load_data(pairs[selected_pair])
        if raw.empty:
            st.error("Live market data is temporarily unavailable. Refresh and try again.")
            st.stop()
        data = indicators(raw)
        if data.empty or len(data) < 60:
            st.error("Live candles are temporarily unavailable. Refresh and try again.")
            st.stop()
        signal, confidence, reason = get_signal(data, expiry)

    if signal == "UP":
        st.success(f"🟢 UP — confidence {confidence}%")
    else:
        st.error(f"🔴 DOWN — confidence {confidence}%")

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
        st.error("Historical data is temporarily unavailable.")
        st.stop()
    data = indicators(raw)
    result = backtest(data, expiry)
    if not result:
        st.error("Backtest data is temporarily unavailable.")
        st.stop()
    a, b, c, d = st.columns(4)
    a.metric("Win rate", f"{result['win_rate']}%")
    b.metric("Trades", result["trades"])
    c.metric("Wins", result["wins"])
    d.metric("Losses", result["losses"])
    st.info("Backtest is historical only. No indicator can guarantee the next trade; broker candles may differ from Yahoo Finance.")
