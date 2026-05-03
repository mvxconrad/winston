"""GPU panel.

Tries pynvml, then nvidia-smi for the basic GPU stats (util, VRAM, die temp,
power draw). Extra temps (Hot Spot, Memory Junction) come from the shared
LHM poller (panels.lhm) — nvidia-smi doesn't expose those values.
"""
import shutil
import subprocess

from rich.text import Text

from panels.base import (bar_gauge, health_for, fit_bar_width, fmt_bytes,
                          empty_color, heatmap_color, FILLED, EMPTY)
from theme import LABEL, SECONDARY, BRIGHT, MEDIUM, DIM, heat_temp


class GpuPanel:
    def __init__(self):
        self.gpus = []
        self.backend = None
        self.error = None
        self._initialized = False

        # Extra temps from LHM, indexed by simplified name.
        # Populated by _fetch_lhm_extras() which reads from panels.lhm cache.
        self.lhm_temps = {}    # {"hot_spot": 58.0, "memory": 54.0, "core": 46.0}

    def _try_pynvml(self):
        try:
            # pynvml is the legacy name; the project was renamed to
            # nvidia-ml-py upstream but pynvml still works fine and is
            # what's installed. Suppress the FutureWarning at import.
            import warnings
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", FutureWarning)
                import pynvml
            pynvml.nvmlInit()
            self._pynvml = pynvml
            self.backend = "pynvml"
            return True
        except Exception:
            return False

    def _try_nvidia_smi(self):
        if shutil.which("nvidia-smi"):
            self.backend = "nvidia-smi"
            return True
        return False

    def _init_backend(self):
        self._initialized = True
        if self._try_pynvml():
            return
        if self._try_nvidia_smi():
            return
        self.backend = None

    def update(self):
        if not self._initialized:
            self._init_backend()
        if self.backend is None:
            self.gpus = []
            return
        try:
            if self.backend == "pynvml":
                self._update_pynvml()
            else:
                self._update_nvidia_smi()
        except Exception as e:
            self.error = str(e)
            self.gpus = []

        # Refresh LHM temps every tick. Reading from the shared LHM poller
        # is essentially free (just a dict lookup) — no need to throttle.
        self._fetch_lhm_extras()

    def _update_pynvml(self):
        nv = self._pynvml
        n = nv.nvmlDeviceGetCount()
        out = []
        for i in range(n):
            h = nv.nvmlDeviceGetHandleByIndex(i)
            name = nv.nvmlDeviceGetName(h)
            if isinstance(name, bytes):
                name = name.decode()
            util = nv.nvmlDeviceGetUtilizationRates(h).gpu
            mem = nv.nvmlDeviceGetMemoryInfo(h)
            try:
                temp = nv.nvmlDeviceGetTemperature(h, nv.NVML_TEMPERATURE_GPU)
            except Exception:
                temp = None
            # Power draw (current) and power limit (max). NVML returns mW.
            try:
                power = nv.nvmlDeviceGetPowerUsage(h) / 1000.0  # W
            except Exception:
                power = None
            try:
                power_limit = nv.nvmlDeviceGetEnforcedPowerLimit(h) / 1000.0  # W
            except Exception:
                power_limit = None
            out.append({
                "name": name, "util": util,
                "mem_used": mem.used, "mem_total": mem.total,
                "temp": temp,
                "power": power, "power_limit": power_limit,
            })
        self.gpus = out

    def _update_nvidia_smi(self):
        result = subprocess.run(
            ["nvidia-smi",
             "--query-gpu=name,utilization.gpu,memory.used,memory.total,"
             "temperature.gpu,power.draw,power.limit",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=2,
        )
        out = []
        for line in result.stdout.strip().split("\n"):
            if not line.strip():
                continue
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 5:
                continue
            # Some fields may be "[N/A]" for cards that don't expose them
            def _f(s):
                try:
                    return float(s)
                except (ValueError, TypeError):
                    return None
            name = parts[0]
            util = _f(parts[1]) or 0.0
            mem_u = _f(parts[2]) or 0
            mem_t = _f(parts[3]) or 0
            temp = _f(parts[4])
            power = _f(parts[5]) if len(parts) > 5 else None
            power_limit = _f(parts[6]) if len(parts) > 6 else None
            out.append({
                "name": name,
                "util": util,
                "mem_used": mem_u * 1024 * 1024,
                "mem_total": mem_t * 1024 * 1024,
                "temp": temp,
                "power": power,
                "power_limit": power_limit,
            })
        self.gpus = out

    def _fetch_lhm_extras(self):
        """Pull Hot Spot and Memory Junction GPU temps from LHM if available.
        These aren't exposed by nvidia-smi/pynvml.

        Reads from the shared LHM poller — no HTTP call here. The poller
        runs on its own thread and refreshes the cached JSON every couple
        seconds. This call is essentially free.
        """
        from panels import lhm
        data = lhm.get_data()
        if data is None:
            return  # LHM not available yet, keep last known values

        # Walk the tree looking for GPU temp sensors
        extras = {}

        def walk(node, in_gpu_subtree=False):
            if not isinstance(node, dict):
                return
            node_text = node.get("Text", "")
            node_text_lower = node_text.lower()
            if any(t in node_text_lower for t in ("nvidia", "geforce", "radeon", "rtx", "gtx")):
                in_gpu_subtree = True

            if in_gpu_subtree and isinstance(node.get("Value"), str) and "°C" in node["Value"]:
                try:
                    num = float(node["Value"].replace("°C", "").strip())
                    leaf_lower = node_text.lower()
                    if "hot spot" in leaf_lower:
                        extras["hot_spot"] = num
                    elif "memory" in leaf_lower:
                        extras["memory"] = num
                    elif leaf_lower in ("gpu core", "core"):
                        extras["core"] = num
                except (ValueError, TypeError):
                    pass

            children = node.get("Children", [])
            if isinstance(children, list):
                for c in children:
                    walk(c, in_gpu_subtree)

        walk(data)
        if extras:
            self.lhm_temps = extras
        # If extras is empty, keep last known values

    # Color for any temperature reading — delegate to theme.
    # Kept as a method so existing call sites (self._temp_color(x)) work unchanged.
    def _temp_color(self, c):
        return heat_temp(c)

    def render(self, width=None):
        if width is None:
            width = 30

        text = Text()
        if self.backend is None:
            text.append("(no GPU detected)\n", style=SECONDARY)
            text.append("install nvidia drivers or\n", style=DIM)
            text.append("run on a system with\nan NVIDIA GPU", style=DIM)
            return text

        if not self.gpus:
            text.append("(GPU info unavailable)", style=SECONDARY)
            return text

        prefix_chars = 5  # "GPU  "
        suffix_chars = 5  # " 100%"
        bar_width = fit_bar_width(width, prefix_chars, suffix_chars,
                                   min_width=6, max_width=80)

        for i, g in enumerate(self.gpus):
            display_name = g["name"]
            # Smart truncation: when space is tight, strip the redundant
            # "NVIDIA GeForce" / "AMD Radeon" prefixes — most users know
            # what GPU vendor they have, and "RTX 4070 SUPER" is the part
            # that actually identifies the card.
            if len(display_name) > width:
                for prefix in ("NVIDIA GeForce ", "NVIDIA ", "AMD Radeon ", "AMD "):
                    if display_name.startswith(prefix):
                        display_name = display_name[len(prefix):]
                        break
            # If still too long, truncate with ellipsis
            if len(display_name) > width:
                display_name = display_name[:width - 1] + "…"
            text.append(f"{display_name}\n", style=BRIGHT)

            # GPU utilization bar — power draw shown INLINE matching VRAM row
            util = g["util"]
            fg = heatmap_color(util)
            bg = empty_color(util)

            # Build power suffix string the same way VRAM does its size string
            power = g.get("power")
            power_limit = g.get("power_limit")
            if power is not None and power_limit:
                power_str = f"  {power:.0f}W of {power_limit:.0f}W"
            elif power is not None:
                power_str = f"  {power:.0f}W"
            else:
                power_str = ""

            gpu_bar_width = max(6, bar_width - len(power_str))
            filled = int((util / 100) * gpu_bar_width)
            empty = gpu_bar_width - filled
            text.append("GPU  ", style=LABEL)
            text.append(FILLED * filled, style=fg)
            text.append(EMPTY * empty, style=bg)
            text.append(f" {int(round(util)):3d}%", style=fg)
            if power_str:
                text.append(power_str + "\n", style=MEDIUM)
            else:
                text.append("\n")

            # VRAM bar — size shown INLINE with the bar (not on a separate
            # line), so it's clearly tied to VRAM. Bar shortens to make room.
            mem_pct = (g["mem_used"] / g["mem_total"] * 100) if g["mem_total"] else 0
            mfg = heatmap_color(mem_pct)
            mbg = empty_color(mem_pct)

            # Build the size string first so we know how much space to reserve.
            # Format: "  2.0GB of 12.0GB" — leading 2-space gap, then sizes.
            size_str = f"  {fmt_bytes(g['mem_used'])} of {fmt_bytes(g['mem_total'])}"
            # VRAM bar gets less room so size fits on same line
            vram_bar_width = max(6, bar_width - len(size_str))
            mfilled = int((mem_pct / 100) * vram_bar_width)
            mempty = vram_bar_width - mfilled

            text.append("VRAM ", style=LABEL)
            text.append(FILLED * mfilled, style=mfg)
            text.append(EMPTY * mempty, style=mbg)
            text.append(f" {int(round(mem_pct)):3d}%", style=mfg)
            text.append(size_str + "\n", style=MEDIUM)

            # ── Temperature row — three readings inline ──
            # Prefer LHM-sourced detailed temps if available, fall back to driver temp.
            text.append("\n")

            # Pick what to show:
            # Three GPU temps:
            #   CORE     — average GPU die temperature (the "main" number)
            #   HOTSPOT  — worst single point on the die (always 10-20°C higher)
            #   VRAM     — memory junction temp (where VRAM connects to board)
            die_temp = self.lhm_temps.get("core") or g.get("temp")

            if die_temp is not None:
                die_color = self._temp_color(die_temp)
                text.append("CORE  ", style=f"bold {die_color}")
                text.append(f"{die_temp:.0f}°C", style=f"bold {die_color}")
            else:
                text.append("CORE  ", style=DIM)
                text.append("--", style=DIM)

            hot = self.lhm_temps.get("hot_spot")
            if hot is not None:
                hot_color = self._temp_color(hot)
                text.append("   ", style=DIM)
                text.append("HOTSPOT  ", style=f"bold {hot_color}")
                text.append(f"{hot:.0f}°C", style=f"bold {hot_color}")

            mem = self.lhm_temps.get("memory")
            if mem is not None:
                mem_color = self._temp_color(mem)
                text.append("   ", style=DIM)
                text.append("VRAM  ", style=f"bold {mem_color}")
                text.append(f"{mem:.0f}°C", style=f"bold {mem_color}")

            if i < len(self.gpus) - 1:
                text.append("\n\n")

        return text

    def csv_headers(self):
        return ["gpu_util", "gpu_mem_pct", "gpu_temp_die", "gpu_temp_hot",
                "gpu_temp_mem", "gpu_power_w"]

    def csv_columns(self):
        if self.gpus:
            g = self.gpus[0]
            mem_pct = (g["mem_used"] / g["mem_total"] * 100) if g["mem_total"] else 0
            die = self.lhm_temps.get("core") or g.get("temp") or 0
            hot = self.lhm_temps.get("hot_spot") or 0
            mem_t = self.lhm_temps.get("memory") or 0
            power = g.get("power") or 0
            return [g["util"], mem_pct, die, hot, mem_t, power]
        return [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]