"""Temperature panel — works on bare Linux, macOS, and WSL2 (with workarounds).

Detection order:
1. Native psutil.sensors_temperatures()  — works on bare Linux/macOS
2. LibreHardwareMonitor HTTP endpoint     — best WSL/Windows results
3. PowerShell WMI ACPI thermal zones      — basic WSL/Windows fallback
4. Show helpful "set up" message

LibreHardwareMonitor setup (highly recommended on Windows/WSL):
  1. winget install LibreHardwareMonitor.LibreHardwareMonitor
  2. Run LibreHardwareMonitor.exe
  3. Options → Remote Web Server → Run (default port 8085)
  4. Winston picks it up automatically
"""
import json
import os
import platform
import shutil
import subprocess
import time
import urllib.request
import urllib.error

import psutil
from rich.text import Text

from panels.base import fit_bar_width, FILLED, EMPTY
from theme import LABEL, SECONDARY, MEDIUM, DIM, BRIGHT, heat_temp, heat_temp_empty
from panels import lhm

# Module-level aliases — keep call sites short. Color decisions are owned
# by theme.py; we just bind shorter names locally.
_temp_color = heat_temp
_temp_empty = heat_temp_empty


# ──────────────── Backend detection ────────────────
def _is_wsl():
    """True if we're running inside WSL."""
    if platform.system() != "Linux":
        return False
    try:
        with open("/proc/version", "r") as f:
            content = f.read().lower()
            return "microsoft" in content or "wsl" in content
    except OSError:
        return False


def _try_native():
    """Try psutil.sensors_temperatures() — works on bare Linux/macOS."""
    try:
        sensors = psutil.sensors_temperatures()
    except (AttributeError, NotImplementedError):
        return None
    except Exception:
        return None
    if not sensors:
        return None

    readings = []
    for chip, entries in sensors.items():
        for entry in entries:
            label = entry.label or chip
            readings.append((label, entry.current, entry.high))
    return readings if readings else None


def _wsl_host_ip():
    """Get the Windows host IP from inside WSL.
    On WSL2, this is the default gateway. Returns None if not in WSL or detection fails.
    """
    if not _is_wsl():
        return None
    try:
        # Read /proc/net/route — the default route's gateway is the Windows host.
        # Format: Iface Destination Gateway Flags ...
        # Default route has Destination = "00000000"
        with open("/proc/net/route", "r") as f:
            lines = f.readlines()
        for line in lines[1:]:  # skip header
            parts = line.split()
            if len(parts) >= 3 and parts[1] == "00000000":
                # Gateway is in hex, little-endian
                gw_hex = parts[2]
                # Convert: "0140A8C0" → 192.168.1.1
                octets = [int(gw_hex[i:i+2], 16) for i in (6, 4, 2, 0)]
                return ".".join(str(o) for o in octets)
    except (OSError, ValueError, IndexError):
        pass
    return None


def _try_lhm_http(host=None, port=8085, timeout=0.5, data=None):
    """Parse LibreHardwareMonitor sensor data into a list of (label, temp, high) readings.

    Either pass pre-fetched data via the `data` arg (preferred — read from
    the shared lhm poller cache), OR fall back to making the HTTP call here
    if data is None (legacy compatibility / startup edge cases).

    The HTTP fallback should rarely run — the panel update path uses the
    cached data from panels.lhm.
    """
    if data is None:
        # Fallback: make the HTTP call ourselves
        host = host or "localhost"
        url = f"http://{host}:{port}/data.json"
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                data = json.loads(r.read().decode())
        except (urllib.error.URLError, OSError, json.JSONDecodeError, TimeoutError):
            return None

    # The response is a nested tree: { Children: [ {Text: "PC", Children: [...]} ] }
    # We walk it, picking out any sensor where SensorType == "Temperature"
    # (the field is sometimes "Type" depending on LHM version, so we check both)
    readings = []

    # Labels that are CONFIG VALUES from BIOS, not actual live temp readings.
    SKIP_LABELS = {
        "critical temp", "warning temp", "critical temperature", "warning temperature",
        "tj. max", "tj max", "tjmax", "max temp", "shutdown temp",
    }

    def smart_label(parent_chain, leaf_label):
        """Given the chain of parent node names and the leaf label,
        produce a useful short name. Examples:
          ['ROG STRIX B550-F', 'Temperatures', 'Thermal Sensor 1'] -> 'Mobo Sensor 1'
          ['AMD Ryzen 7 5800X', 'Temperatures', 'Core (Tctl/Tdie)'] -> 'CPU Tctl'
          ['NVIDIA RTX 4070', 'Temperatures', 'GPU Hot Spot'] -> 'GPU Hot Spot'
          ['Kraken Z73', 'Temperatures', 'Liquid'] -> 'AIO Liquid'
        """
        device_str = " ".join(parent_chain).lower()
        leaf_lower = leaf_label.lower().strip()

        # Detect device type from parent chain
        if "ryzen" in device_str or "intel" in device_str or "core(tm)" in device_str:
            device = "CPU"
        elif "nvidia" in device_str or "geforce" in device_str or "radeon" in device_str:
            device = "GPU"
        elif "kraken" in device_str or "corsair" in device_str or "aio" in device_str:
            device = "AIO"
        elif any(mb in device_str for mb in ("rog ", "strix", "tuf ", "prime ", "msi ",
                                              "gigabyte", "asrock", "b550", "b650",
                                              "x570", "x670", "z690", "z790")):
            device = "Mobo"
        elif "ssd" in device_str or "nvme" in device_str:
            device = "SSD"
        else:
            device = ""

        # Clean up the leaf label
        if leaf_lower.startswith("thermal sensor"):
            # "Thermal Sensor 1" → "Sensor 1"
            short = leaf_label.replace("Thermal ", "").replace("thermal ", "")
        elif leaf_lower in ("temperature", "temp"):
            short = ""
        elif leaf_lower in ("core (tctl/tdie)", "core (tctl)", "tctl/tdie", "tctl"):
            short = "Tctl"
        elif leaf_lower.startswith("ccd") or "(tdie)" in leaf_lower:
            # "CCD1 (Tdie)" → "CCD1"
            short = leaf_label.split("(")[0].strip()
        elif leaf_lower in ("cpu package", "package"):
            short = "Package"
        elif leaf_lower == "core average":
            short = "Avg Core"
        elif leaf_lower.startswith("cpu core #"):
            n = leaf_label.split("#")[-1].strip()
            short = f"Core {n}"
        elif leaf_lower in ("liquid", "water"):
            short = "Liquid"
        else:
            short = leaf_label

        # ── Avoid double-prefixing ──
        # If the leaf ALREADY starts with the device name (e.g. "GPU Hot Spot"
        # under an NVIDIA parent), don't add the device prefix again.
        if device and short.lower().startswith(device.lower() + " "):
            return short
        if device and short.lower() == device.lower():
            return short

        if device and short:
            return f"{device} {short}"
        elif device:
            return device
        else:
            return short or leaf_label

    def walk(node, parent_chain):
        if not isinstance(node, dict):
            return
        if "Value" in node and isinstance(node["Value"], str):
            val_str = node["Value"]
            if "°C" in val_str:
                try:
                    num = float(val_str.replace("°C", "").strip())
                    label = node.get("Text", "?")
                    label_lower = label.lower().strip()

                    if label_lower in SKIP_LABELS:
                        pass
                    elif num < 5 or num > 130:
                        pass
                    else:
                        # Build a smart label from the parent chain context
                        pretty = smart_label(parent_chain, label)
                        readings.append((pretty, num, None))
                except (ValueError, TypeError):
                    pass

        children = node.get("Children", [])
        if isinstance(children, list):
            new_chain = parent_chain + [node.get("Text", "")]
            for child in children:
                walk(child, new_chain)

    walk(data, [])
    return readings if readings else None


def _try_powershell_wmi(timeout=2.0):
    """Try PowerShell WMI MSAcpi_ThermalZoneTemperature.

    Works from WSL by calling powershell.exe (always on PATH in WSL).
    Returns Kelvin*10 — convert to Celsius.
    """
    if not shutil.which("powershell.exe"):
        return None

    # Get all thermal zones, output as: zone_name|temp_in_kelvin*10
    ps_script = (
        '$zones = Get-WmiObject MSAcpi_ThermalZoneTemperature -Namespace "root/wmi" '
        '-ErrorAction SilentlyContinue; '
        'foreach ($z in $zones) { '
        '  Write-Output ($z.InstanceName + "|" + $z.CurrentTemperature) '
        '}'
    )

    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", ps_script],
            capture_output=True, text=True, timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None

    if result.returncode != 0:
        return None

    readings = []
    for line in result.stdout.strip().split("\n"):
        line = line.strip()
        if not line or "|" not in line:
            continue
        try:
            zone, temp_str = line.rsplit("|", 1)
            kelvin_x10 = int(temp_str.strip())
            celsius = (kelvin_x10 / 10.0) - 273.15
            # Skip absurd values (some boards report 0 or 27.85°C placeholder)
            if celsius < 1 or celsius > 150:
                continue
            # Clean up the zone name: ACPI\ThermalZone\CPUZ_0 → CPUZ_0
            label = zone.split("\\")[-1] if "\\" in zone else zone
            readings.append((label, celsius, None))
        except (ValueError, IndexError):
            continue

    return readings if readings else None


# ──────────────── Panel ────────────────
class TempsPanel:
    """Temperature readings panel.

    The `refresh_sec` arg controls how often we actually re-fetch from
    the upstream source (LHM HTTP, PowerShell WMI). The display layer
    can call update() much more often — we just return cached readings
    until enough time has passed since the last real fetch.
    """
    DEFAULT_REFRESH_SEC = 3

    def __init__(self, refresh_sec=None):
        self.refresh_sec = refresh_sec if refresh_sec is not None else self.DEFAULT_REFRESH_SEC
        self.readings = []
        self.backend = None        # "native" | "lhm" | "wmi" | None
        self.help_message = None
        self._last_fetch_at = 0.0  # monotonic time of last real fetch

    def update(self):
        """Update temps if enough wall-clock time has passed since last fetch."""
        now = time.monotonic()
        if (now - self._last_fetch_at) < self.refresh_sec and self.backend is not None:
            return
        self._last_fetch_at = now
        self._do_update()

    def _do_update(self):
        # Try in order of quality
        result = _try_native()
        if result:
            self.readings = self._sorted(result)
            self.backend = "native"
            self.help_message = None
            return

        # Only try Windows backends if we look like we're on WSL
        if _is_wsl():
            # LHM data comes from the shared background poller (panels.lhm).
            # Reading from the cache is essentially free — no blocking HTTP
            # call from the UI thread.
            from panels import lhm
            lhm_data = lhm.get_data()
            if lhm_data is not None:
                result = _try_lhm_http(data=lhm_data)
                if result:
                    self.readings = self._sorted(result)
                    self.backend = "lhm"
                    self.help_message = None
                    return

            result = _try_powershell_wmi()
            if result:
                self.readings = self._sorted(result)
                self.backend = "wmi"
                self.help_message = None
                return

            # WSL with no working backend — give helpful instructions
            self.readings = []
            self.backend = None
            self.help_message = "wsl"
            return

        # Non-WSL Linux/macOS without sensors
        self.readings = []
        self.backend = None
        self.help_message = "generic"

    def _sorted(self, readings):
        """Summarize to one row per hardware category, showing worst-case temp.

        TEMPS panel is for at-a-glance overheat detection. Each device gets
        ONE row showing its hottest meaningful sensor. For deep details on
        a specific device (like all 3 GPU temps), use that device's panel.
        """
        # Group readings by hardware category
        # Categories: CPU, GPU, AIO, MOBO, SSD, OTHER
        def category_of(label):
            l = label.lower()
            if l.startswith("cpu"): return "CPU"
            if l.startswith("gpu"): return "GPU"
            if l.startswith("aio"): return "AIO"
            if l.startswith("ssd"): return "SSD"
            if l.startswith("mobo"): return "MOBO"
            return "OTHER"

        # For each category, pick the worst-case sensor that's meaningful
        # Some sensors are more representative than others — prefer those.
        # Lower priority number = more representative (we'll pick the worst-case
        # within the highest-priority sensor type that exists).
        def sensor_preference(label):
            l = label.lower()
            # CPU: prefer Tctl/Package over individual cores or CCDs
            if "tctl" in l or "package" in l: return 0
            if "ccd" in l: return 1
            if "core average" in l or "avg core" in l: return 1
            if "core" in l: return 2
            # GPU: prefer Hot Spot (the alarm value) for the summary row
            if "hot spot" in l: return 0
            if "core" in l and l.startswith("gpu"): return 1
            if "memory" in l: return 2
            # AIO: prefer real coolant readings, deprioritize generic/critical sensors
            if "liquid" in l or "coolant" in l or "water" in l: return 0
            # Anything called "critical" on an AIO is almost always a firmware
            # placeholder (often stuck at 85.0). Push way down the priority list.
            if "critical" in l: return 9
            # MOBO: any sensor, prefer numbered ones
            if "sensor" in l: return 0
            return 5

        # Filter out values that look like firmware defaults rather than real
        # readings. Common LHM placeholder values for sensors a device doesn't
        # actually have:
        #   85.0 / 85.5 — NZXT Kraken "missing sensor" default
        #   100.0 — generic motherboard placeholder
        # We treat these as suspicious if they're EXACTLY at the placeholder
        # value (real sensors fluctuate, even idle ones drift by 0.1-0.5°C).
        SUSPICIOUS_DEFAULTS = {85.0, 100.0}

        def is_likely_placeholder(label, current):
            """Heuristic: if a non-essential sensor sits at a suspicious round
            value, it's probably a firmware default we should skip."""
            l = label.lower()
            # Don't filter out CPU/GPU primary sensors even if they happen to
            # land on a placeholder value — those are usually real.
            if "tctl" in l or "package" in l or "hot spot" in l or "liquid" in l:
                return False
            # Anything called "critical" at exactly 85.0 — definitely fake
            if "critical" in l and current in SUSPICIOUS_DEFAULTS:
                return True
            return False

        # Bucket readings by category, filtering out likely placeholders
        buckets = {}
        for label, current, high in readings:
            if is_likely_placeholder(label, current):
                continue  # skip this firmware-default value
            cat = category_of(label)
            buckets.setdefault(cat, []).append((label, current, high))

        # For each bucket, pick the BEST (most representative) sensor.
        # Tiebreaker: highest temperature wins (that's the alarm condition).
        summary = []
        for cat, items in buckets.items():
            items.sort(key=lambda r: (sensor_preference(r[0]), -r[1]))
            best = items[0]
            _label, current, high = best
            # Just the category name. The TEMPS panel is for "is X overheating?"
            # at a glance — specific sensor identity isn't useful here.
            # (For details on a specific device, use that device's panel.)
            summary.append((cat, current, high))

        # Order by category importance for display
        order = {"CPU": 0, "GPU": 1, "AIO": 2, "SSD": 3, "MOBO": 4, "OTHER": 5}
        summary.sort(key=lambda r: order.get(r[0], 99))
        return summary[:6]

    def render(self, width=None):
        if width is None:
            width = 30

        text = Text()

        if not self.readings:
            if self.help_message == "wsl":
                text.append("(no sensors detected)\n", style=SECONDARY)
                text.append("install ", style=DIM)
                text.append("LibreHardwareMonitor\n", style=MEDIUM)
                text.append("on windows for full temps:\n", style=DIM)
                text.append("  winget install \n", style=BRIGHT)
                text.append("  LibreHardwareMonitor.\n", style=BRIGHT)
                text.append("  LibreHardwareMonitor", style=BRIGHT)
                return text

            text.append("(no temp sensors)\n", style=SECONDARY)
            text.append("install lm-sensors and\nrun sudo sensors-detect", style=DIM)
            return text

        # Backend indicator (top-right corner subtle)
        backend_label = {"native": "lm-sensors", "lhm": "LHM", "wmi": "WMI"}.get(self.backend, "")
        if backend_label:
            text.append(f"via {backend_label}\n", style=DIM)

        label_w = min(14, max(len(r[0]) for r in self.readings))
        prefix_chars = label_w + 2
        suffix_chars = 6  # " 100°C"
        bar_width = fit_bar_width(width, prefix_chars, suffix_chars,
                                   min_width=4, max_width=80)

        for label, current, high in self.readings:
            scale_max = high if (high and high > 0) else 100
            pct = min(100, max(0, (current / scale_max) * 100))
            fg = _temp_color(current)
            bg = _temp_empty(current)
            filled_count = int((pct / 100) * bar_width)
            empty_count = bar_width - filled_count

            # Label gets bolded for visual weight, AND colored to match the temp.
            # Cool stuff = green label, hot stuff = red label — eye lands on hot rows.
            label_style = f"bold {fg}"

            display_label = label[:label_w].ljust(label_w)
            text.append(f"{display_label}  ", style=label_style)
            text.append(FILLED * filled_count, style=fg)
            text.append(EMPTY * empty_count, style=bg)
            text.append(f" {current:4.0f}°C\n", style=label_style)

        return text

    def csv_headers(self):
        return ["temp_max_c"]

    def csv_columns(self):
        if self.readings:
            return [self.readings[0][1]]
        return [0.0]