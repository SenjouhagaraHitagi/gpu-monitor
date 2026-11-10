"""Entry point used by the packaged exe (and usable with plain Python).

Two modes:

* ``--picker ...``  run the native path picker (spawned by the dashboard when
  frozen, because the exe has to re-enter itself);
* anything else     start the local dashboard and open it in the browser. If
  the dashboard is already running, just open the browser tab.
"""
import os
import sys
import threading
import time
import urllib.request
import webbrowser

import app


def dashboard_url(port):
    return f"http://127.0.0.1:{port}/"


def is_running(url):
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url, timeout=1) as response:
            return response.status == 200
    except Exception:
        return False


def open_when_ready(url):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if is_running(url):
            webbrowser.open(url)
            return
        time.sleep(0.25)


def run_picker(argv):
    from picker import main as picker_main
    sys.argv = [argv[0]] + [item for item in argv[1:] if item != "--picker"]
    picker_main()


def read_port(argv):
    if "--port" in argv:
        try:
            return int(argv[argv.index("--port") + 1])
        except (IndexError, ValueError):
            pass
    return app.DEFAULT_PORT


def main():
    argv = sys.argv[:]
    if "--picker" in argv[1:]:
        run_picker(argv)
        return
    port = read_port(argv)
    url = dashboard_url(port)
    if is_running(url):
        webbrowser.open(url)
        return
    if os.environ.get("GPU_MONITOR_NO_BROWSER", "") in ("", "0"):
        threading.Thread(target=open_when_ready, args=(url,), daemon=True).start()
    sys.argv = [argv[0], "--port", str(port)]
    try:
        app.main()
    except OSError as exc:
        try:
            from tkinter import messagebox
            messagebox.showerror("GPU Monitor", f"无法启动本地服务：{exc}\n端口可能已被其他程序占用。")
        except Exception:
            pass


if __name__ == "__main__":
    main()
