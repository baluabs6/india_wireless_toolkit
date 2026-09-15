"""
india_wireless_toolkit
-----------------------
A toolkit covering current issues in India's wireless/telecom sector:
satcom spectrum delays, the 6 GHz Wi-Fi/5G-6G band split, last-mile fibre
duplication economics, and tracking regulatory press releases.

Modules:
    data_visualization  -- charts, interactive dashboard, state bubble map
    spectrum_simulation -- 6 GHz capacity, SINR/path-loss, Monte Carlo, THz, what-if
    infra_economics     -- duplicate vs shared cost model, NPV/IRR, sensitivity, breakeven
    news_scraper        -- TRAI/DoT/PIB scraper, SQLite archive, RSS, alerts, trends
    report_generator    -- combines everything into one HTML report
    db                  -- Redis caching layer (graceful fallback if unavailable)
    config_loader       -- loads config.yaml
"""

from . import data_visualization
from . import spectrum_simulation
from . import infra_economics
from . import news_scraper
from . import report_generator
from . import db
from . import config_loader

__version__ = "2.0.0"
__all__ = [
    "data_visualization",
    "spectrum_simulation",
    "infra_economics",
    "news_scraper",
    "report_generator",
    "db",
    "config_loader",
]
