import psutil
from rich.text import Text

from panels.base import health_for, fmt_bytes
from theme import LABEL, SECONDARY, BRIGHT, MEDIUM, DIM, heat_pct


class ProcessesPanel:
    def __init__(self, limit=8):
        self.limit = limit
        self.procs = []

    def update(self):
        procs = []
        for p in psutil.process_iter(['pid', 'name', 'cpu_percent', 'memory_info']):
            try:
                info = p.info
                cpu = info['cpu_percent'] or 0.0
                mem = info['memory_info'].rss if info['memory_info'] else 0
                procs.append((cpu, mem, info['name'] or '?', info['pid']))
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        procs.sort(key=lambda p: -p[0])
        self.procs = procs[:self.limit]

    def render(self, width=None):
        if width is None:
            width = 40

        text = Text()

        # Layout: PID(6) + spacer(2) + NAME + spacer(2) + CPU%(6) + spacer(2) + MEM(7)
        # Total non-name overhead: 6 + 2 + 2 + 6 + 2 + 7 = 25 chars
        # Wider names — at least 20 chars (covers most binaries cleanly)
        name_w = max(20, width - 25)

        text.append(f"{'PID':>6}  {'NAME':<{name_w}}  {'CPU%':>6}  {'MEM':>7}\n", style=SECONDARY)

        for cpu, mem, name, pid in self.procs:
            display_name = name if len(name) <= name_w else name[:name_w - 1] + "…"
            # CPU color from theme — same heatmap palette as everywhere else.
            # Idle (<1%) gets dim so the row recedes visually.
            if cpu < 1:
                cpu_style = DIM
                name_style = DIM
            else:
                cpu_color = heat_pct(cpu)
                cpu_style = f"bold {cpu_color}" if cpu > 50 else cpu_color
                name_style = BRIGHT if cpu > 50 else MEDIUM

            text.append(f"{pid:>6}  ", style=SECONDARY)
            text.append(f"{display_name:<{name_w}}  ", style=name_style)
            text.append(f"{cpu:5.1f}%", style=cpu_style)
            text.append(f"  {fmt_bytes(mem):>7}\n", style=SECONDARY)

        return text

    def csv_headers(self):
        return ["top_proc_name", "top_proc_cpu"]

    def csv_columns(self):
        if self.procs:
            return [self.procs[0][2], self.procs[0][0]]
        return ["", 0.0]