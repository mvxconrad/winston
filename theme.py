"""WINSTON theme.

All color decisions live here. Panels import these constants and
heat-mapping helpers — they never define colors locally.

Why: keeps style consistent across panels, makes recoloring the entire
app a one-file change. Touch this file to switch from green-hacker
to amber-CRT to whatever; everything follows.
"""

# ──────────────── Brightness tiers (text styling) ────────────────
# Three-tier hierarchy: BRIGHT for primary values, MEDIUM for secondary,
# DIM for tertiary/background. SECONDARY is for delimiters, LABEL for
# panel headers / row labels.
BRIGHT      = "bright_green"
MEDIUM      = "green"
DIM         = "grey50"
LABEL       = "bold bright_green"
SECONDARY   = "grey50"
HEADER      = "bold bright_cyan"
SUBTITLE    = "italic bright_green"
BORDER      = "bright_green"

# Backgrounds inside bars: dim foreground for empty cells.
EMPTY_DIM         = "color(22)"   # used when bar is mostly empty
EMPTY_VERY_DIM    = "color(235)"  # used when bar is mostly full (focus stays on fill)


# ──────────────── Heatmap palette ────────────────
# A 7-stop gradient from cool to hot. Applies to ANY percentage-based
# value (CPU%, RAM%, disk%, util%) so the visual language is consistent
# across the whole app.
#
# The hex values were picked to span perceptual color space evenly —
# you actually see distinct steps as values rise rather than a sudden
# green→yellow→red flip.
HEAT_PALETTE = [
    "#1a8c1a",  # 0:  deep green       — idle / cool
    "#33b033",  # 1:  green             — light load
    "#7acc33",  # 2:  yellow-green      — comfortable load
    "#cccc33",  # 3:  yellow            — moderate load
    "#e69933",  # 4:  orange            — high load
    "#e64d33",  # 5:  red-orange        — heavy load
    "#cc1a1a",  # 6:  red               — critical
]

# Breakpoints — value < BREAKPOINTS[i] gets HEAT_PALETTE[i].
# Last bucket catches everything above the final breakpoint.
HEAT_BREAKS_PCT = [10, 30, 50, 70, 85, 95]    # for percentages (0–100)
HEAT_BREAKS_TEMP = [40, 55, 65, 75, 85, 95]   # for temperatures (°C)


def _pick_from_palette(value, breaks):
    """Walk the breakpoints to pick a palette index."""
    for i, threshold in enumerate(breaks):
        if value < threshold:
            return HEAT_PALETTE[i]
    return HEAT_PALETTE[-1]


def heat_pct(percent):
    """Map a 0–100 percentage to a heatmap color.

    Use this for: CPU%, RAM%, disk%, GPU util%, VRAM% — anything where
    "100% = hot/bad" is the convention.
    """
    return _pick_from_palette(max(0, min(100, percent)), HEAT_BREAKS_PCT)


def heat_temp(celsius):
    """Map a temperature (°C) to a heatmap color.

    Use this for any sensor reading (CPU temp, GPU temp, AIO temp, etc.).
    Breakpoints are tuned for typical computer hardware ranges where
    >85°C is concerning and >95°C is critical.
    """
    return _pick_from_palette(celsius, HEAT_BREAKS_TEMP)


def heat_pct_empty(percent):
    """Background color for the EMPTY portion of a bar.

    Always dim. When the bar is mostly empty, use a slightly visible dim
    so you can see the bar's overall extent. When the bar is mostly full,
    use near-black so the colored fill stays the visual focus.
    """
    if percent < 50:
        return EMPTY_DIM
    return EMPTY_VERY_DIM


def heat_temp_empty(celsius):
    """Background color for the empty portion of a TEMP bar."""
    if celsius < 65:
        return EMPTY_DIM
    return EMPTY_VERY_DIM


# ──────────────── Health levels (3-tier categorical) ────────────────
# When you need a coarser GREEN/YELLOW/RED status (not a smooth gradient),
# use these. e.g. "is this single value OK or alarming?"
class HealthLevel:
    OK = HEAT_PALETTE[1]         # green
    WARNING = HEAT_PALETTE[3]    # yellow
    CRITICAL = HEAT_PALETTE[6]   # red


def health_pct(percent):
    """Bucket a percentage into OK/WARNING/CRITICAL."""
    if percent < 50:
        return HealthLevel.OK
    elif percent < 80:
        return HealthLevel.WARNING
    return HealthLevel.CRITICAL


def health_temp(celsius):
    """Bucket a temperature into OK/WARNING/CRITICAL."""
    if celsius < 65:
        return HealthLevel.OK
    elif celsius < 85:
        return HealthLevel.WARNING
    return HealthLevel.CRITICAL