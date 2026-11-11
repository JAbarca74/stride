"""Collect candidate ETF metadata, daily/hourly bars, spreads, and correlations from Alpaca."""

# ruff: noqa: E501, PLR2004, PLR0912, PLR0915  (one-off research script: long report strings)

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests

REPO_ROOT = Path(__file__).resolve().parents[2]
DB_PATH = REPO_ROOT / "data" / "stride.db"
ENV_PATH = REPO_ROOT / ".env"
OUT_DIR = REPO_ROOT / "reports" / "mc_universe"
CACHE_DIR = OUT_DIR / "cache"

PAPER_BASE_URL = "https://paper-api.alpaca.markets"
DATA_BASE_URL = "https://data.alpaca.markets"

NY_TZ = ZoneInfo("America/New_York")

SLEEP_SECONDS = 0.35
MAX_RETRIES = 3
HTTP_TOO_MANY_REQUESTS = 429
HTTP_FORBIDDEN = 403
HTTP_SERVER_ERROR_MIN = 500
SIP_DELAY_MINUTES = 20

TRADING_DAYS_PER_YEAR = 252
VOL_WINDOW_SESSIONS = 756
MIN_CORR_SESSIONS = 500
DOLLAR_VOL_WINDOW = 60
HOURLY_LOOKBACK_DAYS = 365
NY_RTH_START_HOUR = 9
NY_RTH_END_HOUR = 15
BPS_FACTOR = 1e4
HIGH_CORR_THRESHOLD = 0.85
DB_SANITY_CORR_THRESHOLD = 0.98
TOP_CORR_COUNT = 3
PERCENT_FACTOR = 100.0
MILLION_FACTOR = 1e6
P90_QUANTILE = 0.90

QUOTE_WINDOW_HOURS = (10, 11, 12, 13, 14)
QUOTE_WINDOW_MINUTE = 30

CANDIDATES: list[tuple[str, str]] = [
    # US equity broad
    ("SPY", "US equity broad"),
    ("QQQ", "US equity broad"),
    ("IWM", "US equity broad"),
    ("DIA", "US equity broad"),
    # US sectors
    ("XLE", "US sectors"),
    ("XLF", "US sectors"),
    ("XLI", "US sectors"),
    ("XLK", "US sectors"),
    ("XLV", "US sectors"),
    ("XLU", "US sectors"),
    ("XLP", "US sectors"),
    ("XLY", "US sectors"),
    ("XLB", "US sectors"),
    ("XLC", "US sectors"),
    ("SMH", "US sectors"),
    # International equity
    ("EFA", "International equity"),
    ("EEM", "International equity"),
    ("VGK", "International equity"),
    ("EWJ", "International equity"),
    ("FXI", "International equity"),
    ("INDA", "International equity"),
    ("EWZ", "International equity"),
    # Bonds
    ("SHY", "Bonds"),
    ("IEF", "Bonds"),
    ("TLT", "Bonds"),
    ("TIP", "Bonds"),
    ("LQD", "Bonds"),
    ("HYG", "Bonds"),
    ("EMB", "Bonds"),
    ("BWX", "Bonds"),
    # Metals
    ("GLD", "Metals"),
    ("SLV", "Metals"),
    ("GDX", "Metals"),
    ("CPER", "Metals"),
    ("PPLT", "Metals"),
    # Commodities
    ("DBC", "Commodities"),
    ("DBA", "Commodities"),
    ("USO", "Commodities"),
    ("UNG", "Commodities"),
    # Real estate
    ("VNQ", "Real estate"),
    # Currencies
    ("UUP", "Currencies"),
    ("FXY", "Currencies"),
    ("FXE", "Currencies"),
    # Crypto ETFs
    ("IBIT", "Crypto ETFs"),
    ("ETHA", "Crypto ETFs"),
    # Inverse (optional)
    ("SH", "Inverse"),
    ("PSQ", "Inverse"),
    ("TBF", "Inverse"),
]


def load_alpaca_headers() -> dict[str, str]:
    """Load Alpaca paper API credentials from environment or .env without logging them."""
    api_key = os.environ.get("ALPACA_API_KEY", "").strip()
    secret_key = os.environ.get("ALPACA_SECRET_KEY", "").strip()

    if (not api_key or not secret_key) and ENV_PATH.exists():
        env_vars: dict[str, str] = {}
        for raw_line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            k = key.strip()
            v = val.strip().strip("'").strip('"')
            env_vars[k] = v
        if not api_key:
            api_key = env_vars.get("ALPACA_API_KEY", "") or env_vars.get("APCA_API_KEY_ID", "")
        if not secret_key:
            secret_key = env_vars.get("ALPACA_SECRET_KEY", "") or env_vars.get(
                "APCA_API_SECRET_KEY", ""
            )

    if not api_key or not secret_key:
        msg = "Missing ALPACA_API_KEY or ALPACA_SECRET_KEY in environment or .env"
        raise RuntimeError(msg)

    return {
        "APCA-API-KEY-ID": api_key,
        "APCA-API-SECRET-KEY": secret_key,
        "Accept": "application/json",
    }


class CachedAlpacaClient:
    """Polite HTTP GET client with disk cache and exponential backoff."""

    def __init__(self, headers: dict[str, str], cache_dir: Path, end_cap_iso: str) -> None:
        self.session = requests.Session()
        self.session.headers.update(headers)
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.end_cap_iso = end_cap_iso

    def _cache_path(self, cache_key: str) -> Path:
        digest = hashlib.sha256(cache_key.encode("utf-8")).hexdigest()[:24]
        safe_prefix = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in cache_key[:60])
        return self.cache_dir / f"{safe_prefix}_{digest}.json"

    def get_json(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        cache_key: str | None = None,
    ) -> tuple[int, dict[str, Any], str]:
        """Fetch JSON from url or cache. Returns (status_code, data_dict, error_text)."""
        key = cache_key or f"{url}?{json.dumps(params or {}, sort_keys=True)}"
        cpath = self._cache_path(key)
        if cpath.exists():
            cached = json.loads(cpath.read_text(encoding="utf-8"))
            return (
                int(cached.get("status", 200)),
                cached.get("data", {}),
                str(cached.get("error", "")),
            )

        status_code = 0
        error_text = ""
        for attempt in range(MAX_RETRIES + 1):
            time.sleep(SLEEP_SECONDS)
            try:
                resp = self.session.get(url, params=params, timeout=30)
                status_code = resp.status_code
                if status_code == HTTP_TOO_MANY_REQUESTS or status_code >= HTTP_SERVER_ERROR_MIN:
                    error_text = resp.text[:300]
                    if attempt < MAX_RETRIES:
                        time.sleep((2**attempt) * 1.0)
                        continue
                if resp.ok:
                    data = resp.json()
                    payload = {"status": status_code, "data": data, "error": ""}
                    cpath.write_text(json.dumps(payload), encoding="utf-8")
                    return status_code, data, ""
                error_text = resp.text[:300]
                payload = {"status": status_code, "data": {}, "error": error_text}
                cpath.write_text(json.dumps(payload), encoding="utf-8")
                return status_code, {}, error_text
            except requests.RequestException as exc:
                error_text = str(exc)
                if attempt < MAX_RETRIES:
                    time.sleep((2**attempt) * 1.0)
                    continue
        return status_code, {}, error_text

    def fetch_paginated_bars(
        self,
        symbol: str,
        timeframe: str,
        start: str,
        end: str,
        cache_tag: str,
    ) -> tuple[list[dict[str, Any]], str]:
        """Fetch all bars across pagination tokens and cache the combined list."""
        combined_path = self.cache_dir / f"bars_{symbol}_{timeframe}_{cache_tag}.json"
        if combined_path.exists():
            cached = json.loads(combined_path.read_text(encoding="utf-8"))
            return cached.get("bars", []), cached.get("error", "")

        all_bars: list[dict[str, Any]] = []
        page_token: str | None = None
        page_idx = 0
        url = f"{DATA_BASE_URL}/v2/stocks/{symbol}/bars"

        while True:
            params: dict[str, Any] = {
                "timeframe": timeframe,
                "adjustment": "all",
                "feed": "sip",
                "start": start,
                "end": end,
                "limit": 10000,
            }
            if page_token:
                params["page_token"] = page_token

            status, data, err = self.get_json(
                url,
                params=params,
                cache_key=f"bars_page_{symbol}_{timeframe}_{cache_tag}_{page_idx}_{page_token}",
            )
            if status != 200 and not data:
                return all_bars, f"HTTP {status}: {err}"

            bars_page = data.get("bars") or []
            all_bars.extend(bars_page)
            page_token = data.get("next_page_token")
            page_idx += 1
            if not page_token:
                break

        combined_path.write_text(
            json.dumps({"bars": all_bars, "error": ""}),
            encoding="utf-8",
        )
        return all_bars, ""


def query_db_dates_and_closes(
    db_path: Path,
) -> tuple[dict[str, tuple[str, str]], dict[str, pd.Series]]:
    """Read min/max date and daily close series for all symbols present in data/stride.db."""
    date_bounds: dict[str, tuple[str, str]] = {}
    close_series: dict[str, pd.Series] = {}
    if not db_path.exists():
        return date_bounds, close_series

    uri = f"file:{db_path.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='prices'")
        if not cur.fetchone():
            return date_bounds, close_series

        cur.execute("SELECT symbol, MIN(date), MAX(date) FROM prices GROUP BY symbol")
        for sym, min_d, max_d in cur.fetchall():
            date_bounds[str(sym)] = (str(min_d or ""), str(max_d or ""))

        df = pd.read_sql_query(
            "SELECT symbol, date, close FROM prices ORDER BY symbol, date",
            conn,
        )
        for sym, grp in df.groupby("symbol"):
            s = pd.Series(grp["close"].to_numpy(dtype=float), index=grp["date"].astype(str))
            s = s[~s.index.duplicated(keep="last")].sort_index()
            close_series[str(sym)] = s
    finally:
        conn.close()

    return date_bounds, close_series


def compute_daily_metrics(
    bars: list[dict[str, Any]],
) -> tuple[str, float | None, float | None, float | None, pd.Series]:
    """Compute first date, 3y annualized vol, 60d median dollar volume, last close, log returns."""
    if not bars:
        return "", None, None, None, pd.Series(dtype=float)

    dates = [str(b["t"])[:10] for b in bars]
    closes = np.array([float(b["c"]) for b in bars], dtype=float)
    volumes = np.array([float(b["v"]) for b in bars], dtype=float)

    df = pd.DataFrame({"close": closes, "volume": volumes}, index=pd.Index(dates, name="date"))
    df = df[~df.index.duplicated(keep="last")].sort_index()

    first_date = str(df.index[0])
    last_close = float(df["close"].iloc[-1])

    log_ret = np.log(df["close"] / df["close"].shift(1)).dropna()
    tail_ret = log_ret.iloc[-VOL_WINDOW_SESSIONS:]
    ann_vol_3y = (
        float(tail_ret.std(ddof=1) * math.sqrt(TRADING_DAYS_PER_YEAR))
        if len(tail_ret) >= 2
        else None
    )

    dollar_vol = (df["close"] * df["volume"]).iloc[-DOLLAR_VOL_WINDOW:]
    median_dv_60d = float(dollar_vol.median()) if not dollar_vol.empty else None

    return first_date, ann_vol_3y, median_dv_60d, last_close, log_ret


def compute_hourly_metrics(
    bars: list[dict[str, Any]],
) -> tuple[int, float | None, float | None]:
    """Filter hourly bars to NY hours 9..15 and compute count, median and p90 abs 1h return bps."""
    if not bars:
        return 0, None, None

    records: list[tuple[str, datetime, float]] = []
    for b in bars:
        ts_utc = datetime.fromisoformat(str(b["t"]).replace("Z", "+00:00"))
        ts_ny = ts_utc.astimezone(NY_TZ)
        if NY_RTH_START_HOUR <= ts_ny.hour <= NY_RTH_END_HOUR:
            records.append((ts_ny.strftime("%Y-%m-%d"), ts_ny, float(b["c"])))

    if not records:
        return 0, None, None

    records.sort(key=lambda r: r[1])
    hourly_count = len(records)

    abs_rets_bps: list[float] = []
    for i in range(1, len(records)):
        day_prev, _, c_prev = records[i - 1]
        day_curr, _, c_curr = records[i]
        if day_prev == day_curr and c_prev > 0 and c_curr > 0:
            abs_rets_bps.append(abs(math.log(c_curr / c_prev)) * BPS_FACTOR)

    if not abs_rets_bps:
        return hourly_count, None, None

    arr = np.array(abs_rets_bps, dtype=float)
    median_bps = float(np.median(arr))
    p90_bps = float(np.quantile(arr, P90_QUANTILE))
    return hourly_count, median_bps, p90_bps


def fetch_quotes_spread(
    client: CachedAlpacaClient,
    symbol: str,
    session_date: str,
) -> tuple[float | None, int, int, str]:
    """Fetch 5 1-minute quote windows for session_date and compute median spread in bps."""
    year, month, day = (int(x) for x in session_date.split("-"))
    spreads_bps: list[float] = []
    last_status = 200
    last_err = ""

    url = f"{DATA_BASE_URL}/v2/stocks/{symbol}/quotes"
    for hour in QUOTE_WINDOW_HOURS:
        start_ny = datetime(year, month, day, hour, QUOTE_WINDOW_MINUTE, 0, tzinfo=NY_TZ)
        end_ny = start_ny + timedelta(minutes=1)
        start_utc = start_ny.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        end_utc = end_ny.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

        params = {
            "feed": "sip",
            "start": start_utc,
            "end": end_utc,
            "limit": 1000,
        }
        status, data, err = client.get_json(
            url,
            params=params,
            cache_key=f"quotes_{symbol}_{session_date}_{hour}{QUOTE_WINDOW_MINUTE}",
        )
        last_status = status
        if status != 200 and not data:
            last_err = err
            if status == HTTP_FORBIDDEN:
                return None, 0, status, err
            continue

        quotes = data.get("quotes") or []
        for q in quotes:
            bp = float(q.get("bp") or 0.0)
            ap = float(q.get("ap") or 0.0)
            if bp > 0.0 and ap > bp:
                mid = 0.5 * (ap + bp)
                spreads_bps.append(((ap - bp) / mid) * BPS_FACTOR)

    if not spreads_bps:
        return None, 0, last_status, last_err

    arr = np.array(spreads_bps, dtype=float)
    return float(np.median(arr)), len(spreads_bps), last_status, last_err


def fmt_num(val: Any, decimals: int = 2) -> str:
    """Format a float to fixed decimals or empty string if missing/NaN."""
    if val is None or val == "":
        return ""
    try:
        fval = float(val)
    except (TypeError, ValueError):
        return str(val)
    if math.isnan(fval):
        return ""
    return f"{fval:.{decimals}f}"


def build_tables_md(
    df_cand: pd.DataFrame,
    corr_full: pd.DataFrame,
    corr_short: pd.DataFrame | None,
) -> str:
    """Build Markdown tables A, B, C, and D from computed candidate and correlation frames."""
    lines: list[str] = ["# Candidate ETF Universe Tables", ""]

    # Table A: Sorted by class then symbol
    lines.append("## Table A: Candidate Summary")
    lines.append("")
    lines.append(
        "| symbol | class | fractionable | shortable | db_first_date | hourly_first_date | "
        "ann_vol_3y (%) | median_dollar_volume_60d (USD millions) | median_spread_bps | "
        "median_abs_1h_ret_bps | last_close |"
    )
    lines.append("|---|---|---|---|---|---|---:|---:|---:|---:|---:|")

    df_sorted = df_cand.sort_values(by=["asset_class", "symbol"], kind="mergesort")
    for _, row in df_sorted.iterrows():
        vol_pct = (
            fmt_num(float(row["ann_vol_3y"]) * PERCENT_FACTOR, 2)
            if pd.notna(row["ann_vol_3y"]) and row["ann_vol_3y"] != ""
            else ""
        )
        dv_m = (
            fmt_num(float(row["median_dollar_volume_60d"]) / MILLION_FACTOR, 2)
            if pd.notna(row["median_dollar_volume_60d"]) and row["median_dollar_volume_60d"] != ""
            else ""
        )
        lines.append(
            f"| {row['symbol']} | {row['asset_class']} | {row['fractionable']} | "
            f"{row['shortable']} | {row['db_first_date']} | {row['hourly_first_date']} | "
            f"{vol_pct} | {dv_m} | {fmt_num(row['median_spread_bps'], 2)} | "
            f"{fmt_num(row['median_abs_1h_ret_bps'], 2)} | {fmt_num(row['last_close'], 2)} |"
        )
    lines.append("")

    # Table B: For each symbol, its 3 most correlated other candidates with the values
    lines.append("## Table B: Top 3 Most Correlated Candidates per Symbol")
    lines.append("")
    lines.append("| symbol | top_1 | top_2 | top_3 | matrix |")
    lines.append("|---|---|---|---|---|")

    for label, cmat in [("756d", corr_full), ("short", corr_short)]:
        if cmat is None or cmat.empty:
            continue
        for sym in cmat.index:
            s = cmat.loc[sym].drop(labels=[sym]).dropna().sort_values(ascending=False)
            top3 = [f"{other} ({val:.2f})" for other, val in s.head(TOP_CORR_COUNT).items()]
            while len(top3) < TOP_CORR_COUNT:
                top3.append("")
            lines.append(f"| {sym} | {top3[0]} | {top3[1]} | {top3[2]} | {label} |")
    lines.append("")

    # Table C: All pairs with correlation >= 0.85, sorted descending
    lines.append("## Table C: Highly Correlated Pairs (corr >= 0.85)")
    lines.append("")
    lines.append("| symbol_1 | symbol_2 | corr | matrix |")
    lines.append("|---|---|---:|---|")

    pairs: list[tuple[str, str, float, str]] = []
    for label, cmat in [("756d", corr_full), ("short", corr_short)]:
        if cmat is None or cmat.empty:
            continue
        syms = list(cmat.index)
        for i in range(len(syms)):
            for j in range(i + 1, len(syms)):
                val = float(cmat.iloc[i, j])
                if not math.isnan(val) and val >= HIGH_CORR_THRESHOLD:
                    pairs.append((syms[i], syms[j], val, label))
    pairs.sort(key=lambda x: (-x[2], x[0], x[1]))
    for s1, s2, val, label in pairs:
        lines.append(f"| {s1} | {s2} | {val:.2f} | {label} |")
    lines.append("")

    # Table D: Average absolute correlation of each symbol with all the others, ascending
    lines.append("## Table D: Average Absolute Correlation (Ascending)")
    lines.append("")
    lines.append("| symbol | avg_abs_corr | matrix |")
    lines.append("|---|---:|---|")

    avg_rows: list[tuple[str, float, str]] = []
    for label, cmat in [("756d", corr_full), ("short", corr_short)]:
        if cmat is None or cmat.empty:
            continue
        for sym in cmat.index:
            others = cmat.loc[sym].drop(labels=[sym]).dropna()
            if not others.empty:
                avg_rows.append((sym, float(others.abs().mean()), label))
    avg_rows.sort(key=lambda x: (0 if x[2] == "756d" else 1, x[1], x[0]))
    for sym, avg_val, label in avg_rows:
        lines.append(f"| {sym} | {avg_val:.4f} | {label} |")
    lines.append("")

    return "\n".join(lines)


def resolve_completed_session_date(spy_bars: list[dict[str, Any]], now_utc: datetime) -> str:
    """Return the most recent completed regular session date strictly before today in NY."""
    today_ny = now_utc.astimezone(NY_TZ).strftime("%Y-%m-%d")
    bar_dates = sorted({str(b["t"])[:10] for b in spy_bars if str(b["t"])[:10] < today_ny})
    if not bar_dates:
        msg = "Could not determine most recent completed session from SPY daily bars"
        raise RuntimeError(msg)
    return bar_dates[-1]


def main() -> None:
    """Run candidate universe data collection and generate CSV/Markdown outputs."""
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    run_dt_utc = datetime.now(UTC)
    end_cap_dt = run_dt_utc - timedelta(minutes=SIP_DELAY_MINUTES)
    # Round down end_cap to the hour so cached runs within the same hour reuse keys cleanly
    end_cap_rounded = end_cap_dt.replace(minute=0, second=0, microsecond=0)
    end_cap_iso = end_cap_rounded.strftime("%Y-%m-%dT%H:%M:%SZ")
    hourly_start_dt = end_cap_rounded - timedelta(days=HOURLY_LOOKBACK_DAYS)
    hourly_start_iso = hourly_start_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    cache_tag = end_cap_rounded.strftime("%Y%m%d")

    headers = load_alpaca_headers()
    client = CachedAlpacaClient(headers=headers, cache_dir=CACHE_DIR, end_cap_iso=end_cap_iso)

    db_bounds, db_closes = query_db_dates_and_closes(DB_PATH)

    # First fetch SPY daily bars to determine the most recent completed regular session
    spy_daily_bars, spy_err = client.fetch_paginated_bars(
        "SPY", "1Day", "2016-01-01", end_cap_iso, f"1d_{cache_tag}"
    )
    if not spy_daily_bars:
        msg = f"Failed to fetch SPY daily bars to determine session date: {spy_err}"
        raise RuntimeError(msg)

    spread_session_date = resolve_completed_session_date(spy_daily_bars, run_dt_utc)

    rows: list[dict[str, Any]] = []
    daily_log_returns: dict[str, pd.Series] = {}
    failures: list[str] = []
    quotes_403_errors: list[tuple[str, int, str]] = []

    for symbol, asset_class in CANDIDATES:
        # 1. Asset metadata
        asset_url = f"{PAPER_BASE_URL}/v2/assets/{symbol}"
        a_status, a_data, a_err = client.get_json(asset_url, cache_key=f"asset_{symbol}")
        if a_status != 200 or not a_data:
            failures.append(f"{symbol}: asset metadata HTTP {a_status} ({a_err})")

        # 2. DB dates
        db_first, db_last = db_bounds.get(symbol, ("", ""))

        # 3. Daily bars
        d_bars, d_err = client.fetch_paginated_bars(
            symbol, "1Day", "2016-01-01", end_cap_iso, f"1d_{cache_tag}"
        )
        if d_err:
            failures.append(f"{symbol}: daily bars error ({d_err})")
        (
            alpaca_daily_first,
            ann_vol_3y,
            median_dv_60d,
            last_close,
            log_ret,
        ) = compute_daily_metrics(d_bars)
        daily_log_returns[symbol] = log_ret

        # 4. Hourly first bar
        h1_url = f"{DATA_BASE_URL}/v2/stocks/{symbol}/bars"
        h1_status, h1_data, h1_err = client.get_json(
            h1_url,
            params={
                "timeframe": "1Hour",
                "adjustment": "all",
                "feed": "sip",
                "start": "2016-01-01",
                "end": end_cap_iso,
                "limit": 1,
            },
            cache_key=f"hourly_first_{symbol}",
        )
        hourly_first_date = ""
        if h1_status == 200 and h1_data.get("bars"):
            hourly_first_date = str(h1_data["bars"][0]["t"])
        elif h1_status != 200:
            failures.append(f"{symbol}: hourly first bar HTTP {h1_status} ({h1_err})")

        # 5. Hourly bars over last 365 calendar days
        h_bars, h_err = client.fetch_paginated_bars(
            symbol, "1Hour", hourly_start_iso, end_cap_iso, f"1h365d_{cache_tag}"
        )
        if h_err:
            failures.append(f"{symbol}: hourly 12m bars error ({h_err})")
        hourly_bars_12m, median_1h_bps, p90_1h_bps = compute_hourly_metrics(h_bars)

        # 6. Quoted spread
        median_spread_bps, quotes_used, q_status, q_err = fetch_quotes_spread(
            client, symbol, spread_session_date
        )
        if q_status == HTTP_FORBIDDEN:
            quotes_403_errors.append((symbol, q_status, q_err))
        elif q_status != 200 or quotes_used == 0:
            failures.append(
                f"{symbol}: quotes spread HTTP {q_status}, quotes_used={quotes_used} ({q_err})"
            )

        rows.append(
            {
                "symbol": symbol,
                "asset_class": asset_class,
                "tradable": a_data.get("tradable", ""),
                "fractionable": a_data.get("fractionable", ""),
                "shortable": a_data.get("shortable", ""),
                "easy_to_borrow": a_data.get("easy_to_borrow", ""),
                "exchange": a_data.get("exchange", ""),
                "status": a_data.get("status", ""),
                "db_first_date": db_first,
                "db_last_date": db_last,
                "alpaca_daily_first_date": alpaca_daily_first,
                "ann_vol_3y": round(ann_vol_3y, 6) if ann_vol_3y is not None else "",
                "median_dollar_volume_60d": (
                    round(median_dv_60d, 2) if median_dv_60d is not None else ""
                ),
                "hourly_first_date": hourly_first_date,
                "hourly_bars_12m": hourly_bars_12m,
                "median_abs_1h_ret_bps": (
                    round(median_1h_bps, 4) if median_1h_bps is not None else ""
                ),
                "p90_abs_1h_ret_bps": (round(p90_1h_bps, 4) if p90_1h_bps is not None else ""),
                "median_spread_bps": (
                    round(median_spread_bps, 4) if median_spread_bps is not None else ""
                ),
                "quotes_used": quotes_used if median_spread_bps is not None else "",
                "spread_session_date": (
                    spread_session_date if median_spread_bps is not None else ""
                ),
                "last_close": round(last_close, 4) if last_close is not None else "",
            }
        )

    df_cand = pd.DataFrame(rows)
    df_cand.to_csv(OUT_DIR / "candidates.csv", index=False)

    # Correlations over last 756 sessions
    all_ret_df = pd.DataFrame(daily_log_returns).sort_index()
    window_756 = all_ret_df.iloc[-VOL_WINDOW_SESSIONS:]
    session_counts = window_756.notna().sum()

    long_syms = [s for s, _ in CANDIDATES if int(session_counts.get(s, 0)) >= MIN_CORR_SESSIONS]
    short_syms = [s for s, _ in CANDIDATES if int(session_counts.get(s, 0)) < MIN_CORR_SESSIONS]

    common_long = window_756[long_syms].dropna(how="any")
    corr_full = common_long.corr(method="pearson").round(2)
    corr_full.to_csv(OUT_DIR / "corr.csv")

    corr_short: pd.DataFrame | None = None
    common_short_len = 0
    if short_syms:
        common_short = window_756[short_syms].dropna(how="any")
        common_short_len = len(common_short)
        corr_short = common_short.corr(method="pearson").round(2)
        corr_short.to_csv(OUT_DIR / "corr_short.csv")

    # Sanity check against data/stride.db
    db_sanity_results: list[tuple[str, int, float]] = []
    db_below_threshold: list[tuple[str, int, float]] = []
    for sym, db_c in sorted(db_closes.items()):
        if sym not in daily_log_returns:
            continue
        db_ret = np.log(db_c / db_c.shift(1)).dropna()
        alp_ret = daily_log_returns[sym]
        joined = pd.concat([db_ret.rename("db"), alp_ret.rename("alpaca")], axis=1).dropna()
        overlap = joined.iloc[-MIN_CORR_SESSIONS:]
        if len(overlap) >= 2:
            cval = float(overlap["db"].corr(overlap["alpaca"]))
            db_sanity_results.append((sym, len(overlap), cval))
            if cval < DB_SANITY_CORR_THRESHOLD:
                db_below_threshold.append((sym, len(overlap), cval))

    # Write tables.md
    tables_md = build_tables_md(df_cand, corr_full, corr_short)
    (OUT_DIR / "tables.md").write_text(tables_md, encoding="utf-8")

    # Write notes.md
    notes_lines: list[str] = [
        "# Candidate Universe Research Notes",
        "",
        f"- **Run timestamp (UTC):** `{run_dt_utc.strftime('%Y-%m-%dT%H:%M:%SZ')}`",
        f"- **SIP end cap (UTC):** `{end_cap_iso}`",
        f"- **Spread session date:** `{spread_session_date}`",
        "",
        "## Regular-Hours Filter Applied (Hourly Bars)",
        "",
        "- Converted each Alpaca `1Hour` bar timestamp (`t`) from UTC to `America/New_York`.",
        "- Alpaca hourly bars are stamped at the start of the hour (`09:00`, `10:00`, ..., `15:00` NY time, where `09:00` covers the partial `09:30-09:59` regular session opening hour and `15:00` covers `15:00-15:59`).",
        "- Kept bars whose start hour in `America/New_York` is between `9` and `15` inclusive (`9 <= ts_ny.hour <= 15`), giving up to 7 regular-session hourly bars per full trading day.",
        "- Computed `|log(c_t / c_{t-1})| * 1e4` only between consecutive kept bars falling on the same NY calendar date (excluding overnight gaps).",
        "",
        "## Correlation Windows",
        "",
        f"- **Main correlation matrix (`corr.csv`):** {len(long_syms)} symbols with >= {MIN_CORR_SESSIONS} sessions in the last {VOL_WINDOW_SESSIONS}-session window; common overlapping dates used: {len(common_long)} sessions.",
    ]
    if short_syms:
        short_desc = ", ".join(f"{s} ({int(session_counts[s])} sessions)" for s in short_syms)
        notes_lines.append(
            f"- **Symbols with < {MIN_CORR_SESSIONS} sessions in the 756-session window (`corr_short.csv`):** {short_desc}. Common overlapping window across these shorter-history symbols: {common_short_len} sessions."
        )
    else:
        notes_lines.append(
            f"- All symbols had at least {MIN_CORR_SESSIONS} sessions in the 756-session window."
        )

    notes_lines.extend(
        ["", "## Database Sanity Check (`data/stride.db` vs Alpaca Adjusted Daily Returns)", ""]
    )
    if db_sanity_results:
        for sym, n_obs, cval in db_sanity_results:
            notes_lines.append(f"- `{sym}`: overlap={n_obs} sessions, corr={cval:.6f}")
        if db_below_threshold:
            notes_lines.append("")
            notes_lines.append("### Symbols Below 0.98 Correlation Threshold")
            for sym, n_obs, cval in db_below_threshold:
                notes_lines.append(f"- **`{sym}`**: corr={cval:.6f} (n={n_obs})")
        else:
            notes_lines.append("")
            notes_lines.append("- All overlapping DB symbols had correlation >= 0.98.")
    else:
        notes_lines.append("- No overlapping symbols found in `data/stride.db`.")

    notes_lines.extend(["", "## Endpoint / Symbol Failures", ""])
    if quotes_403_errors:
        sample_sym, sample_code, sample_msg = quotes_403_errors[0]
        notes_lines.append(
            f"- Quotes endpoint returned HTTP {sample_code} for {len(quotes_403_errors)} symbols (e.g. `{sample_sym}`): `{sample_msg}`"
        )
    if failures:
        for fmsg in failures:
            notes_lines.append(f"- {fmsg}")
    if not quotes_403_errors and not failures:
        notes_lines.append("- None. All endpoints succeeded for all 48 candidate symbols.")

    notes_lines.extend(
        [
            "",
            "## Surprising / Notable Findings",
            "",
            "- Review `tables.md` and the notes above for liquidity, spread, and history differences (such as newer crypto ETFs `IBIT` and `ETHA` if applicable).",
        ]
    )

    (OUT_DIR / "notes.md").write_text("\n".join(notes_lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
