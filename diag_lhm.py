"""
Diagnose LHM connectivity and module loading.

Run from project root:  python3 diag_lhm.py
"""
import sys
sys.stdout.write("=== LHM-only diagnostic ===\n\n")
sys.stdout.flush()

# Step 1: Can we even import the module?
sys.stdout.write("[1] Importing panels.lhm...\n")
sys.stdout.flush()
try:
    sys.path.insert(0, ".")
    from panels import lhm
    sys.stdout.write(f"  OK. Module path: {lhm.__file__}\n")
except Exception as e:
    sys.stdout.write(f"  FAILED: {type(e).__name__}: {e}\n")
    sys.exit(1)
sys.stdout.flush()

# Step 2: Check WSL detection
sys.stdout.write("\n[2] WSL detection...\n")
sys.stdout.flush()
sys.stdout.write(f"  _is_wsl(): {lhm._is_wsl()}\n")
sys.stdout.write(f"  _wsl_host_ip(): {lhm._wsl_host_ip()}\n")
sys.stdout.write(f"  _candidate_hosts(): {lhm._candidate_hosts()}\n")
sys.stdout.flush()

# Step 3: Direct fetch test (no thread, no caching)
sys.stdout.write("\n[3] Direct LHM fetch test...\n")
sys.stdout.flush()
import json, time, urllib.request, urllib.error

for host in lhm._candidate_hosts():
    url = f"http://{host}:8085/data.json"
    sys.stdout.write(f"  Trying {url}...\n")
    sys.stdout.flush()
    try:
        t0 = time.monotonic()
        with urllib.request.urlopen(url, timeout=2.0) as r:
            elapsed = (time.monotonic() - t0) * 1000
            data = json.loads(r.read().decode())
            sys.stdout.write(f"    SUCCESS in {elapsed:.0f}ms\n")
            sys.stdout.write(f"    Top-level keys: {list(data.keys())[:5]}\n")
            children = data.get("Children", [])
            sys.stdout.write(f"    Child count: {len(children)}\n")
            if children:
                first = children[0]
                if isinstance(first, dict):
                    sys.stdout.write(f"    First child Text: {first.get('Text', '?')!r}\n")
            break
    except urllib.error.URLError as e:
        elapsed = (time.monotonic() - t0) * 1000
        sys.stdout.write(f"    URL ERROR after {elapsed:.0f}ms: {e}\n")
    except OSError as e:
        elapsed = (time.monotonic() - t0) * 1000
        sys.stdout.write(f"    OS ERROR after {elapsed:.0f}ms: {e}\n")
    except Exception as e:
        sys.stdout.write(f"    EXCEPTION ({type(e).__name__}): {e}\n")
    sys.stdout.flush()

# Step 4: Test the lhm.get_data() flow
sys.stdout.write("\n[4] Testing lhm.get_data() (starts background thread)...\n")
sys.stdout.flush()
data = lhm.get_data()
sys.stdout.write(f"  First call: {'data dict' if data else 'None (expected on cold start)'}\n")
sys.stdout.flush()

sys.stdout.write("  Waiting 5 seconds for poller to fetch...\n")
sys.stdout.flush()
for i in range(5):
    time.sleep(1)
    data = lhm.get_data()
    fresh = lhm.is_fresh()
    sys.stdout.write(f"  After {i+1}s: data={'YES' if data else 'no'}  fresh={fresh}\n")
    sys.stdout.flush()
    if data:
        break

if data is None:
    sys.stdout.write("\n  Poller never got data. Check that LibreHardwareMonitor.exe\n")
    sys.stdout.write("  is running on Windows AND the firewall rule is in place.\n")
else:
    sys.stdout.write(f"\n  Got data. Tree has {len(data.get('Children', []))} top-level entries.\n")

# Step 5: Test that TempsPanel can use this data
sys.stdout.write("\n[5] Testing TempsPanel via the shared cache...\n")
sys.stdout.flush()
try:
    from panels.temps import TempsPanel
    p = TempsPanel(refresh_sec=1)
    p.update()
    sys.stdout.write(f"  TempsPanel.backend = {p.backend!r}\n")
    sys.stdout.write(f"  TempsPanel.help_message = {p.help_message!r}\n")
    sys.stdout.write(f"  TempsPanel.readings count = {len(p.readings)}\n")
    if p.readings:
        sys.stdout.write("  Sample readings:\n")
        for r in p.readings[:6]:
            sys.stdout.write(f"    {r}\n")
except Exception as e:
    sys.stdout.write(f"  FAILED: {type(e).__name__}: {e}\n")
    import traceback
    traceback.print_exc(file=sys.stdout)
sys.stdout.flush()

# Step 6: Test GPU panel's LHM enrichment
sys.stdout.write("\n[6] Testing GPU panel's _fetch_lhm_extras...\n")
sys.stdout.flush()
try:
    from panels.gpu import GpuPanel
    g = GpuPanel()
    g.update()
    sys.stdout.write(f"  GpuPanel.backend = {g.backend!r}\n")
    sys.stdout.write(f"  GpuPanel.lhm_temps = {g.lhm_temps!r}\n")
    sys.stdout.write(f"  Number of GPUs: {len(g.gpus)}\n")
    if g.gpus:
        sys.stdout.write(f"  First GPU keys: {list(g.gpus[0].keys())}\n")
        sys.stdout.write(f"  First GPU temp: {g.gpus[0].get('temp')}\n")
        sys.stdout.write(f"  First GPU power: {g.gpus[0].get('power')}\n")
except Exception as e:
    sys.stdout.write(f"  FAILED: {type(e).__name__}: {e}\n")
    import traceback
    traceback.print_exc(file=sys.stdout)
sys.stdout.flush()

sys.stdout.write("\n=== END ===\n")