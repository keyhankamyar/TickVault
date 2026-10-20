DUKASCOPY_DATA_FEED_BASE = "https://datafeed.dukascopy.com/datafeed/"
FXMACRODATA_API_BASE = "https://api.fxmacrodata.com/v1"


PIPET_SIZE_REGISTRY: dict[str, float] = {
    # Majors: ------
    "EURUSD": 1e-5,
    "AUDUSD": 1e-5,
    "GBPUSD": 1e-5,
    "NZDUSD": 1e-5,
    "USDCAD": 1e-5,
    "USDCHF": 1e-5,
    "USDJPY": 1e-3,
    # Metals: ------
    "XAUUSD": 1e-3,
    "XAGUSD": 1e-3,
    # Crypto: ------
    "ETHUSD": 0.1,
    "BTCUSD": 0.1,
}

VOLUME_SCALE = 1e6
