import psutil
import csv
from enum import Enum
from collections import deque
from datetime import datetime
from pathlib import Path
from rich.live import Live
from rich.panel import Panel
from rich.text import Text



class HealthLevel(Enum):
    OK = "green"
    WARNING = "yellow"
    CRITICAL = "red"

def health_for(percent):
    """Map a usage percentage to a health level"""
    if percent <50:
        return HealthLevel.OK
    elif percent < 80:
        return HealthLevel.WARNING
    else:
        return HealthLevel.CRITICAL
    

class CpuPanel:
    def __init__(self, label="CPU", history_size=60):
        self.label = label
        self.values = []
        self.histories = []
        self.history_size = history_size

    def update(self):
        self.values = psutil.cpu_percent(interval=1, percpu=True)
        
        # First time we find # of cores, to set up histories
        if not self.histories:
            self.histories = [deque(maxlen=self.history_size) for _ in self.values]

        # Append each core's current value to its own history
        for i, v in enumerate(self.values):
            self.histories[i].append(v)
    
    @property
    def average(self):
        """Aggregate CPU % - derived from per-core values."""
        return sum(self.values) / len(self.values) if self.values else 0.0
    
    @property
    def avg_history(self):
        """Average of all averages over time (rough running mean)"""
        if not self.histories or not self.histories[0]:
            return 0.0
        # average each timestep across cores, then average them
        per_step = [sum(step) / len(step) for step in zip(*self.histories)]
        return sum(per_step) / len(per_step)

    def render(self):
        text = Text()

        # Top line: aggregate
        avg = self.average
        running = self.avg_history
        health = health_for(avg)
        text.append(f"{self.label}: {avg:5.1f}%   (avg: {running:5.1f}%)\n", style=health.value)

        # Per-core breakdown
        for i, v in enumerate(self.values):
            core_health = health_for(v)
            line = f"   Core {i:2d}: {v:5.1f}%"
            text.append(line, style=core_health.value)
            if i < len(self.values) - 1:
                text.append("\n")

        return text
    
    # CSV helpers
    def csv_headers(self):
        headers = ["cpu_avg"]
        for i in range(len(self.values)):
            headers.append(f"core_{i}")
        return headers
    
    def csv_columns(self):
        cols = [self.average]
        cols.extend(self.values)
        return cols
    

class RamPanel: 
    def __init__(self, label="RAM", history_size=60):
        self.label = label
        self.value = 0.0
        self.history = deque(maxlen=history_size)

    def update(self):
        mem = psutil.virtual_memory()
        self.value = mem.percent
        self.history.append(self.value)

    def render(self):
        health = health_for(self.value)
        avg = sum(self.history) / len(self.history) if self.history else 0
        text = f"{self.label}: {self.value:5.1f}%   (avg: {avg:5.1f}%)"
        return Text(text, style=health.value)
    
    # CSV helpers
    def csv_headers(self):
        return ["ram_pct"]
    
    def csv_columns(self):
        return [self.value]
    
class Logger:
    def __init__(self, path="winston_log.csv"):
        self.path = Path(path)
        self._wrote_header = self.path.exists() # if file already exists, assume header is written
        self._file = open(self.path, "a", newline="")
        self._writer = csv.writer(self._file)

    def log(self, sections):
        # Build a row: timestamp, then values from each section
        row = [datetime.now().isoformat()]
        for section in sections:
            row.extend(section.csv_columns())

        if not self._wrote_header:
            header = ["timestamp"]
            for section in sections:
                header.extend(section.csv_headers())
            self._writer.writerow(header)
            self._wrote_header = True

        self._writer.writerow(row)
        self._file.flush() # make sure it hits disk

    def close(self):
        self._file.close()

# Helper to combine multiple Text objects with newlines in between
def render_all(sections):
    combined = Text()
    for i, section in enumerate(sections):
        if i > 0:
            combined.append("\n")
        combined.append(section.render())
    return combined


# Build section list
cpu = CpuPanel()
ram = RamPanel()
logger = Logger()
sections = [cpu, ram]


# Main loop
with Live(refresh_per_second=1) as live:
    try:
        while True:
            for section in sections:
                section.update()
            logger.log(sections)
            live.update(Panel(render_all(sections), title="WINSTON\n Well-trained Intuitive Neural System Translating Observed Numbers"))
    finally:
        logger.close()

