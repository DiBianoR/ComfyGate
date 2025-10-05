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
from urllib.parse import unquote
import yarl

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
PYTHON_EXE = r"E:\ProgramData\miniforge3\envs\comfyui\python.exe"  # ← change to your python

# Additional launch args for ComfyUI. Typical: ["--listen", COMFY_HOST, "--port", str(COMFY_PORT)]
COMFY_ARGS = ["--listen", "--port", str(COMFY_PORT), "--enable-cors-header", "--highvram", "--input-directory", r"D:\Public\Images\AI_art\ComfyUI\input", "--temp-directory", r"D:\Public\Images\AI_art\ComfyUI\temp", "--output-directory", r"D:\Public\Images\AI_art\ComfyUI\output"]

# Where your ComfyUI user folder lives (for backup); adjust to your install type
# If unsure, try COMFY_DIR / "user". Desktop builds may use %APPDATA%/ComfyUI/user
USER_DIR = Path(COMFY_DIR) / "user"
BACKUP_USER_BEFORE_SHUTDOWN = False
BACKUP_ROOT = Path(COMFY_DIR) / "user_backups"  # backups will be USER_DIR copied under this root
CONFLICTING_PROCESSES = [
        {"name": "Python", "path": PYTHON_EXE},  # Always contains this process of course
    ]
    
VERBOSE = True
WS_TIMEOUT = True  #  Web sockets can time out
ASYNCIO_TWEAKS = False  # [experimental] non-critical asyncio-related code (WindowsSelectorEventLoopPolicy)
FIX_URLS_IN_LOCATION_RESPONSE_HEADERS = False  # [experimental] Rewrite location-related headers to proxy URL
DECODE_PATHS = False  # [experimental] decode % encoded characters in paths(not helping)
USE_EXTERNAL_COMFYUI = False  # [experimental] rely on a manually launched instance of ComfyUI

# Idle shutdown
INACTIVITY_TIMEOUT_WS = 60 * 5        # (seconds) when to turn off the websocket (restarts immediately if page open)
INACTIVITY_TIMEOUT_COMFYUI = 60 * 2   # (seconds) when to shutdown the backend server if no websockets
INACTIVITY_TIMEOUT_HARD = 60 * 720     # (seconds) when to hard shutdown the backend server even if there are open websockets
CHECK_PERIOD_SECS = 30                # How often to check for idle

# Internal globals
_comfy_proc: Optional[subprocess.Popen] = None
_starting_lock = asyncio.Lock()
_last_activity = time.monotonic()
_active_ws = 0
_shutting_down = False

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
    "Host",  # Added to preserve/forward the original Host header (e.g., from Abyss) to ComfyUI
    "X-Forwarded-Proto",  # Added to forward the X-Forwarded-Proto header (e.g., 'https' from Abyss) to ComfyUI
}
BLACKLISTED_HEADERS = {"Host", "Connection", "Upgrade", "Proxy-Authorization", "X-Forwarded-For"}


def activity_tick():
    global _last_activity
    _last_activity = time.monotonic()


def comfy_running() -> bool:  # process exists and has not ended
    if USE_EXTERNAL_COMFYUI:
        return True
    return _comfy_proc is not None and _comfy_proc.poll() is None

def is_blocked() -> bool:
    """
    Check if a blocking program (e.g., another VRAM-using app) is running.
    Here, we use nvidia-smi to check for any compute processes on the GPU.
    Adjust this function if you have a different way to detect the specific program.
    """

    try:
        # First check: blacklisted processes
        for proc in CONFLICTING_PROCESSES:
            print(f"checking {proc['name']}: {proc['path']}")
            ps_command = f"(Get-Process -Name {proc['name']} -ErrorAction SilentlyContinue).Path"
            output = subprocess.check_output(
                ["powershell.exe", "-Command", ps_command],
                text=True,
                stderr=subprocess.DEVNULL
            ).strip()
            if proc['path'] in output.splitlines():
                if VERBOSE:
                    print(f"[ComfyGate] Blocked by existing {proc['name']}: {proc['path']}")
                return True  # Blocked if any such processes are running (since checked when !comfy_running())

        # Second check: GPU memory usage
        query_gpu_memory_used = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
            text=True,
            stderr=subprocess.DEVNULL
        ).strip()
        memory_used_mb = int(query_gpu_memory_used)  # in MiB
        print(f"[ComfyGate] Checking GPU memory used: {memory_used_mb} MiB")
        if memory_used_mb > 5000:  # Threshold; adjust if needed
            return True
            
    except (subprocess.CalledProcessError, FileNotFoundError, ValueError):
        # If commands fail, assume not blocked
        print("[ComfyGate] Failed to query system resources; assuming not blocked")
        return False

    print("[ComfyGate] no blocking processes found")
    return False  # if anything was blocking it would have returned True above
    
    
async def comfy_ready(session: ClientSession) -> bool:
    try:  # try to get a response from ComfyUI
        async with session.get(f"http://{COMFY_HOST}:{COMFY_PORT}/", timeout=ClientTimeout(total=2)) as resp:
            return resp.status == 200
    except Exception:
        return False


async def start_comfy():
    global _comfy_proc
    if comfy_running() or _shutting_down:  # process exists and has not ended
        return

    # Ensure working directory
    cwd = str(Path(COMFY_DIR))
    
    if ASYNCIO_TWEAKS:
        policy_line = "import asyncio, runpy, sys, os; asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy()); "
    else:
        policy_line = "import asyncio, runpy, sys, os; "
    
    bootstrap = (
        policy_line +
        f"path={repr(str(Path(COMFY_DIR) / 'main.py'))}; "
        "sys.path.insert(0, os.path.dirname(path)); "
        "runpy.run_path(path, run_name='__main__')"
    )

    creationflags = 0
    if os.name == "nt":
        # Create a new process group so we can send CTRL_BREAK later
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]

    if PYTHON_EXE:
        cmd = [PYTHON_EXE, '-c', bootstrap] + COMFY_ARGS
    else:
        cmd = ["python", '-c', bootstrap] + COMFY_ARGS
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
        proc = _comfy_proc  # capture to avoid races if global is cleared
        if proc is None:
            return
        proc.wait()
        if proc.returncode in [0, -1073741510, 3221225786]:
            print("[ComfyGate] ComfyUI stopped normally")
        else:
            print(f"[ComfyGate] ComfyUI stopped unexpectedly with exit code {proc.returncode}")
    threading.Thread(target=monitor_proc, daemon=True).start()


async def stop_comfy():
    global _comfy_proc
    if not comfy_running() or USE_EXTERNAL_COMFYUI:  # process doesn't exist, has ended, or is external
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

    # Try graceful stop via SIGINT
    try:
        os.kill(_comfy_proc.pid, signal.SIGINT)
        await asyncio.sleep(10)
    except Exception as e:  # Catch any signal send failures (e.g., permission, invalid signal)
        print(f"[ComfyGate] WARNING: Graceful signal(SIGINT) failed to shutdown ComfyUI: {e}")

    # If still alive, Ctrl-Break
    if _comfy_proc.poll() is None:
        try:
            print("[ComfyGate] WARNING: Graceful shutdown timed out, attempting Ctrl-Break")
            os.kill(_comfy_proc.pid, signal.CTRL_BREAK_EVENT)
            await asyncio.sleep(10)
        except Exception as e:  # Catch any signal send failures (e.g., permission, invalid signal)
            print(f"[ComfyGate] WARNING: Ctrl-Break failed to shutdown ComfyUI: {e}")

    # If still alive, terminate / kill
    if _comfy_proc.poll() is None:
        print("[ComfyGate] WARNING: Ctrl-Break timed out, attempting terminate")
        _comfy_proc.terminate()
        try:
            _comfy_proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            print("[ComfyGate] WARNING: terminate failed to shutdown ComfyUI")
            _comfy_proc.kill()
            try:
                _comfy_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                print("[ComfyGate] ERROR: kill failed to shutdown ComfyUI")
    # Always clear out at the end
    _comfy_proc = None


async def idle_watchdog():
    while True:
        await asyncio.sleep(CHECK_PERIOD_SECS)
        idle_for = time.monotonic() - _last_activity
        if idle_for >= INACTIVITY_TIMEOUT_COMFYUI and _active_ws == 0 and comfy_running():
            print(f"[ComfyGate] Idle for {int(idle_for)}s → stopping ComfyUI…")
            await stop_comfy()
        elif idle_for >= INACTIVITY_TIMEOUT_HARD and comfy_running():
            print(f"[ComfyGate] Idle for {int(idle_for)}s, [Warning]{int(_active_ws)} active websockets → stopping ComfyUI…")
            await stop_comfy()

STATUS_MESSAGES = {
    "starting": {
        "title": "Warming up ComfyUI",
        "message": "This can take a moment the first time after a reboot. You’ll be redirected automatically when it’s ready.",
        "note": "If this page doesn’t advance after a while, check the service logs.",
        "interval": 3000,
    },
    "stopped": {
        "title": "ComfyUI is stopped",
        "message": "The server is currently stopped due to inactivity. It will start automatically on refresh or new request.",
        "note": "If unexpected, check the service logs.",
        "interval": 3000,
    },
    "blocked": {
        "title": "ComfyUI is blocked",
        "message": "Another program is using resources (e.g., GPU). Please close any other AI applications or ComfyUI instances.",
        "note": "If this persists, check the service logs or task manager.",
        "interval": 30000,
    },
    "shutting_down": {
        "title": "Shutting down ComfyUI",
        "message": "The server is shutting down due to inactivity or a stop signal. You’ll be redirected when ready again.",
        "note": "If unexpected, check the service logs.",
        "interval": 3000,
    },
    "error": {
        "title": "Error connecting to gate",
        "message": "Cannot reach the proxy server. It may be down, restarting, or there is a network issue.",
        "note": "Check the service status and logs.",
        "interval": 10000,
    },
}


def get_status_page(status: str) -> str:
    if status not in STATUS_MESSAGES:
        status = "error"  # Fallback
    msg = STATUS_MESSAGES[status]
    return f"""
<!doctype html>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>ComfyUI Status</title>
<style>
  body {{ font-family: system-ui, sans-serif; display:grid; place-items:center; height:100dvh; margin:0; }}
  .box {{ text-align:center; max-width: 42rem; padding: 2rem; }}
  .dots::after {{ content: '[..........]'; animation: dots 50s steps(10, end); white-space: pre; font-family: monospace; }}
  @keyframes dots {{
    0% {{ content: '[          ]'; }}
    10% {{ content: '[.         ]'; }}
    20% {{ content: '[..        ]'; }}
    30% {{ content: '[...       ]'; }}
    40% {{ content: '[....      ]'; }}
    50% {{ content: '[.....     ]'; }}
    60% {{ content: '[......    ]'; }}
    70% {{ content: '[.......   ]'; }}
    80% {{ content: '[........  ]'; }}
    90% {{ content: '[......... ]'; }}
    100% {{ content: '[..........]'; }}
  }}
</style>
<div class="box">
  <h1 id="title">{msg['title']}<br><span class="dots"></span></h1>
  <p id="message">{msg['message']}</p>
  <p id="note"><small>{msg['note']}</small></p>
</div>
<script>
const STATUS_MESSAGES = {{
  "starting": {{
    "title": "Warming up ComfyUI",
    "message": "This can take a moment the first time after a reboot. You’ll be redirected automatically when it’s ready.",
    "note": "If this page doesn’t advance after a while, check the service logs.",
    "interval": 3000,
  }},
  "stopped": {{
    "title": "ComfyUI is stopped",
    "message": "The server is currently stopped due to inactivity. It will start automatically on refresh or new request.",
    "note": "If unexpected, check the service logs.",
    "interval": 3000,
  }},
  "blocked": {{
    "title": "ComfyUI is blocked",
    "message": "Another program is using resources (e.g., GPU). Please close any other AI applications or ComfyUI instances.",
    "note": "If this persists, check the service logs or task manager.",
    "interval": 30000,
  }},
  "shutting_down": {{
    "title": "Shutting down ComfyUI",
    "message": "The server is shutting down due to inactivity or a stop signal. You’ll be redirected when ready again.",
    "note": "If unexpected, check the service logs.",
    "interval": 3000,
  }},
  "error": {{
    "title": "Error connecting to gate",
    "message": "Cannot reach the proxy server. It may be down, restarting, or there is a network issue.",
    "note": "Check the service status and logs.",
    "interval": 10000,
  }},
}};

function update_content(status) {{
  if (!STATUS_MESSAGES.hasOwnProperty(status)) return;
  const msg = STATUS_MESSAGES[status];
  document.getElementById('title').innerHTML = `${{msg.title}}<br><span class="dots"></span>`;
  document.getElementById('message').textContent = msg.message;
  document.getElementById('note').innerHTML = msg.note ? `<small>${{msg.note}}</small>` : '';
}}

async function poll() {{
  try {{
    const r = await fetch('/__comfygate/health', {{cache: 'no-store'}});
    if (r.ok) {{
      const j = await r.json();
      update_content(j.status);
      if (j.status === 'ready') {{
        window.location.reload();
        return;
      }} else {{
        setTimeout(poll, STATUS_MESSAGES[j.status].interval || 3000);
      }}
    }} else {{
      update_content('error');
      setTimeout(poll, STATUS_MESSAGES['error'].interval);
    }}
  }} catch (e) {{
    update_content('error');
    setTimeout(poll, STATUS_MESSAGES['error'].interval);
  }}
}}
setTimeout(poll, {msg['interval']});
</script>
"""


async def handle_health(request: web.Request):
    async with ClientSession() as session:

        if comfy_running() and await comfy_ready(session):  # try to get a response from ComfyUI
            status = "ready"
        elif comfy_running():  # process exists and has not ended
            status = "starting"
        elif _shutting_down:
            status = "shutting_down"
        elif is_blocked():
            status = "blocked"
        else:
            status = "stopped"
        if VERBOSE:
            print(f"ComfyUI status: {status}")
        return web.json_response({"status": status})


async def ensure_comfy_started():
    async with _starting_lock:
        if comfy_running():  # process exists and has not ended
            return
        await start_comfy()


async def proxy_ws(request: web.Request):
    global _active_ws
    activity_tick()                         # global _last_activity: for all websockets & http
    local_last_activity = time.monotonic()  # for just this websocket
    timed_out = False
    
    # Ensure upstream exists (start if needed)
    #if not comfy_running() and not is_blocked():
    #    await ensure_comfy_started()  # not sure wether we want ws to be able to boot comfy

    ws_server = web.WebSocketResponse(compress=0)
    await ws_server.prepare(request)
    _active_ws += 1
    print(f"[ComfyGate] Opened new WebSocket")

    if DECODE_PATHS and request.method == 'GET' and str(request.rel_url).startswith('/api/userdata'):
        decoded_path = unquote(request.rel_url.path)
        upstream_url = f"http://{COMFY_HOST}:{COMFY_PORT}{decoded_path}"
        if request.rel_url.query_string:
            upstream_url += '?' + request.rel_url.query_string
        if VERBOSE:
            print(f"[ComfyGate](ws) Original upstream: http://{COMFY_HOST}:{COMFY_PORT}{request.rel_url}")
            print(f"[ComfyGate](ws) Decoded upstream: {upstream_url}")
    else:
        upstream_url = f"http://{COMFY_HOST}:{COMFY_PORT}{request.rel_url}"
    
    # Minimal, safe headers; let aiohttp generate Sec-WebSocket-* itself.
    headers = {}
    for k in ("User-Agent", "Origin", "Cookie"):
        v = request.headers.get(k)
        if v:
            headers[k] = v
    sp = request.headers.get("Sec-WebSocket-Protocol")
    if sp:
        headers["Sec-WebSocket-Protocol"] = sp
    headers["Host"] = f"{COMFY_HOST}:{COMFY_PORT}"

    try:
        async with ClientSession() as session:
            async with session.ws_connect(upstream_url, headers=headers, compress=0, autoping=True, autoclose=True, timeout=10) as ws_client:
                async def ws_to_upstream():
                    nonlocal local_last_activity
                    async for msg in ws_server:
                        activity_tick()
                        local_last_activity = time.monotonic()
                        if msg.type == WSMsgType.TEXT:
                            await ws_client.send_str(msg.data)
                        elif msg.type == WSMsgType.BINARY:
                            await ws_client.send_bytes(msg.data)
                        elif msg.type in (WSMsgType.PING, WSMsgType.PONG):
                            continue  # autoping handles it
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
                        elif msg.type in (WSMsgType.PING, WSMsgType.PONG):
                            continue  # autoping handles it
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
                            with contextlib.suppress(Exception):  # Close upstream first
                                await ws_client.close(code=1000, message=b"idle timeout")
                                await ws_client.wait_closed()
                            await asyncio.sleep(0.2)
                            with contextlib.suppress(Exception):  # Then client-side
                                await ws_server.close(code=1000, message=b"idle timeout")
                            break  # Exit monitor task

                try:
                    if WS_TIMEOUT:
                        await asyncio.gather(ws_to_upstream(), upstream_to_ws(), ws_timeout_monitor())
                    else:
                        await asyncio.gather(ws_to_upstream(), upstream_to_ws())
                except (ConnectionResetError, asyncio.CancelledError):
                    pass
    finally:
        if not timed_out:
            print(f"[ComfyGate] WebSocket Closed")
        with contextlib.suppress(Exception):
            await ws_server.close()
        _active_ws -= 1
        return ws_server


async def proxy_http(request: web.Request):
    activity_tick()
    # Handle WebSocket upgrades specially
    if request.headers.get("Upgrade", "").lower() == "websocket":
        return await proxy_ws(request)

    async with ClientSession() as session:

        if comfy_running() and await comfy_ready(session):  # try to get a response from ComfyUI
            status = "ready"
            pass  # Proxy normally if our instance is running
        elif comfy_running():  # process exists and has not ended
            status = "starting"
            # Our instance is running but not yet ready → wait page
            return web.Response(
                text=get_status_page("starting"),
                content_type="text/html",
                headers={"Cache-Control": "no-store", "Connection": "close"},
            )
        elif _shutting_down:
            status = "shutting_down"
            return web.Response(
                text=get_status_page("shutting_down"),
                content_type="text/html",
                headers={"Cache-Control": "no-store", "Connection": "close"},
            )
        elif is_blocked():
            status = "blocked"  #  Blocked by other program (e.g., GPU in use) → refusal page
            return web.Response(
                text=get_status_page("blocked"),
                content_type="text/html",
                headers={"Cache-Control": "no-store", "Connection": "close"},
            )
        else:
            status = "stopped"
            # Not blocked → start ComfyUI and show wait page
            await ensure_comfy_started()
            return web.Response(
                text=get_status_page("starting"),
                content_type="text/html",
                headers={"Cache-Control": "no-store", "Connection": "close"},
            )
        if VERBOSE:
            print(f"ComfyUI status: {status}")

    # If we reach here, it's ready and our instance → proxy the request
    if DECODE_PATHS and request.method == 'GET' and str(request.rel_url).startswith('/api/userdata'):
        decoded_path = unquote(request.rel_url.path)
        upstream_url = f"http://{COMFY_HOST}:{COMFY_PORT}{decoded_path}"
        if request.rel_url.query_string:
            upstream_url += '?' + request.rel_url.query_string
        if VERBOSE:
            print(f"[ComfyGate] Original upstream: http://{COMFY_HOST}:{COMFY_PORT}{request.rel_url}")
            print(f"[ComfyGate] Decoded upstream: {upstream_url}")
    else:
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
            if FIX_URLS_IN_LOCATION_RESPONSE_HEADERS:
                # Rewrite location-related headers to proxy URL
                proxy_base = f"{request.scheme}://{request.host}"
                comfy_base = f"http://{COMFY_HOST}:{COMFY_PORT}"
                for header in ['Location', 'Content-Location', 'URI']:
                    if header in resp.headers:
                        url = resp.headers[header]
                        if url.startswith(comfy_base):
                            url = url.replace(comfy_base, proxy_base, 1)
                        resp.headers[header] = url  # Update before forwarding
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
    global _shutting_down
    app = make_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, LISTEN_HOST, LISTEN_PORT)
    print(f"[ComfyGate] Listening on http://{LISTEN_HOST}:{LISTEN_PORT}")
    asyncio.create_task(idle_watchdog())
    await site.start()

    # Keep running until interrupted
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        _shutting_down = True
        print("[ComfyGate] Shutting down...")
        await app.shutdown()  # Runs on_shutdown handlers
        await stop_comfy()
        await runner.cleanup()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass

r"""
todo:
- give error screen if another instance of ComfyUI or any VRAM using app is already running
- can't read/write properly from /api/userdata, path not being translated? check ComfyUI source
- everything machine specific through ini
- we want ComfyUI to reliably close if ComfyGate crashes
- connection still produces harmless errors
"""