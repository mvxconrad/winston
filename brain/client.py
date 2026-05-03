"""Ollama client. Talks to a locally-running (or Windows-host-running) Ollama
instance and returns generated text.

Two ways to use it:

  1. generate(prompt) — blocking call. Use for tests and one-off scripts.
  2. generate_async(prompt, on_done) — non-blocking. The callback receives
     the result text on a worker thread. Use this from the UI thread so
     rendering doesn't stall while the model thinks.

Configuration:
  Set OLLAMA_HOST env var if Ollama isn't on the auto-detected host.
  Otherwise we use the Windows host gateway (same as panels/lhm.py).

Notes:
  - keep_alive=-1 keeps the model resident in VRAM forever. We always pass
    this so calls stay fast — first call has a load cost, every subsequent
    call is sub-second.
  - timeout=60s — generation usually finishes in <5s but cold-load can take
    20s, plus a buffer for slow prompts.
  - We catch ALL exceptions and return None — UI code should handle that
    gracefully (show "thinking…" or last good message).
"""
import json
import os
import platform
import queue
import threading
import time
import urllib.error
import urllib.request


# Default model. Override per-call if needed.
DEFAULT_MODEL = "qwen2.5:7b-instruct"

# Ollama port — standard.
OLLAMA_PORT = 11434

# Cap how long any single generation can take. After this we give up.
HTTP_TIMEOUT_SEC = 60.0


def _is_wsl():
    if platform.system() != "Linux":
        return False
    try:
        with open("/proc/version") as f:
            return "microsoft" in f.read().lower()
    except OSError:
        return False


def _wsl_host_ip():
    """Find Windows host IP from /proc/net/route. None if not WSL."""
    if not _is_wsl():
        return None
    try:
        with open("/proc/net/route") as f:
            for line in f.readlines()[1:]:
                parts = line.split()
                if len(parts) >= 3 and parts[1] == "00000000":
                    gw_hex = parts[2]
                    octets = [int(gw_hex[i:i+2], 16) for i in (6, 4, 2, 0)]
                    return ".".join(str(o) for o in octets)
    except (OSError, ValueError, IndexError):
        pass
    return None


def _resolve_host():
    """Pick the Ollama host. Honors OLLAMA_HOST env override.
    On WSL, defaults to the Windows host gateway (same as LHM does).
    Elsewhere, defaults to localhost.
    """
    env = os.environ.get("OLLAMA_HOST")
    if env:
        # User-provided. Strip http:// and any :port — we add port ourselves.
        env = env.replace("http://", "").replace("https://", "")
        if ":" in env:
            env = env.split(":")[0]
        return env
    gw = _wsl_host_ip()
    if gw:
        return gw
    return "localhost"


_host = _resolve_host()
_url = f"http://{_host}:{OLLAMA_PORT}/api/generate"


# ──────────────── Sync API ────────────────
def generate(prompt, system=None, model=DEFAULT_MODEL, timeout=HTTP_TIMEOUT_SEC):
    """Synchronous LLM call. Returns the generated text, or None on error.

    Don't call from the UI thread — this BLOCKS for up to 60 seconds.
    Use generate_async() instead from any code path that needs to render.
    """
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "keep_alive": -1,  # never unload — keeps subsequent calls fast
    }
    if system:
        payload["system"] = system

    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        _url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
            return data.get("response", "").strip() or None
    except (urllib.error.URLError, OSError, json.JSONDecodeError, TimeoutError):
        return None
    except Exception:
        # Catch-all so a transient hiccup never crashes the UI.
        return None


# ──────────────── Streaming API ────────────────
def generate_stream(prompt, system=None, model=DEFAULT_MODEL, timeout=HTTP_TIMEOUT_SEC):
    """Yields text chunks as the model generates them. Generator function.

    Ollama returns newline-delimited JSON when stream=True. Each line is a
    JSON object with a `response` field containing the next chunk (usually
    a token or two). We yield each chunk as it arrives, so the caller can
    update a UI in real-time.

    Yields strings (chunks). Returns nothing — caller accumulates if needed.

    On error: yields nothing then returns. Caller should treat empty
    iteration as a failure case.
    """
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": True,
        "keep_alive": -1,
    }
    if system:
        payload["system"] = system

    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        _url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            # urlopen returns a file-like object. Each chunk arrives as a
            # newline-terminated JSON object.
            for line in r:
                if not line:
                    continue
                try:
                    obj = json.loads(line.decode("utf-8"))
                except json.JSONDecodeError:
                    continue
                chunk = obj.get("response", "")
                if chunk:
                    yield chunk
                if obj.get("done"):
                    return
    except (urllib.error.URLError, OSError, TimeoutError):
        return
    except Exception:
        return


# ──────────────── Async Streaming API ────────────────
def generate_stream_async(prompt, on_chunk, on_done=None, on_error=None,
                          system=None, model=DEFAULT_MODEL):
    """Stream tokens to a callback on a background worker thread.

    Callbacks (called from the WORKER thread, not the UI thread):
      on_chunk(text)   — called for each chunk as it arrives
      on_done(text)    — called once when the stream completes; text is the
                         full accumulated response. Optional.
      on_error()       — called if the stream failed before completion.
                         Optional.

    Note: callbacks fire on the worker thread. Textual's `call_from_thread`
    is the right way to marshal back to the UI thread inside the callback.
    """
    _ensure_worker()
    _job_queue.put(("stream", prompt, system, model,
                    on_chunk, on_done, on_error))


# ──────────────── Async API (background worker thread) ────────────────
# A single worker thread processes a queue of (prompt, callback) jobs. This
# is intentional: queueing means we never have multiple LLM calls in flight
# at once (would thrash the GPU), and a steady FIFO order matches user
# expectations ("the answer to my question, then the next periodic update").
_job_queue = queue.Queue()
_worker_thread = None
_worker_lock = threading.Lock()


def _worker_loop():
    while True:
        job = _job_queue.get()
        if job is None:  # shutdown signal
            return

        # Two job shapes:
        #   ("stream", prompt, system, model, on_chunk, on_done, on_error)
        #   (prompt, system, model, callback)  — non-streaming
        if isinstance(job, tuple) and job[0] == "stream":
            _, prompt, system, model, on_chunk, on_done, on_error = job
            chunks = []
            had_error = False
            try:
                for chunk in generate_stream(prompt, system=system, model=model):
                    chunks.append(chunk)
                    try:
                        on_chunk(chunk)
                    except Exception:
                        # Don't let UI bugs crash the worker
                        pass
            except Exception:
                had_error = True

            full = "".join(chunks).strip()
            if not full:
                had_error = True

            try:
                if had_error:
                    if on_error:
                        on_error()
                else:
                    if on_done:
                        on_done(full)
            except Exception:
                pass
        else:
            # Non-streaming job: (prompt, system, model, callback)
            prompt, system, model, callback = job
            try:
                result = generate(prompt, system=system, model=model)
            except Exception:
                result = None
            try:
                callback(result)
            except Exception:
                pass

        _job_queue.task_done()


def _ensure_worker():
    global _worker_thread
    with _worker_lock:
        if _worker_thread is not None and _worker_thread.is_alive():
            return
        _worker_thread = threading.Thread(
            target=_worker_loop,
            name="winston-llm-worker",
            daemon=True,
        )
        _worker_thread.start()


def generate_async(prompt, on_done, system=None, model=DEFAULT_MODEL):
    """Queue a generation job. on_done(text_or_None) is called when finished.

    Safe to call from the UI thread. Multiple calls are FIFO-queued — they
    don't run in parallel (LLM calls are GPU-bound; running two at once
    just slows both down).
    """
    _ensure_worker()
    _job_queue.put((prompt, system, model, on_done))


def queue_depth():
    """How many jobs are waiting? Useful for the UI to show 'busy' state."""
    return _job_queue.qsize()


def shutdown():
    """Stop the worker thread (clean exit)."""
    _job_queue.put(None)


# ──────────────── Diagnostic ────────────────
def status():
    """Return a dict describing current state. For diagnostics."""
    return {
        "host": _host,
        "url": _url,
        "model": DEFAULT_MODEL,
        "queue_depth": queue_depth(),
        "worker_alive": _worker_thread.is_alive() if _worker_thread else False,
    }


if __name__ == "__main__":
    # Smoke test when run directly: python -m brain.client
    import sys
    print(f"Ollama URL: {_url}")
    print(f"Model:      {DEFAULT_MODEL}")
    print()
    print("Sync test...")
    t0 = time.monotonic()
    result = generate("Reply with exactly one short sentence.")
    elapsed = time.monotonic() - t0
    if result:
        print(f"  OK in {elapsed:.2f}s: {result!r}")
    else:
        print(f"  FAILED after {elapsed:.2f}s")
        sys.exit(1)

    print()
    print("Async test...")
    done = threading.Event()
    received = [None]
    def on_done(text):
        received[0] = text
        done.set()

    t0 = time.monotonic()
    generate_async("Reply with one word.", on_done)
    if done.wait(timeout=30):
        elapsed = time.monotonic() - t0
        print(f"  OK in {elapsed:.2f}s: {received[0]!r}")
    else:
        print("  TIMED OUT")
        sys.exit(1)

    print()
    print("Streaming test (you should see tokens appear one at a time)...")
    done = threading.Event()

    def on_chunk(c):
        sys.stdout.write(c)
        sys.stdout.flush()

    def on_stream_done(_full):
        done.set()

    def on_stream_error():
        done.set()
        print("\n  ERROR")

    t0 = time.monotonic()
    sys.stdout.write("  > ")
    sys.stdout.flush()
    generate_stream_async(
        "Count from one to five, with each number on its own line.",
        on_chunk=on_chunk,
        on_done=on_stream_done,
        on_error=on_stream_error,
    )
    if done.wait(timeout=30):
        elapsed = time.monotonic() - t0
        print(f"\n  Stream finished in {elapsed:.2f}s")
    else:
        print("\n  TIMED OUT")
        sys.exit(1)

    shutdown()
    print()
    print("All good.")