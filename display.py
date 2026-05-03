"""WINSTON display — hand-rolled scrolling braille CPU graph, tight layout."""
import platform
import socket
from datetime import datetime

import psutil
from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.widgets import Static


# ──────────────── Custom chrome ────────────────
class StatusBar(Static):
    def on_mount(self):
        self.set_interval(1.0, self.refresh_status)
        self.refresh_status()

    def refresh_status(self):
        host = socket.gethostname()
        boot = datetime.fromtimestamp(psutil.boot_time())
        up = datetime.now() - boot
        days = up.days
        hours, rem = divmod(up.seconds, 3600)
        mins, _ = divmod(rem, 60)
        uptime = f"{days}d {hours:02d}h {mins:02d}m"
        now = datetime.now().strftime("%H:%M:%S")
        os_name = platform.system().upper()

        line = (
            f"[bold bright_green]◤ WINSTON[/bold bright_green] "
            f"[grey50]v0.5[/grey50]   "
            f"[grey50]HOST[/grey50] [bright_green]{host}[/bright_green]   "
            f"[grey50]OS[/grey50] [bright_green]{os_name}[/bright_green]   "
            f"[grey50]UP[/grey50] [bright_green]{uptime}[/bright_green]"
            f"{' ' * 4}"
            f"[grey50]TIME[/grey50] [bright_green]{now}[/bright_green]"
        )
        self.update(line)


class FooterBar(Static):
    def render(self):
        return (
            "[grey50] [/grey50][bold bright_green]Q[/bold bright_green]"
            "[grey50] quit  [/grey50]"
            "[bold bright_green]R[/bold bright_green]"
            "[grey50] reset history  [/grey50]"
            "[bold bright_green]G[/bold bright_green]"
            "[grey50] process map (soon)[/grey50]"
        )


class CommentaryPanel(Static):
    """Placeholder commentary. Will be wired to LLM next."""
    def on_mount(self):
        self.set_interval(3.0, self.cycle)
        self._tick = 0
        self.cycle()

    def cycle(self):
        self._tick += 1
        lines = [
            "[bold bright_green]>[/bold bright_green] [bright_green]system status[/bright_green] [grey50]::[/grey50] [bright_green]NOMINAL[/bright_green]",
            "[bold bright_green]>[/bold bright_green] [bright_green]analysis subsystem[/bright_green] [grey50]::[/grey50] [#1a8c1a]offline[/#1a8c1a] [grey50](pending llm setup)[/grey50]",
            "[bold bright_green]>[/bold bright_green] [bright_green]observation log[/bright_green] [grey50]::[/grey50] [bright_green]ACTIVE[/bright_green] [grey50]→ logs/raw/[/grey50]",
        ]
        self.update(lines[self._tick % len(lines)])


# ──────────────── Panel widgets ────────────────
class PanelWidget(Static):
    """Generic data-panel wrapper."""
    def __init__(self, panel, **kwargs):
        super().__init__(**kwargs)
        self.panel = panel

    def refresh_panel(self):
        avail = max(20, self.size.width - 4)
        try:
            self.update(self.panel.render(width=avail))
        except TypeError:
            self.update(self.panel.render())

        # If the panel exposes a dynamic title, sync it to the border.
        # Used by panels whose label varies with content (e.g. DISK vs DISKS,
        # or "PROCESSES (8)" with count). Most panels don't define this and
        # keep their static title set at compose time.
        title = getattr(self.panel, "title", None)
        if title is not None:
            new_border_title = f"─ {title} ─"
            if self.border_title != new_border_title:
                self.border_title = new_border_title


class CpuGraphWidget(Static):
    """CPU graph — hand-rolled scrolling braille graph.

    Why not plotext? Plotext recomputes layout every frame, which makes
    axis labels and gridlines visually jitter. This widget draws into
    fixed character positions: data scrolls left, axis stays put. Same
    technique htop/btop use.
    """
    def __init__(self, data_panel, **kwargs):
        super().__init__(**kwargs)
        self.data_panel = data_panel

    def refresh_panel(self):
        from rich.text import Text
        from panels.base import braille_graph, health_for

        text = Text()

        # Width available for the graph itself. Reserve 5 chars on the left
        # for the y-axis labels: "100% ".
        avail = max(40, self.size.width - 4)
        axis_w = 5
        graph_w = avail - axis_w   # in CHARACTERS — each braille char = 2 data points
        if graph_w < 10:
            graph_w = 10

        # Use the last (graph_w * 2) data points so each char column
        # represents two seconds of data (since each braille char is 2 wide).
        # That gives us a 60-wide graph showing 120s of history.
        # We pad with zeros on the left if we don't have enough data.
        WINDOW_PIXELS = graph_w * 2
        history = list(self.data_panel.history)[-WINDOW_PIXELS:]
        if len(history) < WINDOW_PIXELS:
            history = [0.0] * (WINDOW_PIXELS - len(history)) + history

        # Build a 4-row braille graph
        GRAPH_HEIGHT_ROWS = 5
        rows = braille_graph(history, width=graph_w, height=GRAPH_HEIGHT_ROWS, max_val=100)

        # Color: smooth heatmap from theme. Whole graph reflects current load.
        from theme import heat_pct, DIM, MEDIUM
        current = self.data_panel.last_value
        graph_color = heat_pct(current)

        # Header line — current/avg/peak readings
        avg = self.data_panel.average
        peak = self.data_panel.peak
        text.append(f"{current:5.1f}%", style=f"bold {graph_color}")
        text.append("  avg ", style=DIM)
        text.append(f"{avg:.1f}%", style=MEDIUM)
        text.append("  peak ", style=DIM)
        text.append(f"{peak:.1f}%", style=heat_pct(peak))
        text.append("\n")

        # Y-axis labels for each row, top to bottom.
        y_labels_5 = ["100% ", " 75% ", " 50% ", " 25% ", "  0% "]

        # Render each graph row with axis label on left
        for i, row in enumerate(rows):
            text.append(y_labels_5[i], style=DIM)
            text.append(row, style=graph_color)
            text.append("\n")

        # Bottom axis: time labels pinned to character positions.
        text.append("     ", style=DIM)
        text.append("─" * graph_w, style=DIM)
        text.append("\n")

        text.append("     ", style=DIM)
        # Build the label row character-by-character
        # We want labels at positions 0, graph_w//2, graph_w-1
        # Labels: "-120s"(5), "-60s"(4), "now"(3)
        label_row = [" "] * graph_w
        # "-120s" at position 0
        for i, c in enumerate("-120s"):
            if i < graph_w:
                label_row[i] = c
        # "-60s" at middle
        mid = graph_w // 2
        for i, c in enumerate("-60s"):
            pos = mid - 1 + i  # center the label slightly
            if 0 <= pos < graph_w:
                label_row[pos] = c
        # "now" right-aligned
        for i, c in enumerate("now"):
            pos = graph_w - 3 + i
            if 0 <= pos < graph_w:
                label_row[pos] = c
        text.append("".join(label_row), style=DIM)

        self.update(text)


# ──────────────── Main app ────────────────
class WinstonApp(App):
    CSS = """
    Screen {
        background: black;
    }

    StatusBar {
        height: 1;
        background: black;
        padding: 0 1;
    }

    FooterBar {
        height: 1;
        dock: bottom;
        background: black;
        padding: 0 1;
    }

    /* All panels share this — round borders, tight padding, no margin */
    .panel {
        border: round green;
        border-title-color: ansi_bright_green;
        border-title-style: bold;
        padding: 0 1;
    }

    /* Hero CPU graph — hand-rolled scrolling braille graph */
    #cpu_graph_panel {
        height: 11;
        border: round green;
        border-title-color: ansi_bright_green;
        border-title-style: bold;
        padding: 0 1;
    }

    /* Row 1: CORES (2fr) | MEMORY | SYSTEM */
    #row1 {
        height: 11;
    }
    #cores_panel {
        width: 2fr;
        border: round green;
        border-title-color: ansi_bright_green;
        border-title-style: bold;
        padding: 0 1;
    }
    #memory_panel {
        width: 1fr;
        border: round green;
        border-title-color: ansi_bright_green;
        border-title-style: bold;
        padding: 0 1;
    }
    #system_panel {
        width: 1fr;
        border: round green;
        border-title-color: ansi_bright_green;
        border-title-style: bold;
        padding: 0 1;
    }

    /* Row 2: DISKS (narrow) | TEMPS (wider) | GPU (wider) */
    #row2 {
        height: 10;
    }
    #disk_panel {
        width: 1fr;
        border: round green;
        border-title-color: ansi_bright_green;
        border-title-style: bold;
        padding: 0 1;
    }
    #temps_panel {
        width: 2fr;
        border: round green;
        border-title-color: ansi_bright_green;
        border-title-style: bold;
        padding: 0 1;
    }
    #gpu_panel {
        width: 2fr;
        border: round green;
        border-title-color: ansi_bright_green;
        border-title-style: bold;
        padding: 0 1;
    }

    /* Row 3: NETWORK | PROCESSES */
    #row3 {
        height: 12;
    }
    #network_panel {
        width: 1fr;
        border: round green;
        border-title-color: ansi_bright_green;
        border-title-style: bold;
        padding: 0 1;
    }
    #processes_panel {
        width: 2fr;
        border: round green;
        border-title-color: ansi_bright_green;
        border-title-style: bold;
        padding: 0 1;
    }

    /* Commentary — bigger, holds future LLM output */
    #commentary_panel {
        height: 6;
        border: round green;
        border-title-color: ansi_bright_green;
        border-title-style: bold;
        padding: 0 1;
    }
    """

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("r", "reset_history", "Reset"),
    ]

    def __init__(self, sections, logger, logger_hz=1.0):
        super().__init__()
        # sections is a list of (panel_instance, refresh_hz) tuples
        # We split into parallel lists for convenience
        self.sections = [s[0] for s in sections]
        self.section_rates = [s[1] for s in sections]
        self.logger = logger
        self.logger_hz = logger_hz

        (self.cpu_graph,
         self.cpu,
         self.ram,
         self.system,
         self.disk,
         self.temps,
         self.gpu,
         self.network,
         self.processes) = self.sections

        # Map each panel object → list of widgets that visualize it.
        # Built up in compose(); used by per-panel tick handlers.
        self._panel_widgets = {}

    def compose(self) -> ComposeResult:
        yield StatusBar(id="status_bar")

        cpu_g = CpuGraphWidget(self.cpu_graph, id="cpu_graph_panel")
        cpu_g.border_title = "─ CPU LOAD ─"
        self._panel_widgets.setdefault(id(self.cpu_graph), []).append(cpu_g)
        yield cpu_g

        with Horizontal(id="row1"):
            cores = PanelWidget(self.cpu, id="cores_panel")
            cores.border_title = "─ CORES ─"
            self._panel_widgets.setdefault(id(self.cpu), []).append(cores)
            yield cores

            mem = PanelWidget(self.ram, id="memory_panel")
            mem.border_title = "─ MEMORY ─"
            self._panel_widgets.setdefault(id(self.ram), []).append(mem)
            yield mem

            sysm = PanelWidget(self.system, id="system_panel")
            sysm.border_title = "─ SYSTEM ─"
            self._panel_widgets.setdefault(id(self.system), []).append(sysm)
            yield sysm

        with Horizontal(id="row2"):
            disk = PanelWidget(self.disk, id="disk_panel")
            disk.border_title = "─ DISKS ─"
            self._panel_widgets.setdefault(id(self.disk), []).append(disk)
            yield disk

            temps = PanelWidget(self.temps, id="temps_panel")
            temps.border_title = "─ TEMPS ─"
            self._panel_widgets.setdefault(id(self.temps), []).append(temps)
            yield temps

            gpu = PanelWidget(self.gpu, id="gpu_panel")
            gpu.border_title = "─ GPU ─"
            self._panel_widgets.setdefault(id(self.gpu), []).append(gpu)
            yield gpu

        with Horizontal(id="row3"):
            net = PanelWidget(self.network, id="network_panel")
            net.border_title = "─ NETWORK ─"
            self._panel_widgets.setdefault(id(self.network), []).append(net)
            yield net

            procs = PanelWidget(self.processes, id="processes_panel")
            procs.border_title = "─ PROCESSES ─"
            self._panel_widgets.setdefault(id(self.processes), []).append(procs)
            yield procs

        commentary = CommentaryPanel(id="commentary_panel")
        commentary.border_title = "─ COMMENTARY ─"
        yield commentary

        yield FooterBar(id="footer_bar")

    def on_mount(self) -> None:
        # Panels were already primed in run() before this app started.
        # Just do an immediate widget refresh so the prepopulated data shows.
        for panel, _hz in zip(self.sections, self.section_rates):
            for w in self._panel_widgets.get(id(panel), []):
                try:
                    w.refresh_panel()
                except Exception:
                    pass

        # Schedule each panel on its own interval
        for panel, hz in zip(self.sections, self.section_rates):
            interval = 1.0 / hz
            self.set_interval(interval, self._make_tick(panel))

        # Logger ticks at its own rate, regardless of panel rates
        self.set_interval(1.0 / self.logger_hz, self._log_tick)

    def _make_tick(self, panel):
        """Return a closure that updates a specific panel and refreshes its widgets."""
        def tick():
            try:
                panel.update()
            except Exception:
                # Don't let one panel's error kill the whole app
                return
            # Refresh associated widgets
            for w in self._panel_widgets.get(id(panel), []):
                try:
                    w.refresh_panel()
                except Exception:
                    pass
        return tick

    def _log_tick(self):
        """Logger tick — write a row at LOGGER_HZ."""
        try:
            self.logger.log(self.sections)
        except Exception:
            pass

    def action_reset_history(self) -> None:
        for s in self.sections:
            for attr in ("history", "rx_history", "tx_history"):
                if hasattr(s, attr):
                    h = getattr(s, attr)
                    if hasattr(h, "clear"):
                        h.clear()
            if hasattr(s, "histories"):
                for h in s.histories:
                    h.clear()

    def on_unmount(self) -> None:
        self.logger.close()


def run(sections, logger, logger_hz=1.0):
    """Prime panels with one update each (so the screen renders with data
    immediately) THEN start the Textual app. Slow panels like DiskPanel
    finish their initial scan during this priming phase, before the user
    sees the UI — eliminates the "panel is blank for 5 seconds" startup.
    """
    print("WINSTON :: priming sensors...", end=" ", flush=True)

    # Prime psutil
    psutil.cpu_percent(percpu=True)
    psutil.cpu_percent()
    for p in psutil.process_iter():
        try:
            p.cpu_percent()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    # Run each panel's first update so it has data before screen draws
    for panel, _hz in sections:
        try:
            panel.update()
        except Exception:
            pass

    print("ready.")

    app = WinstonApp(sections, logger, logger_hz=logger_hz)
    app.run()