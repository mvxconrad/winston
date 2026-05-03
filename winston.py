import psutil
from rich.live import Live
from rich.panel import Panel

with Live(refresh_per_second=1) as live:
    while True:
        cpu = psutil.cpu_percent(interval=1)
        mem = psutil.virtual_memory()
        text = f"CPU: {cpu}%    RAM: {mem.percent}%"
        live.update(Panel(text, title="WINSTON"))