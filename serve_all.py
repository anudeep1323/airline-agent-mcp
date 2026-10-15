"""
Run the API and the UI in ONE container (the cheapest hosting: one free service, one public URL).

  API: uvicorn on 127.0.0.1:8000, reachable only from inside the container
  UI:  streamlit on 0.0.0.0:$PORT (the host's public port; Render sets PORT, default 10000)

If either process dies, stop the other and exit non-zero, so the host restarts the whole container.
"""
import os
import signal
import subprocess
import sys
import time

import httpx

PORT = os.environ.get("PORT", "10000")

api = subprocess.Popen([sys.executable, "-m", "uvicorn", "airline_service:app", "--host", "127.0.0.1", "--port", "8000"])
for _ in range(60):                                     # wait until the API (and its MCP child) is ready
    try:
        if httpx.get("http://127.0.0.1:8000/ready", timeout=1).status_code == 200:
            break
    except httpx.HTTPError:
        pass
    if api.poll() is not None:
        sys.exit(f"API exited with code {api.returncode} during startup")
    time.sleep(1)
else:
    api.terminate()
    sys.exit("API not ready after 60 s")

ui = subprocess.Popen([sys.executable, "-m", "streamlit", "run", "ui.py", "--server.address", "0.0.0.0",
                       "--server.port", PORT, "--server.headless", "true"],
                      env={**os.environ, "AIRLINE_API": "http://127.0.0.1:8000"})
print(f"serving: UI on :{PORT}, API on 127.0.0.1:8000", flush=True)


def stop(*_):
    for p in (ui, api):
        p.terminate()


signal.signal(signal.SIGTERM, stop)                     # the host sends SIGTERM on redeploy/shutdown
while api.poll() is None and ui.poll() is None:
    time.sleep(1)
stop()
sys.exit(1 if (api.returncode or ui.returncode) else 0)
