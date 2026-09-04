import asyncio
import os
from urllib.parse import quote
from urllib.request import Request, urlopen
import json
import time

import numpy as np
import pandas as pd
import streamlit as st
import yfinance as yf
from ta.momentum import RSIIndicator, StochasticOscillator, WilliamsRIndicator, ROCIndicator
from ta.trend import EMAIndicator, MACD, ADXIndicator, CCIIndicator
from ta.volatility import AverageTrueRange, BollingerBands

st.set_page_config(page_title="Quotex Signal Bot", page_icon="⚡", layout="centered")

REGULAR_PAIRS = {
    "EUR/USD": "EURUSD=X", "GBP/USD": "GBPUSD=X", "USD/JPY": "JPY=X",
    "AUD/USD": "AUDUSD=X", "USD/CAD": "CAD=X", "EUR/JPY": "EURJPY=X",
    "GBP/JPY": "GBPJPY=X", "USD/CHF": "CHF=X", "NZD/USD": "NZDUSD=X",
    "EUR/GBP": "EURGBP=X", "AUD/JPY": "AUDJPY=X", "EUR/AUD": "EURAUD=X",
    "GBP/CHF": "GBPCHF=X", "CHF/JPY": "CHFJPY=X"
}

# Common Quotex OTC symbols. Actual OTC availability is controlled by Quotex.
OTC_PAIRS = {
    "EUR/USD OTC": "EURUSD_otc", "GBP/USD OTC": "GBPUSD_otc", "USD/JPY OTC": "USDJPY_otc",
    "AUD/USD OTC": "AUDUSD_otc", "USD/CAD OTC": "USDCAD_otc", "USD/CHF OTC": "USDCHF_otc",
    "EUR/JPY OTC": "EURJPY_otc", "GBP/JPY OTC": "GBPJPY_otc", "EUR/GBP OTC": "EURGBP_otc",
    "AUD/JPY OTC": "AUDJPY_otc", "EUR/AUD OTC": "EURAUD_otc", "GBP/AUD OTC": "GBPAUD_otc",
    "GBP/CHF OTC": "GBPCHF_otc", "CHF/JPY OTC": "CHFJPY_otc", "NZD/USD OTC": "NZDUSD_otc",
    "XAU/USD OTC": "XAUUSD_otc", "USD/INR OTC": "USDINR_otc", "USD/PKR OTC": "USDPKR_otc",
    "USD/BDT OTC": "USDBDT_otc", "USD/NGN OTC": "USDNGN_otc", "USD/TRY OTC": "USDTRY_otc"
}

ALL_PAIRS = {**REGULAR_PAIRS, **OTC_PAIRS}

st.title("⚡ Quotex Signal Bot")
st.caption("Multi-indicator ensemble using live market candles. No random/fake prices.")

col1, col2 = st.columns(2)
with col1:
    selected_pair = st.selectbox("Select Pair", list(ALL_PAIRS))
with col2:
    expiry = st.selectbox(
        "Expiry / Candle Horizon",
        [5, 10, 15, 30, 60, 120, 300, 600, 900, 1800],
        format_func=lambda x: f"{x} seconds" if x < 60 else f"{x // 60} minute{'s' if x >= 120 else ''}"
    )

is_otc = selected_pair in OTC_PAIRS
asset = ALL_PAIRS[selected_pair]


def yahoo_chart(ticker, interval="1m", range_="1d"):
    for host in ("query1.finance.yahoo.com", "query2.finance.yahoo.com"):
        try:
            url = f"https://{host}/v8/finance/chart/{quote(ticker, safe='')}?range={range_}&interval={interval}&includePrePost=false&events=div%2Csplits"
            req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urlopen(req, timeout=7) as response:
                payload = json.loads(response.read().decode("utf-8"))
            result = payload.get("chart", {}).get("result")
            if not result:
                continue
            result = result[0]
            ts = result.get("timestamp", [])
            q = result.get("indicators", {}).get("quote", [{}])[0]
            df = pd.DataFrame({
                "Open": q.get("open", []), "High": q.get("high", []),
                "Low": q.get("low", []), "Close": q.get("close", [])
            }, index=pd.to_datetime(ts, unit="s", utc=True))
            df = df.replace([np.inf, -np.inf], np.nan).dropna()
            if len(df) >= 35:
                return df
        except Exception:
            continue
    return pd.DataFrame()


async def quotex_candles(asset_name, period):
    try:
        from pyquotex.stable_api import Quotex
        email = st.secrets.get("QUOTEX_EMAIL", os.getenv("QUOTEX_EMAIL", ""))
        password = st.secrets.get("QUOTEX_PASSWORD", os.getenv("QUOTEX_PASSWORD", ""))
        if not email or not password:
            return pd.DataFrame()
        client = Quotex(email=email, password=password, lang="en")
        ok, _ = await client.connect()
        if not ok:
            return pd.DataFrame()
        candles = await client.get_candles(asset_name, time.time(), max(3600, period * 220), period)
        await client.close()
        rows = candles.get("data", []) if isinstance(candles, dict) else []
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows)
        # Quotex candle keys are time/open/high/low/close.
        rename = {"time": "Time", "open": "Open", "high": "High", "low": "Low", "close": "Close"}
        df = df.rename(columns=rename)
        if not all(c in df.columns for c in ["Time", "Open", "High", "Low", "Close"]):
            return pd.DataFrame()
        df.index = pd.to_datetime(df["Time"], unit="s", utc=True)
        return df[["Open", "High", "Low", "Close"]].astype(float).sort_index().dropna()
    except Exception:
        return pd.DataFrame()


@st.cache_data(ttl=6, show_spinner=False)
def load_data(asset_name, otc, period):
    # Seconds + OTC need the broker's own candle feed. Credentials are read only from Streamlit secrets/env.
    if otc:
        if period < 60:
            return asyncio.run(quotex_candles(asset_name, period))
        df = asyncio.run(quotex_candles(asset_name, period))
        if len(df) >= 35:
            return df
        return pd.DataFrame()

    # Regular market fallback: Yahoo supplies 1-minute candles. For 5-30 sec expiry,
    # the engine uses the newest 1-minute candle; this is a proxy, not tick-level data.
    df = yahoo_chart(asset_name, "1m", "1d")
    if len(df) >= 35:
        return df
    try:
        df = yf.download(asset_name, period="7d", interval="1m", progress=False, auto_adjust=False, threads=False)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        if df is not None and not df.empty and all(c in df.columns for c in ["Open", "High", "Low", "Close"]):
            df = df[["Open", "High", "Low", "Close"]].replace([np.inf, -np.inf], np.nan).dropna()
            if len(df) >= 35:
                return df
    except Exception:
        pass
    return pd.DataFrame()


def indicators(df):
    if df is None or len(df) < 35:
        return pd.DataFrame()
    x = df.copy()
    c = x["Close"].astype(float)
    h = x["High"].astype(float)
    l = x["Low"].astype(float)
    o = x["Open"].astype(float)

    x["ema5"] = EMAIndicator(c, 5).ema_indicator()
    x["ema9"] = EMAIndicator(c, 9).ema_indicator()
    x["ema13"] = EMAIndicator(c, 13).ema_indicator()
    x["ema21"] = EMAIndicator(c, 21).ema_indicator()
    x["ema34"] = EMAIndicator(c, 34).ema_indicator()
    x["rsi"] = RSIIndicator(c, 14).rsi()
    x["stoch"] = StochasticOscillator(h, l, c, 14, 3).stoch()
    x["williams"] = WilliamsRIndicator(h, l, c, 14).williams_r()
    x["roc"] = ROCIndicator(c, 5).roc()
    x["cci"] = CCIIndicator(h, l, c, 20).cci()
    m = MACD(c, 12, 26, 9)
    x["macd"] = m.macd()
    x["macd_signal"] = m.macd_signal()
    x["macd_diff"] = m.macd_diff()
    x["adx"] = ADXIndicator(h, l, c, 14).adx()
    bb = BollingerBands(c, 20, 2)
    x["bb_high"] = bb.bollinger_hband()
    x["bb_low"] = bb.bollinger_lband()
    x["bb_mid"] = bb.bollinger_mavg()
    x["atr"] = AverageTrueRange(h, l, c, 14).average_true_range()
    x["body_ratio"] = (c - o).abs() / (h - l).replace(0, np.nan)
    return x.replace([np.inf, -np.inf], np.nan).dropna()


def signal_engine(x, expiry_seconds):
    if x is None or len(x) < 35:
        return "UP", 50.0, "Fallback direction"
    # Last completed candle when available.
    i = -2 if len(x) >= 37 else -1
    r = x.iloc[i]
    p = float(r.Close)
    up = down = 0.0

    # 12+ independent technical votes. Weight trend/momentum more heavily.
    if p > r.ema5 > r.ema9 > r.ema13: up += 3
    if p < r.ema5 < r.ema9 < r.ema13: down += 3
    if p > r.ema21: up += 2
    else: down += 2
    if p > r.ema34: up += 1.5
    else: down += 1.5
    if r.ema5 > r.ema5 if False else r.ema5 > x["ema5"].iloc[i-1]: up += 1
    else: down += 1

    if r.rsi >= 52: up += 2
    if r.rsi <= 48: down += 2
    if r.stoch >= 55: up += 1
    if r.stoch <= 45: down += 1
    if r.williams > -50: up += 1
    if r.williams < -50: down += 1
    if r.roc > 0: up += 1
    if r.roc < 0: down += 1
    if r.cci > 0: up += 1
    if r.cci < 0: down += 1
    if r.macd_diff > 0: up += 2.5
    if r.macd_diff < 0: down += 2.5
    if r.macd > r.macd_signal: up += 1
    if r.macd < r.macd_signal: down += 1
    if r.adx >= 18:
        if up > down: up += 1
        elif down > up: down += 1
    if p > r.bb_mid: up += 1
    if p < r.bb_mid: down += 1
    if r.Close > r.Open and r.body_ratio >= 0.35: up += 1
    if r.Close < r.Open and r.body_ratio >= 0.35: down += 1

    # Short expiry: prioritize immediate momentum. Longer expiry: trend alignment.
    if expiry_seconds <= 30:
        recent = float(x["Close"].iloc[i] - x["Close"].iloc[i-3])
        if recent > 0: up += 2
        elif recent < 0: down += 2
    elif expiry_seconds >= 300:
        if p > r.ema34: up += 2
        else: down += 2

    total = up + down
    edge = abs(up - down)
    confidence = round(min(96.0, 50 + edge / max(total, 1) * 46), 1)
    if up >= down:
        return "UP", confidence, f"Ensemble score {up:.1f} vs {down:.1f}"
    return "DOWN", confidence, f"Ensemble score {down:.1f} vs {up:.1f}"


if st.button("🚀 GET SIGNAL", use_container_width=True):
    with st.spinner("Analyzing fresh candles..."):
        raw = load_data(asset, is_otc, expiry)
        if raw.empty:
            if is_otc:
                st.error("OTC live feed needs a Quotex connection configured in Streamlit Secrets. I will not invent OTC prices.")
            else:
                st.error("Live data provider is temporarily unavailable. No fake price is being used.")
            st.stop()
        data = indicators(raw)
        if data.empty:
            st.error("Not enough indicator candles returned by the data source.")
            st.stop()
        signal, confidence, reason = signal_engine(data, expiry)

    if signal == "UP":
        st.success(f"🟢 UP — confidence {confidence}%")
    else:
        st.error(f"🔴 DOWN — confidence {confidence}%")

    c1, c2, c3, c4 = st.columns(4)
    r = data.iloc[-2] if len(data) >= 37 else data.iloc[-1]
    c1.metric("Price", f"{float(r.Close):.5f}")
    c2.metric("RSI", f"{float(r.rsi):.1f}")
    c3.metric("ADX", f"{float(r.adx):.1f}")
    c4.metric("MACD", f"{float(r.macd_diff):.5f}")
    st.caption(reason)
    if not is_otc and expiry < 60:
        st.info("5–30 second expiry on regular forex uses 1-minute public candles as a proxy. True 5–30 second signals require the Quotex live candle feed.")

st.markdown("---")
st.subheader("Historical backtest")
if st.button("📊 RUN BACKTEST", use_container_width=True):
    raw = load_data(asset, is_otc, max(60, expiry))
    if raw.empty:
        st.error("Backtest data is unavailable for this asset.")
        st.stop()
    data = indicators(raw)
    wins = losses = 0
    for i in range(37, len(data) - 1):
        sig, _, _ = signal_engine(data.iloc[:i+1], expiry)
        entry = float(data["Close"].iloc[i])
        future = float(data["Close"].iloc[i+1])
        wins += int((sig == "UP" and future > entry) or (sig == "DOWN" and future < entry))
        losses += int((sig == "UP" and future <= entry) or (sig == "DOWN" and future >= entry))
    total = wins + losses
    if total:
        a, b, c = st.columns(3)
        a.metric("Win rate", f"{wins / total * 100:.2f}%")
        b.metric("Wins", wins)
        c.metric("Losses", losses)
        st.info("Historical performance is not a guarantee of future results. Use demo testing before risking money.")
    else:
        st.error("No usable backtest candles returned.")
