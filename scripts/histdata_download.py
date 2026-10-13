"""Download HistData.com tick data and build UTC bid/ask M1/M5 bars.

Output layout matches the existing parquet files:
    {out}/{SYMBOL}/raw/HISTDATA_COM_ASCII_{SYMBOL}_T{YYYYMM}.zip   (cached source)
    {out}/{SYMBOL}/M1/YYYY-MM.parquet
    {out}/{SYMBOL}/M5/YYYY-MM.parquet
Columns: o_bid h_bid l_bid c_bid o_ask h_ask l_ask c_ask ticks, index time_utc.
Only bars that contain ticks are written (no forward-filled filler bars).

Usage:
    python scripts/histdata_download.py --start 2020-01-01 --end 2026-11-01   # writes to data/XAUUSD/
    python scripts/histdata_download.py --selftest
"""

import argparse
import io
import re
import time
import zipfile
from datetime import date
from pathlib import Path

import httpx
import pandas as pd
import pyarrow  # noqa: F401  parquet engine; fail before downloading, not after (pip install pyarrow)

BASE = "https://www.histdata.com"
# HistData timestamps are London time minus 5h: UTC-5 in EU winter, UTC-4 in EU summer
# (they switch on the EU DST dates, not the US ones). Verified bar-for-bar against
# Dukascopy on XAUUSD 2021-2026, including the US/EU DST mismatch weeks.
LONDON_OFFSET = pd.Timedelta(hours=5)
EST_TO_UTC = pd.Timedelta(hours=5)  # largest file-to-UTC offset; used to pick which files a range needs
COLUMNS = ["o_bid", "h_bid", "l_bid", "c_bid", "o_ask", "h_ask", "l_ask", "c_ask", "ticks"]


def download_month(client: httpx.Client, symbol: str, year: int, month: int, raw_dir: Path,
                   pause: float = 0.0) -> Path | None:
    """Fetch one monthly tick zip, cached on disk. Returns None if HistData has no file."""
    path = raw_dir / f"HISTDATA_COM_ASCII_{symbol}_T{year}{month:02d}.zip"
    if path.exists() and zipfile.is_zipfile(path):
        return path

    page_url = f"{BASE}/download-free-forex-historical-data/?/ascii/tick-data-quotes/{symbol.lower()}/{year}/{month}"
    for attempt in range(4):
        try:
            page = client.get(page_url)
            page.raise_for_status()
            tk = re.search(r'id="tk" value="([^"]+)"', page.text)
            if not tk:
                return None  # no download form: month not published (yet)
            resp = client.post(
                f"{BASE}/get.php",
                data={"tk": tk.group(1), "date": str(year), "datemonth": f"{year}{month:02d}",
                      "platform": "ASCII", "timeframe": "T", "fxpair": symbol},
                headers={"Origin": BASE, "Referer": page_url},
            )
            resp.raise_for_status()
            if not resp.content.startswith(b"PK"):
                raise RuntimeError(f"not a zip ({len(resp.content)} bytes)")
            raw_dir.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".part")
            tmp.write_bytes(resp.content)
            tmp.replace(path)  # never leave a half-written zip behind
            time.sleep(pause)  # be polite to a free service; cached months skip this
            return path
        except (httpx.HTTPError, RuntimeError) as e:
            if attempt == 3:
                raise RuntimeError(f"{symbol} {year}-{month:02d}: download failed: {e}") from e
            delay = 5 * 2**attempt
            print(f"  {year}-{month:02d}: {e}; retrying in {delay}s")
            time.sleep(delay)
    return None


def to_utc(naive: pd.DatetimeIndex) -> pd.DatetimeIndex:
    """HistData file time -> UTC (see LONDON_OFFSET)."""
    utc = (naive + LONDON_OFFSET).tz_localize("Europe/London", ambiguous="NaT", nonexistent="NaT").tz_convert("UTC")
    if utc.isna().any():  # EU clock change is Sunday 01:00 UTC, when the market is closed
        raise ValueError("ticks fall in a DST-ambiguous hour; time conversion would be guesswork")
    return utc


def count_long_gaps(zip_path: Path) -> int:
    """Intraday gaps >= 50 min from HistData's own status report (a healthy XAUUSD month has ~15-20)."""
    with zipfile.ZipFile(zip_path) as z:
        report = next((z.read(n).decode("utf-8", "replace") for n in z.namelist() if n.lower().endswith(".txt")), "")
    return sum(3000 <= int(g) < 86400 for g in re.findall(r"Gap of (\d+)s", report))


def read_ticks(zip_path: Path) -> tuple[pd.DataFrame, int]:
    """Read the tick CSV straight from the zip. Returns bid/ask indexed by UTC time, and count_long_gaps."""
    with zipfile.ZipFile(zip_path) as z:
        csv_name = next(n for n in z.namelist() if n.lower().endswith(".csv"))
        df = pd.read_csv(io.BytesIO(z.read(csv_name)), header=None,
                         names=["ts", "bid", "ask", "vol"], dtype={"ts": str})
    long_gaps = count_long_gaps(zip_path)
    df.index = to_utc(pd.DatetimeIndex(pd.to_datetime(df["ts"], format="%Y%m%d %H%M%S%f")))
    df.index.name = "time_utc"
    print(f"    {zip_path.name}: {long_gaps} gaps >= 50 min")
    return df[["bid", "ask"]].sort_index(kind="stable"), long_gaps


def to_bars(ticks: pd.DataFrame, rule: str) -> pd.DataFrame:
    """Bid/ask OHLC + tick count; empty intervals are dropped, not filled."""
    bid = ticks["bid"].resample(rule).ohlc()
    ask = ticks["ask"].resample(rule).ohlc()
    bars = pd.concat([bid.add_suffix("_bid"), ask.add_suffix("_ask")], axis=1)
    bars.columns = [f"{c[0]}_{c.split('_')[1]}" for c in bars.columns]  # open_bid -> o_bid
    bars["ticks"] = ticks["bid"].resample(rule).count()
    return bars[bars["ticks"] > 0][COLUMNS]


def months_between(start: date, end: date) -> list[tuple[int, int]]:
    return [(p.year, p.month) for p in pd.period_range(start, end - pd.Timedelta(days=1), freq="M")]


MAX_LONG_GAPS = 25  # above this, the source month is missing trading hours (2023-03 had 114)


def run(symbol: str, start: date, end: date, out: Path, pause: float) -> None:
    lo, hi = pd.Timestamp(start, tz="UTC"), pd.Timestamp(end, tz="UTC")
    # UTC range can start in the previous EST month (first 5h of a UTC month are EST's last day)
    months = months_between((lo - EST_TO_UTC).date(), (hi - EST_TO_UTC).date() + pd.Timedelta(days=1))

    def done(key: str) -> bool:
        return all((out / symbol / tf / f"{key}.parquet").exists() for tf in ("M1", "M5"))

    def write(key: str, parts: list[pd.DataFrame]) -> None:
        if done(key):  # never overwrite: a resumed run may only hold part of this month
            return
        ticks = pd.concat(parts).sort_index(kind="stable")
        for tf, rule in (("M1", "1min"), ("M5", "5min")):
            d = out / symbol / tf
            d.mkdir(parents=True, exist_ok=True)
            tmp = d / f"{key}.parquet.part"
            to_bars(ticks, rule).to_parquet(tmp)
            tmp.replace(d / f"{key}.parquet")  # a killed run must not leave a truncated "done" file
        print(f"  wrote {symbol} {key}: {len(ticks):,} ticks")

    # Each HistData file feeds its own UTC month plus the first hours of the next one,
    # so a UTC month is complete once its own file is processed.
    pending: dict[str, list[pd.DataFrame]] = {}
    problems: list[str] = []

    def check_gaps(key: str, long_gaps: int) -> None:
        if long_gaps > MAX_LONG_GAPS:
            problems.append(f"{key}: written but INCOMPLETE at source, {long_gaps} gaps >= 50 min (normal ~15-20)")

    with httpx.Client(headers={"User-Agent": "Mozilla/5.0"}, timeout=120, follow_redirects=True) as client:
        for y, m in months:
            key = f"{y}-{m:02d}"
            nxt = (pd.Period(key, "M") + 1).strftime("%Y-%m")
            if done(key) and done(nxt):
                cached = out / symbol / "raw" / f"HISTDATA_COM_ASCII_{symbol}_T{y}{m:02d}.zip"
                if zipfile.is_zipfile(cached):  # skipped months still get reported on every run
                    check_gaps(key, count_long_gaps(cached))
                continue
            path = download_month(client, symbol, y, m, out / symbol / "raw", pause)
            try:
                if path is None:
                    raise ValueError("not available on HistData")
                ticks, long_gaps = read_ticks(path)
            except ValueError as e:
                print(f"  {key}: SKIPPED, {e}")
                problems.append(f"{key}: skipped ({e}); {nxt} may lack its first UTC hours")
                pending.pop(key, None)  # a few carried-over hours are not a month
                continue
            check_gaps(key, long_gaps)
            ticks = ticks[(ticks.index >= lo) & (ticks.index < hi)]
            print(f"  {key}: {len(ticks):,} ticks in range")
            for k, part in ticks.groupby(ticks.index.strftime("%Y-%m")):
                pending.setdefault(k, []).append(part)
            for k in sorted(k for k in pending if k <= key):
                write(k, pending.pop(k))
        for k in sorted(pending):  # tail of the range
            write(k, pending.pop(k))

    print("\nproblems:" if problems else "\nno problems found")
    for p in problems:
        print(f"  {p}")


def selftest() -> None:
    t = pd.DataFrame(
        {"bid": [1.0, 3.0, 2.0, 5.0], "ask": [1.5, 3.5, 2.5, 5.5]},
        index=pd.to_datetime(["2024-03-04 00:00:01", "2024-03-04 00:00:30",
                              "2024-03-04 00:00:59", "2024-03-04 00:03:00"]).tz_localize("UTC"),
    )
    b = to_bars(t, "1min")
    assert list(b.columns) == COLUMNS
    assert len(b) == 2, "empty minutes must be dropped, not filled"
    assert b.iloc[0][["o_bid", "h_bid", "l_bid", "c_bid", "ticks"]].tolist() == [1.0, 3.0, 1.0, 2.0, 3]
    assert b.iloc[0]["h_ask"] == 3.5 and b.iloc[1]["ticks"] == 1
    assert months_between(date(2024, 12, 30), date(2025, 2, 1)) == [(2024, 12), (2025, 1)]

    files = pd.DatetimeIndex(["2026-01-09 16:59", "2026-03-20 16:00", "2026-03-30 17:00", "2026-08-07 16:59"])
    expected = ["2026-01-09 21:59", "2026-03-20 21:00", "2026-03-30 21:00", "2026-08-07 20:59"]
    #           EU winter: UTC-5   US DST, EU not yet: UTC-5   EU summer: UTC-4   EU summer: UTC-4
    assert list(to_utc(files)) == [pd.Timestamp(t, tz="UTC") for t in expected]
    print("selftest ok")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--symbol", default="XAUUSD")
    p.add_argument("--start", type=date.fromisoformat, help="UTC date, inclusive")
    p.add_argument("--end", type=date.fromisoformat, help="UTC date, exclusive")
    p.add_argument("--out", type=Path, default=Path("data"), help="default: data (gitignored)")
    p.add_argument("--pause", type=float, default=3.0, help="seconds between monthly downloads")
    p.add_argument("--selftest", action="store_true")
    a = p.parse_args()
    if a.selftest:
        return selftest()
    if not (a.start and a.end) or a.end <= a.start:
        p.error("--start and --end (after start) are required")
    run(a.symbol.upper(), a.start, a.end, a.out, a.pause)


if __name__ == "__main__":
    main()
