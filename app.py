import ipaddress
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parent
# A PyInstaller build keeps read-only resources (web/) in a temporary bundle
# directory while settings.json has to live next to the exe.
FROZEN = bool(getattr(sys, "frozen", False))
RESOURCE_ROOT = Path(getattr(sys, "_MEIPASS", ROOT))
DATA_ROOT = Path(sys.executable).resolve().parent if FROZEN else ROOT
SETTINGS_PATH = DATA_ROOT / "settings.json"
WEB_ROOT = RESOURCE_ROOT / "web"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8767

GPU_QUERY = (
    "nvidia-smi --query-gpu=index,uuid,name,utilization.gpu,memory.used,memory.total,"
    "temperature.gpu,power.draw,power.limit --format=csv,noheader,nounits"
)
PROC_QUERY = "nvidia-smi --query-compute-apps=gpu_uuid,pid,process_name,used_memory --format=csv,noheader,nounits"

CACHE = {}
CACHE_LOCK = threading.Lock()
CLIENT_LAST_SEEN = time.monotonic()
CLIENT_LOCK = threading.Lock()
CLOSE_REQUESTED_AT = None
SETTINGS_CACHE = {"mtime": None, "data": None}
SETTINGS_LOCK = threading.Lock()


def touch_client():
    global CLIENT_LAST_SEEN, CLOSE_REQUESTED_AT
    with CLIENT_LOCK:
        CLIENT_LAST_SEEN = time.monotonic()
        CLOSE_REQUESTED_AT = None


def request_page_close():
    global CLOSE_REQUESTED_AT
    with CLIENT_LOCK:
        CLOSE_REQUESTED_AT = time.monotonic()


def is_internal_host(hostname):
    """决定服务器归入“内网”还是“外网”。

    优先使用 settings.json 里的 internal_prefix（按 HostName 前缀匹配）；
    留空时按私有 IP 自动判断：10/172.16-31/192.168/127 等算内网，其余算外网。
    主机名（非 IP）在前缀为空时按外网处理。
    """
    prefix = load_settings().get("internal_prefix", "").strip()
    if prefix:
        return hostname.startswith(prefix)
    try:
        return ipaddress.ip_address(hostname).is_private
    except ValueError:
        return False


def load_ssh_servers(mode="internal"):
    config_path = ssh_config_path()
    if not config_path.exists():
        return []
    try:
        lines = config_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return []
    blocks, current = [], None
    for raw in lines + ["Host __END__"]:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition(" ")
        if not value:
            key, _, value = line.partition("\t")
        key, value = key.lower(), value.strip()
        if key == "host":
            if current:
                blocks.append(current)
            current = {"aliases": value.split()}
        elif current is not None and key in {"hostname", "user", "port", "identityfile"}:
            current[key] = value
    defaults = blocks[0] if blocks and blocks[0].get("aliases") == ["*"] else {}
    result, seen = [], set()
    for block in blocks:
        aliases = block.get("aliases", [])
        if not aliases or any("*" in alias or "?" in alias or alias.startswith("!") for alias in aliases):
            continue
        hostname = block.get("hostname") or defaults.get("hostname")
        if not hostname:
            continue
        hostname = hostname.replace("%h", aliases[0])
        internal = is_internal_host(hostname)
        if (mode == "internal") != internal:
            continue
        alias = aliases[0]
        user = block.get("user") or defaults.get("user", "")
        port = int(block.get("port") or defaults.get("port") or 22)
        key = (alias, hostname, user, port)
        if key in seen:
            continue
        seen.add(key)
        identity = block.get("identityfile") or defaults.get("identityfile", "")
        result.append({"id": f"ssh-{len(result)+1}", "name": alias, "host": hostname,
                       "ssh_target": alias, "user": user, "port": port,
                       "identity_file": expand_path(identity)})
    return result


EDITOR_NAMES = {
    "cursor": ("cursor", "cursor.cmd", "Cursor.exe"),
    "vscode": ("code", "code.cmd", "Code.exe"),
}

# Standard Windows install locations, relative to the roots derived from environment
# variables. No absolute drive letter is assumed, so the app works on other machines.
EDITOR_LAYOUTS = {
    "cursor": (
        Path("Cursor") / "Cursor.exe",
        Path("Cursor") / "bin" / "cursor.cmd",
        Path("Programs") / "Cursor" / "Cursor.exe",
        Path("Programs") / "Cursor" / "bin" / "cursor.cmd",
    ),
    "vscode": (
        Path("Microsoft VS Code") / "Code.exe",
        Path("Microsoft VS Code") / "bin" / "code.cmd",
        Path("Programs") / "Microsoft VS Code" / "Code.exe",
        Path("Programs") / "Microsoft VS Code" / "bin" / "code.cmd",
    ),
}

INSTALL_ROOT_VARS = ("LOCALAPPDATA", "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432")

EDITOR_CACHE = {}
EDITOR_CACHE_LOCK = threading.Lock()
EDITOR_CACHE_POSITIVE_TTL = 600
EDITOR_CACHE_NEGATIVE_TTL = 60
PICK_TIMEOUT = 300
PICK_LOCK = threading.Lock()


def default_settings():
    return {"ssh_config": "", "cursor": "", "vscode": "", "internal_prefix": ""}


def load_settings():
    try:
        mtime = SETTINGS_PATH.stat().st_mtime_ns
    except OSError:
        return default_settings()
    with SETTINGS_LOCK:
        if SETTINGS_CACHE["mtime"] == mtime and SETTINGS_CACHE["data"] is not None:
            return dict(SETTINGS_CACHE["data"])
        try:
            raw = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        data = {key: str(raw.get(key) or "").strip() for key in default_settings()}
        SETTINGS_CACHE["mtime"] = mtime
        SETTINGS_CACHE["data"] = data
        return dict(data)


def save_settings_data(data):
    cleaned = {key: str(data.get(key) or "").strip() for key in default_settings()}
    SETTINGS_PATH.write_text(json.dumps(cleaned, ensure_ascii=False, indent=2), encoding="utf-8")
    with SETTINGS_LOCK:
        SETTINGS_CACHE["mtime"] = None
        SETTINGS_CACHE["data"] = None
    with EDITOR_CACHE_LOCK:
        EDITOR_CACHE.clear()


def expand_path(value):
    text = str(value or "").strip()
    if not text:
        return ""
    return os.path.expandvars(os.path.expanduser(text))


def ssh_config_path():
    configured = load_settings().get("ssh_config", "")
    if configured:
        candidate = Path(expand_path(configured))
        if candidate.is_dir():
            candidate = candidate / "config"
        return candidate
    return ssh_directory() / "config"


def install_roots():
    roots, seen = [], set()
    for name in INSTALL_ROOT_VARS:
        value = os.environ.get(name)
        if not value:
            continue
        path = Path(value)
        key = str(path).lower()
        if key in seen or not path.is_dir():
            continue
        seen.add(key)
        roots.append(path)
    return roots


def resolve_editor_target(editor, value):
    """Resolve a configured path (executable, install directory, or its parent)."""
    text = expand_path(value)
    if not text:
        return ""
    target = Path(text)
    if target.is_file():
        return str(target)
    if not target.is_dir():
        return ""
    candidates = [target / name for name in EDITOR_NAMES[editor]]
    candidates.extend(target / rel for rel in EDITOR_LAYOUTS[editor])
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    # One shallow level down covers layouts such as D:\software\cursor\_\Cursor.exe
    try:
        children = [child for child in target.iterdir() if child.is_dir()]
    except OSError:
        children = []
    for child in children[:50]:
        for name in EDITOR_NAMES[editor]:
            candidate = child / name
            if candidate.is_file():
                return str(candidate)
    return ""


def find_editor_auto(editor):
    """Search PATH and standard per-user / per-machine install folders."""
    for name in EDITOR_NAMES[editor]:
        found = shutil.which(name)
        if found and Path(found).is_file():
            return str(Path(found))
    for root in install_roots():
        for rel in EDITOR_LAYOUTS[editor]:
            candidate = root / rel
            if candidate.is_file():
                return str(candidate)
    return ""


def find_editor(editor, refresh=False):
    if editor not in EDITOR_NAMES:
        return None
    now = time.monotonic()
    with EDITOR_CACHE_LOCK:
        cached = EDITOR_CACHE.get(editor)
    if cached and not refresh:
        ttl = EDITOR_CACHE_POSITIVE_TTL if cached["path"] else EDITOR_CACHE_NEGATIVE_TTL
        if now - cached["checked"] < ttl and (not cached["path"] or Path(cached["path"]).exists()):
            return cached["path"]
    configured = load_settings().get(editor, "")
    path = resolve_editor_target(editor, configured) if configured else ""
    if not path:
        path = find_editor_auto(editor)
    path = path or None
    with EDITOR_CACHE_LOCK:
        EDITOR_CACHE[editor] = {"path": path, "checked": now}
    return path


def settings_payload():
    settings = load_settings()
    ssh_path = ssh_config_path()
    cursor = find_editor("cursor")
    vscode = find_editor("vscode")
    return {
        "settings": settings,
        "effective": {"ssh_config": str(ssh_path), "cursor": cursor or "", "vscode": vscode or ""},
        "detected": {
            "ssh_config": str(ssh_directory() / "config"),
            "cursor": find_editor_auto("cursor"),
            "vscode": find_editor_auto("vscode"),
        },
        "ssh_config_exists": ssh_path.is_file(),
    }


def run_path_picker(target, mode):
    """Open the native Windows picker in a short-lived helper process."""
    if target == "ssh_config":
        current = ssh_config_path()
        initial = current.parent if current.parent.is_dir() else ssh_directory()
        kind, title = "ssh_config", "选择 SSH config 文件"
    elif target in EDITOR_NAMES:
        current = find_editor(target)
        initial = Path(current).parent if current else Path(os.environ.get("USERPROFILE", "."))
        kind = "editor"
        title = "选择 Cursor 可执行文件" if target == "cursor" else "选择 VS Code 可执行文件"
    else:
        return {"ok": False, "error": "不支持的配置项"}
    if FROZEN:
        command = [sys.executable, "--picker", "--mode", mode, "--kind", kind,
                   "--title", title, "--initial", str(initial)]
    else:
        command = [sys.executable, str(ROOT / "picker.py"), "--mode", mode, "--kind", kind,
                   "--title", title, "--initial", str(initial)]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=PICK_TIMEOUT,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "选择窗口等待超时，请重试。"}
    except OSError as exc:
        return {"ok": False, "error": f"无法打开系统选择窗口：{exc}"}
    if completed.returncode != 0:
        detail = (completed.stderr or "").strip().splitlines()
        return {"ok": False, "error": f"系统选择窗口启动失败：{detail[-1] if detail else '未知错误'}"}
    lines = (completed.stdout or "").strip().splitlines()
    try:
        payload = json.loads(lines[-1]) if lines else {}
    except json.JSONDecodeError:
        return {"ok": False, "error": "系统选择窗口未返回有效路径。"}
    path = str(payload.get("path") or "")
    if not path:
        return {"ok": False, "cancelled": True}
    return {"ok": True, "path": path}


def configured_hosts():
    return {value for mode in ("internal", "external") for server in load_ssh_servers(mode)
            for value in (server.get("host"), server.get("ssh_target")) if value}


def ssh_directory():
    return Path(os.environ.get("USERPROFILE") or os.environ.get("HOME") or "~").expanduser() / ".ssh"


def discover_ssh_hosts():
    """Find concrete hosts already known to the local OpenSSH client.

    This intentionally avoids probing an entire subnet. It is fast, works without
    admin rights, and only returns hosts the user has configured or contacted.
    """
    config_path = ssh_config_path()
    ssh_dir = config_path.parent
    found = {}
    current = {}

    def commit():
        if not current:
            return
        aliases = current.get("aliases", [])
        host = current.get("hostname") or (aliases[0] if aliases else "")
        if not host or any(alias in ("*", "?", "!*") for alias in aliases):
            return
        item = {
            "id": f"discovered-{len(found) + 1}",
            "name": aliases[0] if aliases else host,
            "host": host,
            "user": current.get("user", ""),
            "port": int(current.get("port") or 22),
            "identity_file": current.get("identityfile", ""),
            "source": "SSH config",
        }
        found[f"{item['user']}@{host}:{item['port']}"] = item

    if config_path.exists():
        try:
            for raw in config_path.read_text(encoding="utf-8", errors="ignore").splitlines():
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                key, _, value = line.partition(" ")
                if not value:
                    key, _, value = line.partition("\t")
                key, value = key.lower(), value.strip()
                if key == "host":
                    commit()
                    current = {"aliases": value.split()}
                elif key in {"hostname", "user", "port", "identityfile"} and current:
                    current[key] = expand_path(value) if key == "identityfile" else value
            commit()
        except OSError:
            pass

    known_hosts_path = ssh_dir / "known_hosts"
    if known_hosts_path.exists():
        try:
            for raw in known_hosts_path.read_text(encoding="utf-8", errors="ignore").splitlines():
                if not raw or raw.startswith("#"):
                    continue
                host_field = raw.split(" ", 1)[0]
                for host_entry in host_field.split(","):
                    if host_entry.startswith("|") or host_entry.startswith("!"):
                        continue
                    port = 22
                    host = host_entry
                    if host.startswith("[") and "]" in host:
                        host, _, port_text = host[1:].partition("]:")
                        try:
                            port = int(port_text or 22)
                        except ValueError:
                            port = 22
                    if host and host not in {"localhost", "127.0.0.1", "::1"}:
                        key = f"@{host}:{port}"
                        found.setdefault(key, {"id": f"discovered-{len(found) + 1}", "name": host, "host": host, "user": "", "port": port, "identity_file": "", "source": "known_hosts"})
        except OSError:
            pass
    return list(found.values())


def split_csv_line(line):
    # nvidia-smi emits CSV with optional spaces after commas.
    return [item.strip() for item in line.split(",")]


STREAM_INTERVAL = 15
try:
    STREAM_INTERVAL = max(5, int(os.environ.get("GPU_MONITOR_INTERVAL") or 15))
except (TypeError, ValueError):
    STREAM_INTERVAL = 15
STREAM_STALL_TIMEOUT = max(120, STREAM_INTERVAL * 3 + 60)
STREAM_RECONNECT_DELAY = 5
WORKERS = {}
WORKERS_LOCK = threading.Lock()
ACTIVE_MODE = {"value": None}


def ssh_command(server, remote):
    host = server.get("host", "")
    user = server.get("user", "")
    port = str(server.get("port", "22"))
    identity_file = server.get("identity_file", "")
    target_host = server.get("ssh_target") or host
    target = f"{user}@{target_host}" if user else target_host
    command = ["ssh", "-T", "-o", "ClearAllForwardings=yes", "-o", "BatchMode=yes",
               "-o", "ConnectTimeout=8", "-o", "ServerAliveInterval=15",
               "-o", "ServerAliveCountMax=3", "-p", port]
    if identity_file:
        command.extend(["-i", identity_file])
    command.extend([target, remote])
    return command


def stream_script(interval):
    """Remote loop that pushes a full snapshot every interval seconds."""
    return (
        "while :; do "
        "echo __GPU_BEGIN__; "
        f"{GPU_QUERY}; "
        "echo __GPU_END__; "
        "echo __PROC_BEGIN__; "
        f"{PROC_QUERY}; "
        "echo __PROC_END__; "
        "ps -ww -eo user:128=,pid=,comm=,etime=; "
        "echo __SNAP_END__; "
        f"sleep {interval}; "
        "done"
    )


def parse_snapshot(gpu_text, proc_text, ps_text):
    gpus = []
    for line in gpu_text.splitlines():
        if not line.strip():
            continue
        fields = split_csv_line(line)
        if len(fields) < 9:
            continue
        try:
            gpu = {
                "index": int(fields[0]),
                "uuid": fields[1],
                "name": fields[2],
                "utilization": float(fields[3]),
                "memory_used": float(fields[4]),
                "memory_total": float(fields[5]),
                "temperature": float(fields[6]),
                "power_draw": float(fields[7]) if fields[7] not in ("N/A", "[N/A]") else None,
                "power_limit": float(fields[8]) if fields[8] not in ("N/A", "[N/A]") else None,
            }
        except ValueError:
            continue
        gpu["memory_percent"] = round(gpu["memory_used"] / gpu["memory_total"] * 100, 1) if gpu["memory_total"] else 0
        gpu["processes"] = []
        gpus.append(gpu)
    users = {}
    for line in ps_text.splitlines():
        fields = line.split(None, 3)
        if len(fields) >= 2:
            users[fields[1]] = {"user": fields[0], "runtime": fields[3] if len(fields) >= 4 else "—"}
    processes = []
    for line in proc_text.splitlines():
        fields = split_csv_line(line)
        if len(fields) >= 4 and fields[0] != "No running processes found":
            owner = users.get(fields[1], {"user": "未知用户", "runtime": "—"})
            item = {"gpu_uuid": fields[0], "pid": fields[1], "name": fields[2],
                    "memory": fields[3], "user": owner["user"], "runtime": owner["runtime"]}
            processes.append(item)
            for gpu in gpus:
                if gpu.get("uuid") == item["gpu_uuid"]:
                    gpu["processes"].append(item)
    return {"status": "ok", "gpus": gpus, "processes": processes}


class ServerWorker:
    """One long-lived SSH stream per server, with automatic reconnect.

    The remote side pushes a full snapshot every STREAM_INTERVAL seconds, so a
    slow SSH session setup (NFS home, PAM, ...) is paid once instead of on
    every refresh. Query rate on the server stays the same as before.
    """

    def __init__(self, key, server):
        self.key = key
        self.server = server
        self.stop_event = threading.Event()
        self.process = None
        self.last_activity = time.monotonic()
        self.snapshots = 0
        self.thread = threading.Thread(target=self.run, name=f"stream-{key[1]}", daemon=True)

    def start(self):
        self.thread.start()

    def alive(self):
        return self.thread.is_alive()

    def stop(self):
        self.stop_event.set()
        process = self.process
        if process is not None and process.poll() is None:
            try:
                process.kill()
            except OSError:
                pass
        if self.thread.is_alive() and self.thread is not threading.current_thread():
            self.thread.join(timeout=5)

    def run(self):
        failures = 0
        while not self.stop_event.is_set():
            before = self.snapshots
            try:
                self.session()
            except (OSError, ValueError) as exc:
                self.store_error(f"SSH 采集异常：{exc}")
            if self.stop_event.is_set():
                break
            if self.snapshots == before:
                failures += 1
                delay = min(30, STREAM_RECONNECT_DELAY * (2 ** (failures - 1)))
            else:
                failures = 0
                delay = STREAM_RECONNECT_DELAY
            self.stop_event.wait(delay)

    def session(self):
        if not self.server.get("host"):
            self.store_error("未配置主机地址")
            self.stop_event.wait(30)
            return
        process = subprocess.Popen(ssh_command(self.server, stream_script(STREAM_INTERVAL)),
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   stdin=subprocess.DEVNULL, text=True, encoding="utf-8",
                                   errors="replace", bufsize=1,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.process = process
        self.last_activity = time.monotonic()
        stderr_tail = deque(maxlen=20)
        threading.Thread(target=drain_stream, args=(process.stderr, stderr_tail), daemon=True).start()
        threading.Thread(target=self.watch, args=(process,), daemon=True).start()

        gpu_lines, proc_lines, ps_lines = [], [], []
        section = None
        snapshot_started = time.monotonic()
        try:
            for raw in process.stdout:
                self.last_activity = time.monotonic()
                line = raw.rstrip("\r\n")
                if line == "__GPU_BEGIN__":
                    gpu_lines, proc_lines, ps_lines = [], [], []
                    section = "gpu"
                    snapshot_started = time.monotonic()
                elif line == "__GPU_END__":
                    section = None
                elif line == "__PROC_BEGIN__":
                    section = "proc"
                elif line == "__PROC_END__":
                    section = None
                elif line == "__SNAP_END__":
                    self.publish(gpu_lines, proc_lines, ps_lines, snapshot_started)
                    section = None
                elif section == "gpu":
                    gpu_lines.append(line)
                elif section == "proc":
                    proc_lines.append(line)
                else:
                    ps_lines.append(line)
        finally:
            if process.poll() is None:
                try:
                    process.kill()
                except OSError:
                    pass
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            self.process = None
        if self.stop_event.is_set():
            return
        detail = [line for line in stderr_tail if line.strip()]
        self.store_error(detail[-1][:300] if detail else "SSH 连接已断开，正在重连")

    def watch(self, process):
        while not self.stop_event.is_set() and process.poll() is None:
            if time.monotonic() - self.last_activity > STREAM_STALL_TIMEOUT:
                try:
                    process.kill()
                except OSError:
                    pass
                return
            self.stop_event.wait(5)

    def publish(self, gpu_lines, proc_lines, ps_lines, started):
        result = parse_snapshot("\n".join(gpu_lines), "\n".join(proc_lines), "\n".join(ps_lines))
        result["elapsed_ms"] = round((time.monotonic() - started) * 1000)
        result["last_attempt"] = int(time.time())
        if result.get("status") == "ok":
            result["last_success"] = int(time.time())
        self.snapshots += 1
        with CACHE_LOCK:
            CACHE[self.key] = {"result": result, "refreshing": False, "started": time.monotonic()}

    def store_error(self, message):
        with CACHE_LOCK:
            previous = CACHE.get(self.key, {}).get("result")
            if previous and previous.get("status") == "ok":
                result = dict(previous, stale=True, error=message)
            else:
                result = {"status": "error", "error": message, "gpus": [], "processes": [], "elapsed_ms": 0}
            result["last_attempt"] = int(time.time())
            CACHE[self.key] = {"result": result, "refreshing": False, "started": time.monotonic()}


def drain_stream(stream, sink):
    try:
        for line in stream:
            if line.strip():
                sink.append(line.strip())
    except (OSError, ValueError):
        pass


def ensure_workers(mode, servers):
    with WORKERS_LOCK:
        if ACTIVE_MODE["value"] != mode:
            for key in [key for key in WORKERS if key[0] != mode]:
                WORKERS.pop(key).stop()
            ACTIVE_MODE["value"] = mode
        wanted = {(mode, server.get("id") or server.get("host")): server for server in servers}
        for key, server in wanted.items():
            worker = WORKERS.get(key)
            if worker is None or not worker.alive() or worker.server != server:
                if worker is not None:
                    worker.stop()
                worker = ServerWorker(key, server)
                WORKERS[key] = worker
                worker.start()
        for key in [key for key in WORKERS if key[0] == mode and key not in wanted]:
            WORKERS.pop(key).stop()


def stop_all_workers():
    with WORKERS_LOCK:
        workers = list(WORKERS.values())
        WORKERS.clear()
        ACTIVE_MODE["value"] = None
    for worker in workers:
        worker.stop()


def collect_all(force=False, mode="internal"):
    servers = load_ssh_servers(mode)
    ensure_workers(mode, servers)
    output = []
    for server in servers:
        key = (mode, server.get("id") or server.get("host"))
        with CACHE_LOCK:
            entry = CACHE.get(key) or {}
            result = entry.get("result") or {"status": "loading", "gpus": [], "elapsed_ms": 0}
            refreshing = entry.get("refreshing", False) or result.get("status") == "loading"
        output.append(dict(server, result=result, refreshing=refreshing))
    return output


class Handler(BaseHTTPRequestHandler):
    def json_response(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        touch_client()
        if path == "/api/heartbeat":
            self.json_response({"ok": True})
            return
        if path == "/api/editors":
            cursor = find_editor("cursor")
            vscode = find_editor("vscode")
            self.json_response({"cursor": bool(cursor), "vscode": bool(vscode),
                                "cursor_path": cursor or "", "vscode_path": vscode or ""})
            return
        if path == "/api/settings":
            self.json_response(settings_payload())
            return
        if path == "/api/servers":
            query = urlparse(self.path).query
            mode = "external" if "mode=external" in query else "internal"
            self.json_response({"servers": collect_all("refresh=1" in query, mode), "timestamp": int(time.time())})
            return
        if path == "/api/status":
            query = urlparse(self.path).query
            mode = "external" if "mode=external" in query else "internal"
            items = []
            for item in collect_all("refresh=1" in query, mode):
                result = item.get("result", {})
                flat = dict(result)
                flat.update({"name": item.get("name") or item.get("host"), "host": item.get("host"),
                             "ssh_target": item.get("ssh_target"),
                             "refreshing": item.get("refreshing", False),
                             "latency_ms": result.get("elapsed_ms", 0),
                             "last_success": result.get("last_success")})
                flat.setdefault("gpus", [])
                flat.setdefault("processes", [])
                items.append(flat)
            self.json_response({"servers": items, "timestamp": int(time.time())})
            return
        if path == "/api/discover":
            self.json_response({"servers": discover_ssh_hosts()})
            return
        if path == "/" or path == "/index.html":
            path = "/index.html"
        file_path = (WEB_ROOT / path.lstrip("/")).resolve()
        if WEB_ROOT not in file_path.parents or not file_path.exists() or not file_path.is_file():
            self.send_error(404)
            return
        content_type = "text/html; charset=utf-8" if file_path.suffix == ".html" else "text/css; charset=utf-8" if file_path.suffix == ".css" else "application/javascript; charset=utf-8"
        body = file_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        touch_client()
        if urlparse(self.path).path == "/api/heartbeat":
            self.json_response({"ok": True})
            return
        if urlparse(self.path).path == "/api/page-close":
            request_page_close()
            self.json_response({"ok": True})
            return
        if urlparse(self.path).path == "/api/open-editor":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                data = json.loads(self.rfile.read(length).decode("utf-8"))
                editor, host = data.get("editor"), data.get("host")
                if editor not in {"cursor", "vscode"} or host not in configured_hosts():
                    raise ValueError("无效的编辑器或服务器")
                executable = find_editor(editor)
                if not executable:
                    raise FileNotFoundError("本机未找到对应编辑器")
                subprocess.Popen([executable, "--new-window", "--remote", f"ssh-remote+{host}"],
                                 cwd=str(Path(executable).parent),
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                self.json_response({"ok": True})
            except (ValueError, FileNotFoundError, json.JSONDecodeError, OSError) as exc:
                self.json_response({"ok": False, "error": str(exc)}, 400)
            return
        if urlparse(self.path).path == "/api/pick-path":
            self.pick_path()
            return
        if urlparse(self.path).path == "/api/settings":
            self.update_settings()
            return
        self.send_error(404)

    def pick_path(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            data = json.loads(self.rfile.read(length).decode("utf-8"))
            target = data.get("target")
            mode = data.get("mode") or "file"
            if target not in {"ssh_config", "cursor", "vscode"} or mode not in {"file", "folder"}:
                raise ValueError("参数无效")
        except (ValueError, json.JSONDecodeError) as exc:
            self.json_response({"ok": False, "error": str(exc)}, 400)
            return
        if not PICK_LOCK.acquire(blocking=False):
            self.json_response({"ok": False, "error": "已有选择窗口打开，请先完成或关闭它。"}, 409)
            return
        try:
            result = run_path_picker(target, mode)
        finally:
            PICK_LOCK.release()
        self.json_response(result)

    def update_settings(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            data = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(data, dict):
                raise ValueError("设置格式无效")
        except (ValueError, json.JSONDecodeError) as exc:
            self.json_response({"ok": False, "error": str(exc)}, 400)
            return
        settings = load_settings()
        before = dict(settings)
        errors = []
        if "ssh_config" in data:
            value = str(data.get("ssh_config") or "").strip()
            if value:
                candidate = Path(expand_path(value))
                if candidate.is_dir():
                    candidate = candidate / "config"
                if candidate.is_file():
                    settings["ssh_config"] = str(candidate)
                else:
                    errors.append(f"SSH 配置文件不存在：{candidate}")
            else:
                settings["ssh_config"] = ""
        for editor, label in (("cursor", "Cursor"), ("vscode", "VS Code")):
            if editor not in data:
                continue
            value = str(data.get(editor) or "").strip()
            if not value:
                settings[editor] = ""
                continue
            resolved = resolve_editor_target(editor, value)
            if resolved:
                settings[editor] = resolved
            else:
                errors.append(f"{label} 路径无效：{value}")
        if "internal_prefix" in data:
            settings["internal_prefix"] = str(data.get("internal_prefix") or "").strip()[:64]
        if errors:
            self.json_response({"ok": False, "error": "；".join(errors)}, 400)
            return
        try:
            save_settings_data(settings)
        except OSError as exc:
            self.json_response({"ok": False, "error": f"写入 settings.json 失败：{exc}"}, 400)
            return
        if before.get("ssh_config") != settings.get("ssh_config"):
            stop_all_workers()
            with CACHE_LOCK:
                CACHE.clear()
        payload = settings_payload()
        payload["ok"] = True
        self.json_response(payload)

    def log_message(self, format, *args):
        return


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Local multi-server GPU dashboard")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"GPU Monitor running at http://{args.host}:{args.port}")
    print(f"SSH config: {ssh_config_path()}")
    print(f"Settings: {SETTINGS_PATH}")
    def idle_watchdog():
        while True:
            time.sleep(5)
            with CLIENT_LOCK:
                close_requested = CLOSE_REQUESTED_AT
            if close_requested and time.monotonic() - close_requested > 8:
                server.shutdown()
                break
    threading.Thread(target=idle_watchdog, daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped")
    finally:
        stop_all_workers()
        server.server_close()


if __name__ == "__main__":
    main()
