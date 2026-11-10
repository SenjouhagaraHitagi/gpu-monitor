import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

BASE = Path(__file__).resolve().parent
PORT = 8767
URL = f"http://127.0.0.1:{PORT}/"


def running():
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(URL, timeout=1) as response:
            body = response.read().decode("utf-8", errors="ignore")
            return response.status == 200 and "GPU Monitor" in body
    except Exception:
        return False


def main():
    if not running():
        log = (BASE / "launcher.log").open("a", encoding="utf-8")
        subprocess.Popen([sys.executable, str(BASE / "app.py"), "--port", str(PORT)], cwd=BASE,
                         stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        deadline = time.monotonic() + 12
        while not running() and time.monotonic() < deadline:
            time.sleep(0.25)
    webbrowser.open(URL)


if __name__ == "__main__":
    main()
