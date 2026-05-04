"""Test 8: like test 7 but with REAL psutil calls in panel ticks.

Winston's actual panels call:
  - psutil.cpu_percent(percpu=True) at 4Hz
  - psutil.virtual_memory() at 2Hz
  - psutil.process_iter() at 1Hz  <- this one is the suspect

psutil.process_iter() can take 50-100ms on a busy system. That's a long
synchronous block on the UI thread, every second. If THIS drops
characters but test 7 didn't, psutil is the culprit.

Run:  python3 input_test_8.py
"""
import time
import csv
import os
import psutil
from textual.app import App, ComposeResult
from textual.widgets import Input, Static
from rich.text import Text
from textual.containers import Horizontal


class PsutilApp(App):
    CSS = """
    Screen { background: black; }
    .panel { height: 5; padding: 0 1; border: round green; width: 1fr; }
    #echo { height: 3; color: ansi_bright_green; padding: 1; }
    Input { border: round green; height: 3; margin: 0 1; }
    Horizontal { height: 5; }
    """
    BINDINGS = [("escape", "quit", "Quit")]

    def __init__(self):
        super().__init__()
        os.makedirs("logs/raw", exist_ok=True)
        self._csv_file = open("logs/raw/test8_writes.csv", "w", newline="")
        self._csv = csv.writer(self._csv_file)
        self._csv.writerow(["timestamp", "value"])

    def compose(self) -> ComposeResult:
        yield Static("Test 8: real psutil calls. Type, Esc to quit.")
        with Horizontal():
            yield Static("cpu", id="p1", classes="panel")
            yield Static("ram", id="p2", classes="panel")
            yield Static("processes", id="p3", classes="panel")
        yield Input(placeholder="type here...", id="user_input")
        yield Static("(echo)", id="echo")

    def on_mount(self):
        self.query_one("#user_input", Input).focus()
        # Prime psutil
        psutil.cpu_percent(percpu=True)
        for p in psutil.process_iter():
            try: p.cpu_percent()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        self.set_interval(0.25, self._tick_cpu)            # 4Hz cpu_percent
        self.set_interval(0.50, self._tick_ram)            # 2Hz virtual_memory
        self.set_interval(1.00, self._tick_processes)      # 1Hz process_iter (suspect!)
        self.set_interval(1.00, self._log_tick)

    def _tick_cpu(self):
        vals = psutil.cpu_percent(percpu=True)
        avg = sum(vals) / len(vals) if vals else 0
        t = Text()
        t.append("cpu\n", style="bold bright_green")
        t.append(f"{avg:.1f}%", style="green")
        self.query_one("#p1", Static).update(t)

    def _tick_ram(self):
        m = psutil.virtual_memory()
        t = Text()
        t.append("ram\n", style="bold bright_green")
        t.append(f"{m.percent:.1f}%", style="green")
        self.query_one("#p2", Static).update(t)

    def _tick_processes(self):
        # Real psutil.process_iter — same as Winston's ProcessesPanel
        procs = []
        for p in psutil.process_iter(['pid', 'name', 'cpu_percent', 'memory_info']):
            try:
                info = p.info
                cpu = info['cpu_percent'] or 0.0
                procs.append((cpu, info['name'] or '?'))
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        procs.sort(key=lambda p: -p[0])
        t = Text()
        t.append("processes\n", style="bold bright_green")
        if procs:
            top = procs[0]
            t.append(f"{top[1]}: {top[0]:.1f}%", style="green")
        self.query_one("#p3", Static).update(t)

    def _log_tick(self):
        self._csv.writerow([time.time(), 0])
        self._csv_file.flush()

    def on_input_changed(self, event: Input.Changed):
        if event.input.id == "user_input":
            self.query_one("#echo", Static).update(f"echo: {event.value}")

    def on_unmount(self):
        try: self._csv_file.close()
        except: pass


if __name__ == "__main__":
    PsutilApp().run()