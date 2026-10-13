"""Fill missing HistData bars with real (non-flat) bars from a Dukascopy bar zip.

Reads {out}/{SYMBOL}/{M1,M5}/YYYY-MM.parquet, adds Dukascopy bars for timestamps HistData
lacks, and writes {out}/{SYMBOL}/{M1,M5}_filled/YYYY-MM.parquet. Existing files are not touched.
HistData bars always win; flat Dukascopy bars (high == low on bid and ask) are filler and skipped.
Filled bars keep Dukascopy's negative `ticks` value, which marks them as "no real tick count".

Usage:
    python scripts/fill_from_dukascopy.py --zip M5.zip --months 2023-02 2023-03   # reads/writes data/XAUUSD/
"""

import argparse
import io
import zipfile
from pathlib import Path

import pandas as pd


def fill(histdata: pd.DataFrame, dukascopy: pd.DataFrame) -> pd.DataFrame:
    flat = (dukascopy.h_bid == dukascopy.l_bid) & (dukascopy.h_ask == dukascopy.l_ask)
    real = dukascopy[~flat & ~dukascopy.index.isin(histdata.index)]
    merged = pd.concat([histdata, real[histdata.columns]]).sort_index()
    assert merged.index.is_unique and merged.loc[histdata.index].equals(histdata), "HistData bars must survive unchanged"
    return merged


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--zip", type=Path, required=True, help="Dukascopy zip with M1/ and M5/ YYYY-MM.parquet")
    p.add_argument("--out", type=Path, default=Path("data"), help="default: data (gitignored)")
    p.add_argument("--symbol", default="XAUUSD")
    p.add_argument("--months", nargs="+", required=True)
    a = p.parse_args()
    base = a.out / a.symbol.upper()
    with zipfile.ZipFile(a.zip) as z:
        for m in a.months:
            for tf in ("M1", "M5"):
                hist = pd.read_parquet(base / tf / f"{m}.parquet")
                merged = fill(hist, pd.read_parquet(io.BytesIO(z.read(f"{tf}/{m}.parquet"))))
                d = base / f"{tf}_filled"
                d.mkdir(exist_ok=True)
                tmp = d / f"{m}.parquet.part"
                merged.to_parquet(tmp)
                tmp.replace(d / f"{m}.parquet")
                print(f"{m} {tf}: {len(hist):,} + {len(merged) - len(hist):,} filled = {len(merged):,}")


if __name__ == "__main__":
    main()
