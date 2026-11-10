"""
Download REAL daily crypto prices (public, free, no API key) and freeze them to CSV.

Sources (see data/SOURCES.md):
  - Binance public market-data API (data-api.binance.vision): daily close of XXX/USDT pairs.
  - European Central Bank (ECB) data API: daily EUR/USD reference rate.

Method:
  price_usd = Binance close in USDT (1 USDT treated as 1 USD; USDT itself is set to 1.00)
  price_eur = price_usd / ECB EUR/USD rate (weekends and holidays: last known ECB rate)

Output: data/raw/prices/prices_daily.csv  (date, currency, price_usd, price_eur)
        data/raw/prices/DOWNLOADED_ON.txt  (freeze date)
"""
from datetime import date, datetime, timezone
from io import StringIO
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "raw" / "prices"
START = "2024-11-01"          # 24 full months: Nov 2024 -> Oct 2026
END = "2026-10-31"

# currency code used in the study -> Binance symbol (None = stablecoin pegged to USD)
SYMBOLS = {
    "BTC": "BTCUSDT",
    "ETH": "ETHUSDT",
    "LTC": "LTCUSDT",
    "TRX": "TRXUSDT",
    "BNB": "BNBUSDT",
    "SOL": "SOLUSDT",
    "USDC": "USDCUSDT",
    "USDT": None,
}


def binance_daily(symbol: str) -> pd.DataFrame:
    """Daily closes for one symbol between START and END (Binance returns max 1000 rows per call)."""
    start_ms = int(datetime.fromisoformat(START).replace(tzinfo=timezone.utc).timestamp() * 1000)
    end_ms = int(datetime.fromisoformat(END).replace(tzinfo=timezone.utc).timestamp() * 1000)
    rows = []
    while start_ms <= end_ms:
        r = requests.get(
            "https://data-api.binance.vision/api/v3/klines",
            params={"symbol": symbol, "interval": "1d", "startTime": start_ms, "endTime": end_ms, "limit": 1000},
            timeout=30,
        )
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        rows += batch
        start_ms = batch[-1][0] + 86_400_000  # next day
    df = pd.DataFrame(rows).iloc[:, [0, 4]]
    df.columns = ["open_ms", "close"]
    df["date"] = pd.to_datetime(df["open_ms"], unit="ms").dt.date
    df["price_usd"] = df["close"].astype(float)
    return df[["date", "price_usd"]]


def ecb_eurusd() -> pd.Series:
    """ECB daily EUR/USD reference rate (USD per 1 EUR), forward-filled over every calendar day."""
    for attempt in range(3):  # the ECB API is sometimes slow: retry with a long timeout
        try:
            r = requests.get(
                "https://data-api.ecb.europa.eu/service/data/EXR/D.USD.EUR.SP00.A",
                params={"format": "csvdata", "startPeriod": "2024-10-15", "endPeriod": END},
                timeout=120,
            )
            r.raise_for_status()
            break
        except requests.RequestException:
            if attempt == 2:
                raise
    df = pd.read_csv(StringIO(r.text), usecols=["TIME_PERIOD", "OBS_VALUE"])
    s = df.set_index(pd.to_datetime(df["TIME_PERIOD"]))["OBS_VALUE"].astype(float)
    all_days = pd.date_range("2024-10-15", END, freq="D")
    s = s.reindex(all_days).ffill()
    s.index = s.index.date
    return s


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    fx = ecb_eurusd()
    days = pd.date_range(START, END, freq="D").date
    frames = []
    for cur, sym in SYMBOLS.items():
        if sym is None:
            df = pd.DataFrame({"date": days, "price_usd": 1.0})
        else:
            df = binance_daily(sym)
        df["currency"] = cur
        frames.append(df)
    prices = pd.concat(frames, ignore_index=True)
    prices["eurusd_ecb"] = prices["date"].map(fx)
    prices["price_eur"] = prices["price_usd"] / prices["eurusd_ecb"]
    prices = prices[["date", "currency", "price_usd", "price_eur", "eurusd_ecb"]].sort_values(["currency", "date"])

    # quality check: one row per currency per day, no missing values
    counts = prices.groupby("currency").size()
    assert (counts == len(days)).all(), counts
    assert prices.notna().all().all()

    prices.to_csv(OUT / "prices_daily.csv", index=False, float_format="%.6f")
    (OUT / "DOWNLOADED_ON.txt").write_text(
        f"Downloaded on {date.today().isoformat()} from data-api.binance.vision (daily klines, close) "
        f"and data-api.ecb.europa.eu (EXR D.USD.EUR.SP00.A). Period {START} to {END}.\n"
    )
    print(prices.groupby("currency")["price_eur"].agg(["min", "max", "last"]).round(2))


if __name__ == "__main__":
    main()
