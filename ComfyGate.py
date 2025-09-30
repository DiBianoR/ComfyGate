r"""
ComfyGate — an on‑demand launcher/reverse‑proxy for ComfyUI on Windows

What it does
============
• Listens on http://127.0.0.1:9000 (configurable) as a tiny gate in front of ComfyUI.
• If a request arrives and ComfyUI is NOT running, it starts ComfyUI and shows a waiting page.
• Once ComfyUI responds, it transparently reverse‑proxies all HTTP and WebSocket traffic to it.
• Tracks activity; if there are no requests or WebSocket traffic for INACTIVITY_TIMEOUT_COMFYUI 
  (default 1800s), it (optionally) backs up your ComfyUI user folder and then gracefully stops 
  ComfyUI.

How to use (quick)
==================
1) Edit the CONFIG section below to point to your ComfyUI and Python paths.
2) Install deps:  pip install aiohttp
3) Run once in a terminal to test:  python comfy_gate.py
4) Point Abyss reverse proxy to http://127.0.0.1:9000 (or whatever LISTEN_HOST/PORT you set).
5) (Optional but recommended) Install this script as a Windows service using NSSM (see chat steps).

Notes
=====
• ComfyUI saves UI settings automatically to its user folder (e.g., .../ComfyUI/user/default). This gate can
  make a timestamped backup before shutdown. Configure USER_DIR and BACKUP_USER_BEFORE_SHUTDOWN below.
• This proxy forwards WebSockets, which ComfyUI uses for progress updates. Abyss must be configured to reverse
  proxy WebSockets to this gate; the gate will in turn proxy to ComfyUI.
• For a truly graceful stop, the script tries to send a CTRL_BREAK to the ComfyUI process group; if that fails,
  it terminates the process.
"""

from aiohttp import web, ClientSession, ClientTimeout, WSMsgType
import asyncio
import contextlib
from datetime import datetime
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import threading
import time
from typing import Optional

# =============================
# CONFIG — EDIT THESE PATHS
# =============================
LISTEN_HOST = "127.0.0.1"         # Where this gate listens
LISTEN_PORT = 9000                 # Port for this gate (Abyss should proxy here)

COMFY_HOST = "127.0.0.1"          # Where ComfyUI will listen (keep localhost)
COMFY_PORT = 8188                  # ComfyUI's port

# Path to ComfyUI repo folder (containing main.py)
COMFY_DIR = r"E:\Servers\ComfyUI"         # ← change to your install path

# Python executable to run ComfyUI (portable builds: point to embedded python)
PYTHON_EXE = r"C:\Program Files\Python313\python.exe"  # ← change to your python

# [Alternatively] Conda executable to and env containing Python executable to run ComfyUI
CONDA_EXE = r"E:\ProgramData\miniforge3\Scripts\conda.exe"  # Path to conda.exe
ENV_NAME = "comfyui"  # Your env name

# Additional launch args for ComfyUI. Typical: ["--listen", COMFY_HOST, "--port", str(COMFY_PORT)]
COMFY_ARGS = ["--listen", "--enable-cors-header", "--highvram", "--input-directory", r"D:\Public\Images\AI_art\ComfyUI\input", "--temp-directory", r"D:\Public\Images\AI_art\ComfyUI\temp", "--output-directory", r"D:\Public\Images\AI_art\ComfyUI\output"]

# Where your ComfyUI user folder lives (for backup); adjust to your install type
# If unsure, try COMFY_DIR / "user". Desktop builds may use %APPDATA%/ComfyUI/user
USER_DIR = Path(COMFY_DIR) / "user"
BACKUP_USER_BEFORE_SHUTDOWN = True
BACKUP_ROOT = Path(COMFY_DIR) / "user_backups"  # backups will be USER_DIR copied under this root

VERBOSE = True

# Idle shutdown
INACTIVITY_TIMEOUT_WS = 60 * 60        # (seconds) when to turn off the websocket (restarts immediately if page open)
INACTIVITY_TIMEOUT_COMFYUI = 60 * 5   # (seconds) when to shutdown the backend server if no websockets
INACTIVITY_TIMEOUT_HARD = 60 * 720     # (seconds) when to hard shutdown the backend server even if there are open websockets
CHECK_PERIOD_SECS = 30                # How often to check for idle

# Internal globals
_comfy_proc: Optional[subprocess.Popen] = None
_starting_lock = asyncio.Lock()
_last_activity = time.monotonic()
_active_ws = 0

header_forwarding_policy = 'whitelist'  # 'blacklist' or 'whitelist'
WHITELISTED_HEADERS = {
    # headers we forward from Abyss/browser to ComfyUI; Host is overridden to upstream host
    "User-Agent",
    "Accept",
    "Accept-Encoding",
    "Accept-Language",
    "Cache-Control",
    "Content-Type",
    "Origin",
    "Referer",
    "Cookie",
    "X-Requested-With",
    "Sec-WebSocket-Key",
    "Sec-WebSocket-Version",
    "Sec-WebSocket-Extensions",
}
BLACKLISTED_HEADERS = {"Host", "Connection", "Upgrade", "Proxy-Authorization", "X-Forwarded-For"}


def activity_tick():
    global _last_activity
    _last_activity = time.monotonic()


def comfy_running() -> bool:
    return _comfy_proc is not None and _comfy_proc.poll() is None


async def comfy_ready(session: ClientSession) -> bool:
    try:
        async with session.get(f"http://{COMFY_HOST}:{COMFY_PORT}/", timeout=ClientTimeout(total=2)) as resp:
            return resp.status == 200
    except Exception:
        return False


async def start_comfy():
    global _comfy_proc
    if comfy_running():
        return

    # Ensure working directory
    cwd = str(Path(COMFY_DIR))
    main_py = str(Path(COMFY_DIR) / "main.py")

    creationflags = 0
    if os.name == "nt":
        # Create a new process group so we can send CTRL_BREAK later
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]

    if CONDA_EXE:
        cmd = [CONDA_EXE, 'run', '-n', ENV_NAME, 'python', main_py] + COMFY_ARGS
    elif PYTHON_EXE:
        cmd = [PYTHON_EXE, main_py] + COMFY_ARGS
    else:
        cmd = ["python", main_py] + COMFY_ARGS
    _comfy_proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
        creationflags=creationflags,
        text=True,
        bufsize=1,
    )
    print("[ComfyGate] ComfyUI started.")
    
    def monitor_proc():
        _comfy_proc.wait()  # Blocks until process exits
        if _comfy_proc.returncode in [0, -1073741510, 3221225786]:  # 0=normal, others=Ctrl+Break interrupt
            print("[ComfyGate] ComfyUI stopped normally")
        else:
            print(f"[ComfyGate] ComfyUI stopped unexpectedly with exit code {_comfy_proc.returncode}")
    threading.Thread(target=monitor_proc, daemon=True).start()


async def stop_comfy():
    global _comfy_proc
    if not comfy_running():
        return

    # Optional backup
    if BACKUP_USER_BEFORE_SHUTDOWN and USER_DIR.exists():
        try:
            BACKUP_ROOT.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d-%H%M%S")
            dst = BACKUP_ROOT / f"user-backup-{ts}"
            # copytree requires dst not exist
            shutil.copytree(USER_DIR, dst)
        except Exception as e:
            print(f"[ComfyGate] Backup failed: {e}")

    # Try graceful stop via CTRL_BREAK on Windows; fall back to terminate
    try:
        if os.name == "nt":
            os.kill(_comfy_proc.pid, signal.CTRL_BREAK_EVENT)  # type: ignore[attr-defined]
            await asyncio.sleep(3)
        # If still alive, terminate
        if _comfy_proc.poll() is None:
            _comfy_proc.terminate()
            try:
                _comfy_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                _comfy_proc.kill()
    finally:
        _comfy_proc = None
        print("[ComfyGate] ComfyUI stopped.")


async def idle_watchdog(app: web.Application):
    await app.startup()
    try:
        while True:
            await asyncio.sleep(CHECK_PERIOD_SECS)
            idle_for = time.monotonic() - _last_activity
            if idle_for >= INACTIVITY_TIMEOUT_COMFYUI and _active_ws == 0 and comfy_running():
                print(f"[ComfyGate] Idle for {int(idle_for)}s → stopping ComfyUI…")
                await stop_comfy()
            elif idle_for >= INACTIVITY_TIMEOUT_HARD and comfy_running():
                print(f"[ComfyGate] Idle for {int(idle_for)}s, [Warning]{int(_active_ws)} active websockets → stopping ComfyUI…")
                await stop_comfy()
    finally:
        await app.shutdown()


WAIT_PAGE = f"""
<!doctype html>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Starting ComfyUI…</title>
<style>
  body {{ font-family: system-ui, sans-serif; display:grid; place-items:center; height:100dvh; margin:0; }}
  .box {{ text-align:center; max-width: 42rem; padding: 2rem; }}
  .dots::after {{ content: '…'; animation: dots 1.5s steps(3, end) infinite; }}
  @keyframes dots {{ 0% {{ content:''; }} 33% {{ content:'.'; }} 66% {{ content:'..'; }} 100% {{ content:'...'; }} }}
</style>
<div class="box">
  <h1>Warming up ComfyUI<span class="dots"></span></h1>
  <p>This can take a moment the first time after a reboot. You’ll be redirected automatically when it’s ready.</p>
  <p><small>If this page doesn’t advance after a while, check the service logs.</small></p>
</div>
<script>
 async function poll() {{
   try {{
     const r = await fetch('/__comfygate/health', {{cache: 'no-store'}});
     if (r.ok) {{
       const j = await r.json();
       if (j.status === 'ready') {{
         window.location.reload();
         return;
       }}
     }}
   }} catch (e) {{}}
   setTimeout(poll, 1000);
 }}
 poll();
</script>
"""


async def handle_health(request: web.Request):
    activity_tick()
    async with ClientSession() as session:
        ready = await comfy_ready(session)
        return web.json_response({"status": "ready" if ready else "starting"})


async def ensure_comfy_started():
    async with _starting_lock:
        if comfy_running():
            return
        await start_comfy()


async def proxy_ws(request: web.Request):
    global _active_ws
    activity_tick()                         # global _last_activity: for all websockets, http, and handle_health
    local_last_activity = time.monotonic()  # for just this websocket
    timed_out = False
    
    # Ensure upstream exists (start if needed)
    await ensure_comfy_started()

    ws_server = web.WebSocketResponse()
    await ws_server.prepare(request)
    _active_ws += 1
    print(f"[ComfyGate] Opened new WebSocket")

    upstream_url = f"http://{COMFY_HOST}:{COMFY_PORT}{request.rel_url}"
    if header_forwarding_policy == 'whitelist':
        headers = {k: v for k, v in request.headers.items() if k in WHITELISTED_HEADERS}
    else:
        headers = {k: v for k, v in request.headers.items() if k not in BLACKLISTED_HEADERS}
    headers["Host"] = f"{COMFY_HOST}:{COMFY_PORT}"

    try:
        async with ClientSession() as session:
            async with session.ws_connect(upstream_url, headers=headers) as ws_client:
                async def ws_to_upstream():
                    nonlocal local_last_activity
                    async for msg in ws_server:
                        activity_tick()
                        local_last_activity = time.monotonic()
                        if msg.type == WSMsgType.TEXT:
                            await ws_client.send_str(msg.data)
                        elif msg.type == WSMsgType.BINARY:
                            await ws_client.send_bytes(msg.data)
                        elif msg.type == WSMsgType.PING:
                            await ws_client.ping()
                        elif msg.type == WSMsgType.PONG:
                            continue
                        elif msg.type == WSMsgType.CLOSE:
                            await ws_client.close()
                        elif msg.type == WSMsgType.ERROR:
                            print(f"[ComfyGate] client sent message type ERROR")
                            break
                        else:
                            print(f"[ComfyGate] client sent unhandled message type {msg.type}")

                async def upstream_to_ws():
                    nonlocal local_last_activity
                    async for msg in ws_client:
                        activity_tick()
                        local_last_activity = time.monotonic()
                        if msg.type == WSMsgType.TEXT:
                            await ws_server.send_str(msg.data)
                        elif msg.type == WSMsgType.BINARY:
                            await ws_server.send_bytes(msg.data)
                        elif msg.type == WSMsgType.PING:
                            await ws_server.ping()
                        elif msg.type == WSMsgType.PONG:
                            continue
                        elif msg.type == WSMsgType.CLOSE:
                            break
                        elif msg.type == WSMsgType.ERROR:
                            print(f"[ComfyGate] server returned message type ERROR")
                            break
                        else:
                            print(f"[ComfyGate] server returned unhandled message type {msg.type}")
                            
                async def ws_timeout_monitor():
                    nonlocal local_last_activity, timed_out
                    while True:
                        await asyncio.sleep(CHECK_PERIOD_SECS)
                        idle_for = time.monotonic() - local_last_activity
                        if idle_for >= INACTIVITY_TIMEOUT_WS:
                            print(f"[ComfyGate] Closing inactive WebSocket after {int(idle_for)}s")
                            timed_out = True
                            await ws_client.close()  # Close upstream first
                            await ws_server.close()  # Then client-side
                            break  # Exit monitor task

                await asyncio.gather(ws_to_upstream(), upstream_to_ws(), ws_timeout_monitor())
    finally:
        if not timed_out:
            print(f"[ComfyGate] WebSocket Closed")
        await ws_server.close()
        _active_ws -= 1
        return ws_server


async def proxy_http(request: web.Request):
    activity_tick()
    # Handle WebSocket upgrades specially
    if request.headers.get("Upgrade", "").lower() == "websocket":
        return await proxy_ws(request)

    # Ensure upstream exists (start if needed); if not ready yet, show waiting page
    async with ClientSession() as session:
        if not await comfy_ready(session):
            await ensure_comfy_started()
            return web.Response(text=WAIT_PAGE, content_type="text/html", headers={
                "Cache-Control": "no-store"
            })

    # Proxy HTTP request
    upstream_url = f"http://{COMFY_HOST}:{COMFY_PORT}{request.rel_url}"
    headers = {k: v for k, v in request.headers.items() if k in WHITELISTED_HEADERS}
    headers["Host"] = f"{COMFY_HOST}:{COMFY_PORT}"

    data = await request.read()
    timeout = ClientTimeout(total=None)
    async with ClientSession(timeout=timeout) as session:
        async with session.request(
            method=request.method,
            url=upstream_url,
            headers=headers,
            data=data if data else None,
            allow_redirects=False,
        ) as resp:
            # Stream response back
            raw = web.StreamResponse(status=resp.status, reason=resp.reason)
            for (k, v) in resp.headers.items():
                if k.lower() == "content-length":
                    # We'll set this automatically
                    continue
                raw.headers[k] = v
            await raw.prepare(request)
            async for chunk in resp.content.iter_chunked(64 * 1024):
                await raw.write(chunk)
            await raw.write_eof()
            return raw


def make_app() -> web.Application:
    app = web.Application()
    app.add_routes([
        web.get("/__comfygate/health", handle_health),
        web.get("/{tail:.*}", proxy_http),       # Handles both GET and HEAD
        web.post("/{tail:.*}", proxy_http),
        web.put("/{tail:.*}", proxy_http),
        web.patch("/{tail:.*}", proxy_http),
        web.delete("/{tail:.*}", proxy_http),
        web.options("/{tail:.*}", proxy_http),
    ])
    return app


async def main():
    app = make_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, LISTEN_HOST, LISTEN_PORT)
    print(f"[ComfyGate] Listening on http://{LISTEN_HOST}:{LISTEN_PORT}")
    asyncio.create_task(idle_watchdog(app))
    await site.start()

    # Keep running
    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass

r"""
todo:
- connection still produces harmless errors
- we want ComfyUI to reliably close if ComfyGate crashes or is forcibly stopped
- everything machine specific through ini
- refuse launch launch.bat if another instance is already running
"""
