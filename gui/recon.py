"""RECON tab — immersive intelligence view for the Winston dashboard.

Globe-first layout: the globe fills the entire tab as a hero element.
Stats, intel, and event data float as frameless overlays on top.
No panel borders, no boxes — clean cinematic aesthetic.

City selection: clicking a city marker on the globe updates all overlays
with city-specific data. Clicking empty space resets to global overview.

Data: static CITY_DATA dict for now. ReconDataFetcher class provides the
interface for future REST Countries API + Claude API integration.
"""

import time
import threading

from PyQt6.QtCore import Qt, QTimer, QObject, pyqtSignal
from PyQt6.QtGui import QFont, QPainter, QColor, QPen, QBrush
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame,
)

from gui.globe import GlobeWidget
from gui.command import WinstonCore, _mono, CMD_DIM

# ──────────────── Palette ────────────────
R_GLOW    = "#22c55e"   # primary neon green
R_BRIGHT  = "#e0f0e5"   # bright text
R_LABEL   = "#2a6b3f"   # dim labels
R_DIM     = "#1a3a2a"   # very dim
R_FAINT   = "#0d1f14"   # faintest — borders, separators
R_BG      = "#030a06"   # near-black background
R_OVERLAY = "rgba(3, 10, 6, 180)"  # semi-transparent overlay bg


# ──────────────── City data ────────────────
CITY_DATA = {
    "NYC": {"name": "New York City", "country": "US", "country_name": "United States", "native": "", "region": "Northeast", "lat": 40.7, "lon": -74.0, "alt_m": 10, "tz": "EST -5",
            "pop_city": "8.3M", "pop_metro": "20.1M", "density": "10,194/km²", "area_km2": 783, "climate": "Humid subtropical",
            "languages": "English", "religions": "Christianity, Judaism, Islam",
            "currency": "USD", "gdp": "$25.5T", "gdp_pc": "$76,399", "industries": "Finance, Tech, Media", "elevation": "10m"},
    "LAX": {"name": "Los Angeles", "country": "US", "country_name": "United States", "native": "", "region": "West Coast", "lat": 34.1, "lon": -118.2, "alt_m": 71, "tz": "PST -8",
            "pop_city": "3.9M", "pop_metro": "13.2M", "density": "3,276/km²", "area_km2": 1213, "climate": "Mediterranean",
            "languages": "English, Spanish", "religions": "Christianity, None, Judaism",
            "currency": "USD", "gdp": "$25.5T", "gdp_pc": "$76,399", "industries": "Entertainment, Tech, Aerospace", "elevation": "71m"},
    "LON": {"name": "London", "country": "GB", "country_name": "United Kingdom", "native": "", "region": "England", "lat": 51.5, "lon": -0.1, "alt_m": 11, "tz": "GMT +0",
            "pop_city": "8.8M", "pop_metro": "14.4M", "density": "5,666/km²", "area_km2": 1572, "climate": "Temperate oceanic",
            "languages": "English", "religions": "Christianity, Islam, None",
            "currency": "GBP", "gdp": "$3.1T", "gdp_pc": "$46,125", "industries": "Finance, Insurance, Tech", "elevation": "11m"},
    "PAR": {"name": "Paris", "country": "FR", "country_name": "France", "native": "", "region": "Île-de-France", "lat": 48.9, "lon": 2.3, "alt_m": 35, "tz": "CET +1",
            "pop_city": "2.2M", "pop_metro": "12.4M", "density": "20,755/km²", "area_km2": 105, "climate": "Temperate oceanic",
            "languages": "French", "religions": "Christianity, Islam, None",
            "currency": "EUR", "gdp": "$2.8T", "gdp_pc": "$42,330", "industries": "Tourism, Luxury, Aerospace", "elevation": "35m"},
    "TKY": {"name": "Tokyo", "country": "JP", "country_name": "Japan", "native": "東京都", "region": "Kantō", "lat": 35.7, "lon": 139.7, "alt_m": 40, "tz": "JST +9",
            "pop_city": "13.9M", "pop_metro": "37.4M", "density": "6,363/km²", "area_km2": 2194, "climate": "Humid subtropical",
            "languages": "Japanese", "religions": "Shinto, Buddhism, None",
            "currency": "JPY", "gdp": "$4.2T", "gdp_pc": "$33,815", "industries": "Electronics, Auto, Finance", "elevation": "40m"},
    "SHA": {"name": "Shanghai", "country": "CN", "country_name": "China", "native": "上海市", "region": "East China", "lat": 31.2, "lon": 121.5, "alt_m": 4, "tz": "CST +8",
            "pop_city": "24.9M", "pop_metro": "28.5M", "density": "3,926/km²", "area_km2": 6341, "climate": "Humid subtropical",
            "languages": "Mandarin", "religions": "Folk religion, Buddhism, None",
            "currency": "CNY", "gdp": "$17.7T", "gdp_pc": "$12,556", "industries": "Manufacturing, Finance, Shipping", "elevation": "4m"},
    "MUM": {"name": "Mumbai", "country": "IN", "country_name": "India", "native": "मुंबई", "region": "Maharashtra", "lat": 19.1, "lon": 72.9, "alt_m": 14, "tz": "IST +5:30",
            "pop_city": "12.5M", "pop_metro": "21.7M", "density": "20,634/km²", "area_km2": 603, "climate": "Tropical wet/dry",
            "languages": "Hindi, Marathi, English", "religions": "Hinduism, Islam, Christianity",
            "currency": "INR", "gdp": "$3.7T", "gdp_pc": "$2,612", "industries": "Finance, Film, Textiles", "elevation": "14m"},
    "SYD": {"name": "Sydney", "country": "AU", "country_name": "Australia", "native": "", "region": "New South Wales", "lat": -33.9, "lon": 151.2, "alt_m": 3, "tz": "AEST +10",
            "pop_city": "5.3M", "pop_metro": "5.3M", "density": "433/km²", "area_km2": 12368, "climate": "Humid subtropical",
            "languages": "English", "religions": "Christianity, None, Buddhism",
            "currency": "AUD", "gdp": "$1.7T", "gdp_pc": "$65,366", "industries": "Finance, Mining, Tourism", "elevation": "3m"},
    "MOW": {"name": "Moscow", "country": "RU", "country_name": "Russia", "native": "Москва", "region": "Central Federal District", "lat": 55.8, "lon": 37.6, "alt_m": 156, "tz": "MSK +3",
            "pop_city": "12.6M", "pop_metro": "17.1M", "density": "4,941/km²", "area_km2": 2561, "climate": "Humid continental",
            "languages": "Russian", "religions": "Orthodox Christianity, Islam, None",
            "currency": "RUB", "gdp": "$2.2T", "gdp_pc": "$15,345", "industries": "Energy, Defense, IT", "elevation": "156m"},
    "SAO": {"name": "São Paulo", "country": "BR", "country_name": "Brazil", "native": "", "region": "Southeast", "lat": -23.5, "lon": -46.6, "alt_m": 760, "tz": "BRT -3",
            "pop_city": "12.3M", "pop_metro": "22.0M", "density": "8,005/km²", "area_km2": 1521, "climate": "Humid subtropical",
            "languages": "Portuguese", "religions": "Christianity, Spiritism, None",
            "currency": "BRL", "gdp": "$2.1T", "gdp_pc": "$10,412", "industries": "Finance, Manufacturing, Tech", "elevation": "760m"},
    "CAI": {"name": "Cairo", "country": "EG", "country_name": "Egypt", "native": "القاهرة", "region": "Lower Egypt", "lat": 30.0, "lon": 31.2, "alt_m": 75, "tz": "EET +2",
            "pop_city": "10.1M", "pop_metro": "21.8M", "density": "19,376/km²", "area_km2": 528, "climate": "Hot desert",
            "languages": "Arabic", "religions": "Islam, Christianity",
            "currency": "EGP", "gdp": "$476B", "gdp_pc": "$4,295", "industries": "Textiles, Tourism, Petroleum", "elevation": "75m"},
    "NBO": {"name": "Nairobi", "country": "KE", "country_name": "Kenya", "native": "", "region": "East Africa", "lat": -1.3, "lon": 36.8, "alt_m": 1661, "tz": "EAT +3",
            "pop_city": "4.7M", "pop_metro": "5.4M", "density": "6,888/km²", "area_km2": 696, "climate": "Subtropical highland",
            "languages": "Swahili, English", "religions": "Christianity, Islam, Traditional",
            "currency": "KES", "gdp": "$113B", "gdp_pc": "$2,099", "industries": "Agriculture, Tourism, Tech", "elevation": "1661m"},
    "SIN": {"name": "Singapore", "country": "SG", "country_name": "Singapore", "native": "新加坡", "region": "Southeast Asia", "lat": 1.3, "lon": 103.8, "alt_m": 15, "tz": "SGT +8",
            "pop_city": "5.9M", "pop_metro": "5.9M", "density": "8,358/km²", "area_km2": 733, "climate": "Tropical rainforest",
            "languages": "English, Mandarin, Malay, Tamil", "religions": "Buddhism, Christianity, Islam",
            "currency": "SGD", "gdp": "$515B", "gdp_pc": "$87,884", "industries": "Finance, Electronics, Biomedical", "elevation": "15m"},
    "SEL": {"name": "Seoul", "country": "KR", "country_name": "South Korea", "native": "서울특별시", "region": "Sudogwon", "lat": 37.6, "lon": 127.0, "alt_m": 38, "tz": "KST +9",
            "pop_city": "9.7M", "pop_metro": "25.5M", "density": "16,000/km²", "area_km2": 605, "climate": "Humid continental",
            "languages": "Korean", "religions": "Christianity, Buddhism, None",
            "currency": "KRW", "gdp": "$1.7T", "gdp_pc": "$32,255", "industries": "Electronics, Auto, Shipbuilding", "elevation": "38m"},
    "BER": {"name": "Berlin", "country": "DE", "country_name": "Germany", "native": "", "region": "Brandenburg", "lat": 52.5, "lon": 13.4, "alt_m": 34, "tz": "CET +1",
            "pop_city": "3.7M", "pop_metro": "6.1M", "density": "4,206/km²", "area_km2": 892, "climate": "Temperate oceanic",
            "languages": "German", "religions": "Christianity, None, Islam",
            "currency": "EUR", "gdp": "$4.1T", "gdp_pc": "$48,717", "industries": "Auto, Engineering, Pharma", "elevation": "34m"},
    "DXB": {"name": "Dubai", "country": "AE", "country_name": "UAE", "native": "دبي", "region": "Persian Gulf", "lat": 25.3, "lon": 55.3, "alt_m": 5, "tz": "GST +4",
            "pop_city": "3.5M", "pop_metro": "3.5M", "density": "860/km²", "area_km2": 4114, "climate": "Hot desert",
            "languages": "Arabic, English", "religions": "Islam, Christianity, Hinduism",
            "currency": "AED", "gdp": "$499B", "gdp_pc": "$49,451", "industries": "Trade, Tourism, Real Estate", "elevation": "5m"},
    "BEI": {"name": "Beijing", "country": "CN", "country_name": "China", "native": "北京市", "region": "North China", "lat": 39.9, "lon": 116.4, "alt_m": 43, "tz": "CST +8",
            "pop_city": "21.5M", "pop_metro": "24.5M", "density": "1,313/km²", "area_km2": 16411, "climate": "Humid continental",
            "languages": "Mandarin", "religions": "Folk religion, Buddhism, None",
            "currency": "CNY", "gdp": "$17.7T", "gdp_pc": "$12,556", "industries": "Tech, Finance, Government", "elevation": "43m"},
    "IST": {"name": "Istanbul", "country": "TR", "country_name": "Turkey", "native": "İstanbul", "region": "Marmara", "lat": 41.0, "lon": 29.0, "alt_m": 39, "tz": "TRT +3",
            "pop_city": "15.8M", "pop_metro": "15.8M", "density": "2,965/km²", "area_km2": 5343, "climate": "Temperate oceanic",
            "languages": "Turkish", "religions": "Islam, Christianity, Judaism",
            "currency": "TRY", "gdp": "$1.1T", "gdp_pc": "$13,110", "industries": "Textiles, Auto, Tourism", "elevation": "39m"},
}

WORLD_STATS = {
    "internet_users": "5.56B",
    "urban_pct": "57.5%",
    "co2_mt": "37.4 Gt/yr",
}

REGIONAL_DATA = [
    ("ASIA",          "4.75B"),
    ("AFRICA",        "1.46B"),
    ("EUROPE",        "0.74B"),
    ("LATIN AMERICA", "0.66B"),
    ("NORTH AMERICA", "0.38B"),
    ("OCEANIA",       "0.05B"),
]

# Static intel briefs per city (placeholder for future Claude API)
_INTEL_BRIEFS = {
    "NYC": "Major financial hub. NYSE and NASDAQ handle ~$50T+ annual volume. "
           "High density urban core with significant critical infrastructure. "
           "Primary media and cultural influence center. Elevated transit dependency.",
    "LON": "Global financial center. London Stock Exchange among world's largest. "
           "Five international airports. Significant diplomatic presence with 170+ embassies. "
           "Key NATO intelligence hub.",
    "TKY": "World's largest metropolitan economy. Critical tech manufacturing corridor. "
           "Seismic risk zone — advanced earthquake early warning systems deployed. "
           "Imperial Palace district. Major port of Yokohama adjacent.",
    "SHA": "China's largest city by population. Pudong financial district hosts "
           "Shanghai Stock Exchange. World's busiest container port. "
           "Key Belt and Road Initiative node.",
    "MUM": "India's financial capital. Bombay Stock Exchange — oldest in Asia. "
           "Bollywood film industry produces 1,500+ films annually. "
           "Critical port infrastructure on Arabian Sea.",
    "SYD": "Australia's largest city and economic center. Major Pacific trade hub. "
           "Significant defense installations. Critical submarine cable landing point.",
    "MOW": "Russian Federation capital. Kremlin administrative complex. "
           "Moscow Metro serves 2.5B+ annual passengers. Key energy sector command center.",
    "PAR": "EU political influence center. UNESCO World Heritage site density among highest. "
           "Charles de Gaulle Airport — major European hub. Defense industry corridor.",
    "SAO": "Largest city in Southern Hemisphere. Brazil's economic engine — "
           "generates ~33% of national GDP. Major agribusiness command center.",
    "CAI": "Largest city in Arab world and Africa. Suez Canal administration hub. "
           "Significant archaeological assets. Key regional stability indicator.",
    "NBO": "East Africa's tech hub — 'Silicon Savannah'. Major UN administrative center "
           "(UNEP, UN-Habitat). Key wildlife conservation coordination point.",
    "SIN": "Strategic chokepoint — Strait of Malacca. World's busiest transshipment port. "
           "Major defense and intelligence hub. Financial center rivaling Hong Kong.",
    "SEL": "Major tech economy — Samsung, LG, Hyundai HQs. DMZ 35km north. "
           "Critical semiconductor manufacturing corridor. US military presence.",
    "BER": "German capital and EU's largest economy center. Significant cold war legacy "
           "infrastructure. Major startup hub. Key European transportation node.",
    "DXB": "UAE's commercial capital. Jebel Ali — world's largest man-made harbor. "
           "Major aviation hub — Emirates operates 250+ aircraft. Free trade zones.",
    "BEI": "PRC capital. Zhongnanhai government compound. "
           "Major military command infrastructure. Tech corridor — Zhongguancun district.",
    "IST": "Transcontinental city — Europe/Asia. Bosphorus Strait strategic chokepoint. "
           "Major cultural crossroads. NATO's southeast flank.",
    "LAX": "Second-largest US city. Major Pacific Rim trade gateway. "
           "Largest US port complex (LA + Long Beach). Entertainment industry capital.",
}


# ──────────────── Data fetcher ────────────────
class ReconDataFetcher(QObject):
    """Background data fetcher for RECON tab.

    Provides cached access to:
      - REST Countries API (future)
      - Claude API intel briefs (future)

    Currently returns static data from CITY_DATA and _INTEL_BRIEFS.
    Runs fetches on a background thread to not block UI.
    """

    data_ready = pyqtSignal(str, dict)   # (city_code, {merged data dict})
    intel_ready = pyqtSignal(str, str)   # (city_code, intel_text)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._cache = {}
        self._intel_cache = {}

    def fetch_city_data(self, code):
        if code in self._cache:
            self.data_ready.emit(code, self._cache[code])
            return
        t = threading.Thread(target=self._fetch_worker, args=(code,), daemon=True)
        t.start()

    def _fetch_worker(self, code):
        city = CITY_DATA.get(code)
        if not city:
            return
        data = dict(city)
        self._cache[code] = data
        self.data_ready.emit(code, data)

    def fetch_city_intel(self, city_name, country, code=""):
        if code in self._intel_cache:
            self.intel_ready.emit(code, self._intel_cache[code])
            return
        t = threading.Thread(target=self._intel_worker,
                             args=(city_name, country, code), daemon=True)
        t.start()

    def _intel_worker(self, city_name, country, code):
        brief = _INTEL_BRIEFS.get(code, f"Intelligence data for {city_name}, "
                                  f"{country} is being compiled. Stand by.")
        self._intel_cache[code] = brief
        time.sleep(0.3)
        self.intel_ready.emit(code, brief)


# ──────────────── Pop counter widget ────────────────
class PopCounter(QWidget):
    """Big high-tech world population counter with label and glow."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(58)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.setAlignment(Qt.AlignmentFlag.AlignLeft)

        # Sublabel
        self._sub = QLabel("WORLD POPULATION")
        self._sub.setFont(_mono(7))
        self._sub.setStyleSheet(f"color: {R_LABEL}; background: transparent; letter-spacing: 4px;")
        self._sub.setAlignment(Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(self._sub)

        # Big number
        self._digits = QLabel("8,200,000,000")
        f = _mono(22)
        f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 3)
        self._digits.setFont(f)
        self._digits.setStyleSheet(
            f"color: {R_GLOW}; background: transparent; font-weight: bold;"
        )
        self._digits.setAlignment(Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(self._digits)

    def set_value(self, pop_str):
        self._digits.setText(pop_str)

    def set_city_mode(self, city_name, country_name, coords_str):
        """Switch to city target display."""
        self._sub.setText("TARGET ACQUIRED")
        self._sub.setStyleSheet(f"color: {R_GLOW}; background: transparent; letter-spacing: 4px;")
        f = _mono(22)
        f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 3)
        self._digits.setFont(f)
        self._digits.setText(city_name.upper())
        self._digits.setStyleSheet(
            f"color: {R_BRIGHT}; background: transparent; font-weight: bold;"
        )

    def set_world_mode(self, pop_str):
        """Switch back to world population."""
        self._sub.setText("WORLD POPULATION")
        self._sub.setStyleSheet(f"color: {R_LABEL}; background: transparent; letter-spacing: 4px;")
        self._digits.setText(pop_str)
        self._digits.setStyleSheet(
            f"color: {R_GLOW}; background: transparent; font-weight: bold;"
        )


# ──────────────── Stat overlay (frameless) ────────────────
class StatOverlay(QWidget):
    """Frameless, semi-transparent stat block that floats over the globe."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setStyleSheet("background: transparent;")

        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(12, 8, 12, 8)
        self._layout.setSpacing(1)

    def body(self):
        return self._layout

    def paintEvent(self, event):
        """Draw semi-transparent dark background with subtle border."""
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QPen(QColor(R_DIM), 1))
        p.setBrush(QBrush(QColor(3, 10, 6, 200)))
        p.drawRoundedRect(1, 1, self.width() - 2, self.height() - 2, 6, 6)
        p.end()


def _overlay_header(text, layout):
    """Dim section header inside an overlay."""
    lbl = QLabel(text)
    lbl.setFont(_mono(8))
    lbl.setStyleSheet(f"color: {R_LABEL}; background: transparent; letter-spacing: 3px;")
    layout.addWidget(lbl)


def _overlay_row(label, value, layout, value_color=R_BRIGHT):
    """Single stat row: dim label left, bright value right."""
    row = QHBoxLayout()
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(4)

    lbl = QLabel(label)
    lbl.setFont(_mono(8))
    lbl.setStyleSheet(f"color: {R_LABEL}; background: transparent;")
    row.addWidget(lbl)

    row.addStretch()

    val = QLabel(value)
    val.setFont(_mono(9))
    val.setStyleSheet(f"color: {value_color}; background: transparent; font-weight: bold;")
    val.setAlignment(Qt.AlignmentFlag.AlignRight)
    row.addWidget(val)

    layout.addLayout(row)
    return val


def _thin_sep(layout):
    """Hairline separator."""
    sep = QFrame()
    sep.setFrameShape(QFrame.Shape.HLine)
    sep.setFixedHeight(1)
    sep.setStyleSheet(f"color: {R_FAINT}; background: {R_FAINT};")
    layout.addWidget(sep)


# ──────────────── Intel brief with typing effect ────────────────
class IntelBriefOverlay(QWidget):
    """Frameless intel brief with typing animation."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(12, 8, 12, 8)
        self._layout.setSpacing(2)

        _overlay_header("INTELLIGENCE BRIEF", self._layout)

        self._text = QLabel("Select a target on the globe.")
        self._text.setFont(_mono(7))
        self._text.setStyleSheet(f"color: {CMD_DIM}; background: transparent;")
        self._text.setWordWrap(True)
        self._text.setMinimumWidth(180)
        self._layout.addWidget(self._text)

        self._full_text = ""
        self._displayed_chars = 0
        self._typing = False
        self._type_timer = QTimer(self)
        self._type_timer.setInterval(12)
        self._type_timer.timeout.connect(self._type_tick)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QPen(QColor(R_DIM), 1))
        p.setBrush(QBrush(QColor(3, 10, 6, 200)))
        p.drawRoundedRect(1, 1, self.width() - 2, self.height() - 2, 6, 6)
        p.end()

    def set_loading(self):
        self._typing = False
        self._type_timer.stop()
        self._text.setText("COMPILING...")
        self._text.setStyleSheet(f"color: {R_GLOW}; background: transparent;")

    def set_brief(self, text):
        self._full_text = text
        self._displayed_chars = 0
        self._typing = True
        self._text.setText("")
        self._text.setStyleSheet(f"color: {R_BRIGHT}; background: transparent;")
        self._type_timer.start()

    def set_world_brief(self):
        self._typing = False
        self._type_timer.stop()
        self._text.setText("Select a target on the globe.")
        self._text.setStyleSheet(f"color: {CMD_DIM}; background: transparent;")

    def _type_tick(self):
        if not self._typing:
            self._type_timer.stop()
            return
        self._displayed_chars += 1
        if self._displayed_chars >= len(self._full_text):
            self._text.setText(self._full_text)
            self._typing = False
            self._type_timer.stop()
        else:
            self._text.setText(self._full_text[:self._displayed_chars] + "█")


# ──────────────── Event feed ────────────────
class EventFeed(QWidget):
    """Compact scrolling event feed with military timestamps."""

    def __init__(self, max_events=6, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._max = max_events

        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(12, 8, 12, 6)
        self._layout.setSpacing(1)

        _overlay_header("EVENT FEED", self._layout)

        self._labels = []
        placeholders = [
            ("RECON module initialized"),
            ("Sensor grid online"),
            ("Telemetry sync complete"),
        ]
        for msg in placeholders:
            self._add_label(msg)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QPen(QColor(R_DIM), 1))
        p.setBrush(QBrush(QColor(3, 10, 6, 200)))
        p.drawRoundedRect(1, 1, self.width() - 2, self.height() - 2, 6, 6)
        p.end()

    def _add_label(self, msg):
        t = time.gmtime()
        ts = f"{t.tm_hour:02d}{t.tm_min:02d}Z"
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        ts_lbl = QLabel(ts)
        ts_lbl.setFont(_mono(6))
        ts_lbl.setStyleSheet(f"color: {R_GLOW}; background: transparent;")
        ts_lbl.setFixedWidth(36)
        row.addWidget(ts_lbl)
        msg_lbl = QLabel(msg)
        msg_lbl.setFont(_mono(6))
        msg_lbl.setStyleSheet(f"color: {CMD_DIM}; background: transparent;")
        row.addWidget(msg_lbl, stretch=1)
        self._layout.addLayout(row)
        self._labels.append((ts_lbl, msg_lbl))

    def add_event(self, message):
        t = time.gmtime()
        ts = f"{t.tm_hour:02d}{t.tm_min:02d}Z"
        for i in range(len(self._labels) - 1, 0, -1):
            prev_ts, prev_msg = self._labels[i - 1]
            self._labels[i][0].setText(prev_ts.text())
            self._labels[i][1].setText(prev_msg.text())
        if self._labels:
            self._labels[0][0].setText(ts)
            self._labels[0][1].setText(message)


# ──────────────── Main RECON Tab ────────────────
class ReconTab(QWidget):
    """Immersive RECON tab — globe fills the space, stats float on top.

    Layout:
      - Top center:  Big world pop counter
      - Full area:   GlobeWidget (hero, fills everything)
      - Left float:  Stats overlay (world or city)
      - Right float: Intel brief + event feed
      - Bottom-right: Tiny Winston orb (no panel)
    """

    def __init__(self, winston_state=None, parent=None):
        super().__init__(parent)
        self._winston_state = winston_state
        self._current_city = None
        self._world_pop = 8_200_000_000
        self._refresh_counter = 0
        self.setStyleSheet(f"background: {R_BG};")

        # Data fetcher
        self._fetcher = ReconDataFetcher(self)
        self._fetcher.data_ready.connect(self._on_data_ready)
        self._fetcher.intel_ready.connect(self._on_intel_ready)

        # World population timer (1Hz)
        self._pop_timer = QTimer(self)
        self._pop_timer.setInterval(1000)
        self._pop_timer.timeout.connect(self._pop_tick)
        self._pop_timer.start()

        self._build_ui()

    def _build_ui(self):
        # Everything is a floating overlay on top of the globe.
        # Single QWidget with manual child positioning via resizeEvent.
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Main area: globe fills, everything floats on top
        self._main_area = QWidget()
        self._main_area.setStyleSheet(f"background: {R_BG};")
        root.addWidget(self._main_area, stretch=1)

        # Globe fills the main area
        self._globe = GlobeWidget(self._main_area)
        self._globe.city_clicked.connect(self._on_city_selected)

        # ── Pop counter (top-left, floating) ──
        self._pop_counter = PopCounter(self._main_area)

        # ── Left overlay: stats ──
        self._left_overlay = StatOverlay(self._main_area)
        self._left_overlay.setFixedWidth(260)
        self._build_left_world()

        # ── Winston orb (top-right, no frame) ──
        self._orb = WinstonCore(winston_state=self._winston_state,
                                show_label=False)
        self._orb.setParent(self._main_area)
        self._orb.setFixedSize(160, 160)
        self._orb.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        # ── Right overlay: intel + events (below Winston) ──
        self._right_container = QWidget(self._main_area)
        self._right_container.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._right_container.setStyleSheet("background: transparent;")
        right_layout = QVBoxLayout(self._right_container)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(6)
        self._right_container.setFixedWidth(260)

        self._intel_brief = IntelBriefOverlay()
        right_layout.addWidget(self._intel_brief)

        self._event_feed = EventFeed()
        right_layout.addWidget(self._event_feed)

        right_layout.addStretch()

        # ── City info overlay (hidden by default, below pop counter) ──
        self._city_info = QWidget(self._main_area)
        self._city_info.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._city_info.setStyleSheet("background: transparent;")
        self._city_info.setVisible(False)
        ci_layout = QVBoxLayout(self._city_info)
        ci_layout.setContentsMargins(0, 0, 0, 0)
        ci_layout.setSpacing(0)

        self._city_sub = QLabel("")
        self._city_sub.setFont(_mono(7))
        self._city_sub.setStyleSheet(
            f"color: {R_LABEL}; background: transparent; letter-spacing: 2px;"
        )
        ci_layout.addWidget(self._city_sub)

        self._city_coords = QLabel("")
        self._city_coords.setFont(_mono(6))
        self._city_coords.setStyleSheet(f"color: {R_DIM}; background: transparent;")
        ci_layout.addWidget(self._city_coords)

        self._set_world_view()

    def _build_left_world(self):
        """Build world-view stats in the left overlay."""
        lay = self._left_overlay.body()
        # Clear existing
        while lay.count():
            item = lay.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
            elif item.layout():
                while item.layout().count():
                    sub = item.layout().takeAt(0)
                    if sub.widget():
                        sub.widget().deleteLater()

        _overlay_header("GLOBAL STATS", lay)
        _thin_sep(lay)
        self._ov_pop = _overlay_row("POPULATION", f"{int(self._world_pop):,}", lay, R_GLOW)
        self._ov_inet = _overlay_row("INTERNET", WORLD_STATS["internet_users"], lay)
        self._ov_urban = _overlay_row("URBAN", WORLD_STATS["urban_pct"], lay)
        self._ov_co2 = _overlay_row("CO2", WORLD_STATS["co2_mt"], lay)

        _thin_sep(lay)
        _overlay_header("BY REGION", lay)

        self._ov_regions = []
        for region, pop in REGIONAL_DATA:
            val = _overlay_row(region, pop, lay)
            self._ov_regions.append(val)

        lay.addStretch()

    def _build_left_city(self, data):
        """Rebuild left overlay for city-specific data."""
        lay = self._left_overlay.body()
        while lay.count():
            item = lay.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
            elif item.layout():
                while item.layout().count():
                    sub = item.layout().takeAt(0)
                    if sub.widget():
                        sub.widget().deleteLater()

        _overlay_header("DEMOGRAPHICS", lay)
        _thin_sep(lay)
        _overlay_row("CITY POP", data.get("pop_city", "--"), lay, R_GLOW)
        _overlay_row("METRO", data.get("pop_metro", "--"), lay)
        _overlay_row("DENSITY", data.get("density", "--"), lay)
        _overlay_row("LANGUAGES", data.get("languages", "--"), lay)

        _thin_sep(lay)
        _overlay_header("GEOGRAPHY", lay)
        _overlay_row("AREA", f"{data.get('area_km2', '--')} km²", lay)
        _overlay_row("ELEVATION", data.get("elevation", f"{data.get('alt_m', '--')}m"), lay)
        _overlay_row("CLIMATE", data.get("climate", "--"), lay)

        _thin_sep(lay)
        _overlay_header("ECONOMY", lay)
        _overlay_row("GDP", data.get("gdp", "--"), lay, R_GLOW)
        _overlay_row("GDP/CAPITA", data.get("gdp_pc", "--"), lay)
        _overlay_row("CURRENCY", data.get("currency", "--"), lay)
        _overlay_row("INDUSTRIES", data.get("industries", "--"), lay)

        lay.addStretch()

    def resizeEvent(self, event):
        """Position globe and overlays on resize."""
        super().resizeEvent(event)
        self._position_overlays()

    def _position_overlays(self):
        """Manually position floating overlays within main_area."""
        area = self._main_area
        w = area.width()
        h = area.height()
        margin = 14

        # Globe fills the entire area
        self._globe.setGeometry(0, 0, w, h)

        # Pop counter: top-left
        pc_w = self._pop_counter.sizeHint().width()
        pc_h = self._pop_counter.sizeHint().height()
        self._pop_counter.setGeometry(margin, margin, max(pc_w, 320), pc_h)

        # City info: below pop counter, left-aligned
        ci_w = 300
        ci_h = 40
        self._city_info.setGeometry(margin, margin + pc_h + 2, ci_w, ci_h)

        # Left stats overlay: below pop counter (+ city info space)
        lw = self._left_overlay.width()
        top_offset = margin + pc_h + ci_h + 8
        lh = min(h - top_offset - margin, self._left_overlay.sizeHint().height())
        self._left_overlay.setGeometry(margin, top_offset, lw, lh)

        # Winston orb: top-right
        ow = self._orb.width()
        oh = self._orb.height()
        self._orb.setGeometry(w - ow - margin, margin, ow, oh)

        # Right overlay (intel + events): below Winston orb
        rw = self._right_container.width()
        right_top = margin + oh + 8
        rh = min(h - right_top - margin, self._right_container.sizeHint().height())
        self._right_container.setGeometry(w - rw - margin, right_top, rw, rh)

        # Ensure overlays are above the globe
        self._pop_counter.raise_()
        self._city_info.raise_()
        self._left_overlay.raise_()
        self._orb.raise_()
        self._right_container.raise_()

    # ── Population counter ──

    def _pop_tick(self):
        self._world_pop += 2.5
        pop_str = f"{int(self._world_pop):,}"
        if self._current_city is None:
            self._pop_counter.set_value(pop_str)
            self._ov_pop.setText(pop_str)

    # ── City selection ──

    def _on_city_selected(self, code):
        if code not in CITY_DATA:
            return

        self._current_city = code
        data = CITY_DATA[code]

        # Pop counter → city name
        lat = data.get("lat", 0)
        lon = data.get("lon", 0)
        lat_dir = "N" if lat >= 0 else "S"
        lon_dir = "E" if lon >= 0 else "W"
        coords = f"{abs(lat):.1f}°{lat_dir}  {abs(lon):.1f}°{lon_dir}"
        self._pop_counter.set_city_mode(data["name"], data["country_name"], coords)

        # City info sub-line
        self._city_sub.setText(f"{data['country_name']}  /  {data.get('region', '')}")
        self._city_coords.setText(f"{coords}   ALT {data.get('alt_m', 0)}m   {data.get('tz', '')}")
        self._city_info.setVisible(True)

        # Rebuild left overlay for city
        self._build_left_city(data)

        # Intel brief
        self._intel_brief.set_loading()
        self._fetcher.fetch_city_intel(data["name"], data["country_name"], code)

        # Event feed
        self._event_feed.add_event(f"Target: {data['name']}")

        self._fetcher.fetch_city_data(code)

    def _on_data_ready(self, code, data):
        if code == self._current_city:
            self._build_left_city(data)

    def _on_intel_ready(self, code, text):
        if code == self._current_city:
            self._intel_brief.set_brief(text)

    def _on_globe_reset(self):
        self._set_world_view()

    def _set_world_view(self):
        self._current_city = None
        pop_str = f"{int(self._world_pop):,}"
        self._pop_counter.set_world_mode(pop_str)
        self._city_info.setVisible(False)

        self._build_left_world()

        self._intel_brief.set_world_brief()

    def frame_tick(self):
        """Called from main.py's frame loop for data refresh."""
        self._refresh_counter += 1
        if self._refresh_counter % 8 != 0:
            return

    def showEvent(self, event):
        super().showEvent(event)
        self._position_overlays()
        if not hasattr(self, '_reset_timer'):
            self._reset_timer = QTimer(self)
            self._reset_timer.setInterval(250)
            self._reset_timer.timeout.connect(self._check_globe_reset)
        self._reset_timer.start()

    def hideEvent(self, event):
        super().hideEvent(event)
        if hasattr(self, '_reset_timer'):
            self._reset_timer.stop()

    def _check_globe_reset(self):
        if self._current_city is not None and not self._globe._zoomed:
            self._on_globe_reset()
