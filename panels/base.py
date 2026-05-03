"""Rendering helpers used across panels.

Color decisions live in `theme.py`. This module is just the visual
primitives (bars, sparklines, braille graphs) and number formatters.

For backwards compat we re-export the heat helpers from theme so panels
that still import them from here keep working.
"""
from theme import (
    heat_pct as heatmap_color,
    heat_pct_empty as empty_color,
    heat_temp,
    heat_temp_empty,
    health_pct as health_for,
    HealthLevel,
)


# ──────────────── Sparklines ────────────────
SPARKLINE_CHARS = "▁▂▃▅▆▇█"

def sparkline(values, width=30):
    if not values:
        return ""
    recent = list(values)[-width:]
    chars = []
    for v in recent:
        idx = min(int(v / 100 * len(SPARKLINE_CHARS)), len(SPARKLINE_CHARS) - 1)
        chars.append(SPARKLINE_CHARS[idx])
    return "".join(chars)


# ──────────────── Bar gauges ────────────────
FILLED = "▓"
EMPTY = "·"

def bar_gauge(percent, width=20, filled_char=FILLED, empty_char=EMPTY):
    percent = max(0, min(100, percent))
    filled = int((percent / 100) * width)
    return filled_char * filled + empty_char * (width - filled)


def fit_bar_width(panel_width, prefix_chars=0, suffix_chars=0,
                   min_width=6, max_width=80):
    """Compute bar width from panel width."""
    avail = panel_width - prefix_chars - suffix_chars
    return max(min_width, min(max_width, avail))


# ──────────────── Braille graph ────────────────
_BRAILLE_BITS = {
    (0, 0): 0x01, (1, 0): 0x08,
    (0, 1): 0x02, (1, 1): 0x10,
    (0, 2): 0x04, (1, 2): 0x20,
    (0, 3): 0x40, (1, 3): 0x80,
}

def braille_graph(values, width=40, height=4, max_val=100):
    """Filled-area braille graph. Returns list of row strings."""
    if not values:
        return [" " * width for _ in range(height)]

    pixel_width = width * 2
    pixel_height = height * 4
    recent = list(values)[-pixel_width:]
    pad = pixel_width - len(recent)
    points = [None] * pad + recent

    grid = [[False] * pixel_width for _ in range(pixel_height)]
    for x, v in enumerate(points):
        if v is None:
            continue
        v = max(0, min(max_val, v))
        y = pixel_height - 1 - int((v / max_val) * (pixel_height - 1))
        for fill_y in range(y, pixel_height):
            grid[fill_y][x] = True

    rows = []
    for char_row in range(height):
        row_chars = []
        for char_col in range(width):
            code = 0x2800
            for (dx, dy), bit in _BRAILLE_BITS.items():
                px = char_col * 2 + dx
                py = char_row * 4 + dy
                if grid[py][px]:
                    code |= bit
            row_chars.append(chr(code))
        rows.append("".join(row_chars))
    return rows


# ──────────────── Number formatting ────────────────
def fmt_bytes(n):
    """Format a byte count with proper unit suffix.
    1024 → 1.0KB, 1048576 → 1.0MB, etc.
    Bytes-only values keep just "B" (no "BB").
    """
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    for unit in units:
        if abs(n) < 1024 or unit == units[-1]:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}{units[-1]}"


def fmt_rate(bytes_per_sec):
    return fmt_bytes(bytes_per_sec) + "/s"