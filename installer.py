"""User-level installer / uninstaller for GPU Monitor.

Build (payload = the portable ``GPU-Monitor`` folder):

    pyinstaller --noconfirm --onefile --windowed --name GPU-Monitor-Setup ^
        --add-data "payload;payload" installer.py

Usage:

    Setup.exe                     install with the wizard
    Setup.exe --silent [--dir P]  silent install (no shortcuts unless enabled)
    Setup.exe --uninstall         uninstall with a confirmation dialog
    Setup.exe --uninstall --silent

Uninstalling removes every file the installer created, including settings.json.
No administrator rights are needed: everything goes to the current user's
profile (``%LOCALAPPDATA%\\Programs\\GPU-Monitor``) and ``HKCU``.
"""
import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

APP_NAME = "GPU Monitor"
APP_DIR_NAME = "GPU-Monitor"
APP_EXE = "GPU-Monitor.exe"
VERSION = "1.0.0"
REG_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\GPU-Monitor"
LEGACY_NAMES = ("GPU Monitor", "GPU Fleet Monitor")
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def resource_root():
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


def payload_dir():
    return resource_root() / "payload" / APP_DIR_NAME


def default_install_dir():
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "Programs" / APP_DIR_NAME


def start_menu_dir():
    return Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs"


def desktop_dir():
    return Path(os.environ.get("USERPROFILE", "")) / "Desktop"


def log(message, log_path=None):
    target = Path(log_path) if log_path else Path(tempfile.gettempdir()) / "gpu-monitor-install.log"
    try:
        with target.open("a", encoding="utf-8") as handle:
            handle.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}\n")
    except OSError:
        pass


def ps_quote(value):
    return "'" + str(value).replace("'", "''") + "'"


def create_shortcut(link, target, workdir):
    script = (
        f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut({ps_quote(link)});"
        f"$s.TargetPath={ps_quote(target)};"
        f"$s.WorkingDirectory={ps_quote(workdir)};"
        f"$s.IconLocation={ps_quote(target)};"
        "$s.Save()"
    )
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                   check=True, capture_output=True, text=True, creationflags=CREATE_NO_WINDOW)


def remove_shortcuts():
    for name in LEGACY_NAMES:
        for link in (desktop_dir() / f"{name}.lnk",
                     start_menu_dir() / f"{name}.lnk",
                     start_menu_dir() / f"{name} 卸载.lnk"):
            try:
                link.unlink()
            except OSError:
                pass


def register_uninstall(install_dir):
    try:
        import winreg
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, REG_KEY) as key:
            winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, APP_NAME)
            winreg.SetValueEx(key, "DisplayVersion", 0, winreg.REG_SZ, VERSION)
            winreg.SetValueEx(key, "Publisher", 0, winreg.REG_SZ, APP_NAME)
            winreg.SetValueEx(key, "InstallLocation", 0, winreg.REG_SZ, str(install_dir))
            winreg.SetValueEx(key, "DisplayIcon", 0, winreg.REG_SZ, str(install_dir / APP_EXE))
            winreg.SetValueEx(key, "UninstallString", 0, winreg.REG_SZ,
                              f'"{install_dir / "uninstall.exe"}" --uninstall')
            winreg.SetValueEx(key, "NoModify", 0, winreg.REG_DWORD, 1)
            winreg.SetValueEx(key, "NoRepair", 0, winreg.REG_DWORD, 1)
        return True
    except OSError as exc:
        log(f"registry entry skipped: {exc}")
        return False


def unregister_uninstall():
    import winreg
    for key in (REG_KEY, r"Software\Microsoft\Windows\CurrentVersion\Uninstall\GPU-Fleet-Monitor"):
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key)
        except OSError:
            pass


def copy_payload(install_dir):
    source = payload_dir()
    if not (source / APP_EXE).is_file():
        raise RuntimeError("安装包不完整：缺少程序文件 payload\\GPU-Monitor。")
    install_dir.mkdir(parents=True, exist_ok=True)
    settings = install_dir / "settings.json"
    saved = settings.read_bytes() if settings.is_file() else None
    for item in source.iterdir():
        destination = install_dir / item.name
        if item.is_dir():
            shutil.copytree(item, destination, dirs_exist_ok=True)
        elif item.name != "settings.json":
            shutil.copy2(item, destination)
    if saved is not None:
        settings.write_bytes(saved)


def write_uninstaller(install_dir):
    if getattr(sys, "frozen", False):
        target = install_dir / "uninstall.exe"
        if Path(sys.executable).resolve() != target.resolve():
            shutil.copy2(sys.executable, target)


def install(install_dir, desktop=True, startmenu=True, log_path=None):
    log(f"install start: {install_dir}", log_path)
    remove_shortcuts()
    copy_payload(install_dir)
    write_uninstaller(install_dir)
    exe = install_dir / APP_EXE
    created = []
    if startmenu:
        try:
            link = start_menu_dir() / f"{APP_NAME}.lnk"
            create_shortcut(link, exe, install_dir)
            created.append(link)
            uninstaller = install_dir / "uninstall.exe"
            if uninstaller.is_file():
                uninstall_link = start_menu_dir() / f"{APP_NAME} 卸载.lnk"
                create_shortcut(uninstall_link, uninstaller, install_dir)
                created.append(uninstall_link)
        except Exception as exc:
            log(f"start menu shortcut failed: {exc}", log_path)
    if desktop:
        try:
            link = desktop_dir() / f"{APP_NAME}.lnk"
            create_shortcut(link, exe, install_dir)
            created.append(link)
        except Exception as exc:
            log(f"desktop shortcut failed: {exc}", log_path)
    register_uninstall(install_dir)
    log("install done", log_path)
    return exe, created


def uninstall(install_dir, log_path=None):
    log(f"uninstall start: {install_dir}", log_path)
    remove_shortcuts()
    unregister_uninstall()
    if not install_dir.is_dir():
        return
    for item in install_dir.iterdir():
        if item.name == "uninstall.exe":
            continue
        try:
            if item.is_dir():
                shutil.rmtree(item)
            else:
                item.unlink()
        except OSError as exc:
            log(f"cannot remove {item}: {exc}", log_path)
    try:
        install_dir.rmdir()
    except OSError:
        schedule_self_delete(install_dir)


def schedule_self_delete(install_dir):
    """Remove the uninstaller (and its folder) shortly after this process exits.

    A tiny batch file retries the removal for up to ~30 seconds, because Windows
    keeps the running uninstall.exe locked until this process is gone.
    """
    script = Path(tempfile.gettempdir()) / "gpu-monitor-cleanup.bat"
    try:
        script.write_text(
            "@echo off\r\n"
            "timeout /t 2 /nobreak >nul\r\n"
            "set n=0\r\n"
            ":retry\r\n"
            f'rmdir /s /q "{install_dir}" 2>nul\r\n'
            f'if not exist "{install_dir}" goto done\r\n'
            "set /a n+=1\r\n"
            "if %n% geq 30 goto done\r\n"
            "timeout /t 1 /nobreak >nul\r\n"
            "goto retry\r\n"
            ":done\r\n"
            f'del /f /q "{script}" >nul 2>&1\r\n',
            encoding="ascii",
        )
        subprocess.Popen(
            ["cmd", "/c", f'"{script}"'],
            creationflags=CREATE_NO_WINDOW | getattr(subprocess, "DETACHED_PROCESS", 0),
            close_fds=True,
        )
    except OSError as exc:
        log(f"self delete failed: {exc}")


def launch_app(install_dir):
    exe = install_dir / APP_EXE
    if exe.is_file():
        subprocess.Popen([str(exe)], cwd=str(install_dir),
                         creationflags=CREATE_NO_WINDOW | getattr(subprocess, "DETACHED_PROCESS", 0),
                         close_fds=True)


def run_gui(install_dir, desktop, startmenu, uninstall_mode=False):
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    root = tk.Tk()
    root.title(f"{APP_NAME} 卸载程序" if uninstall_mode else f"{APP_NAME} 安装程序")
    root.resizable(False, False)
    frame = ttk.Frame(root, padding=18)
    frame.grid(row=0, column=0, sticky="nsew")

    ttk.Label(frame, text=APP_NAME, font=("Microsoft YaHei UI", 16, "bold")).grid(
        row=0, column=0, columnspan=3, sticky="w")
    subtitle = ("卸载会删除安装目录下的全部文件、快捷方式和卸载项"
                if uninstall_mode else f"版本 {VERSION} · 免管理员安装（当前用户）")
    ttk.Label(frame, text=subtitle).grid(row=1, column=0, columnspan=3, sticky="w", pady=(2, 14))

    path_var = tk.StringVar(value=str(install_dir))
    ttk.Label(frame, text="安装目录" if uninstall_mode else "安装位置").grid(row=2, column=0, sticky="w")
    entry = ttk.Entry(frame, textvariable=path_var, width=52)
    entry.grid(row=3, column=0, columnspan=2, sticky="we", pady=(4, 0))

    desktop_var = tk.BooleanVar(value=desktop)
    menu_var = tk.BooleanVar(value=startmenu)

    if uninstall_mode:
        entry.state(["readonly"])
        ttk.Label(frame, text="将删除：程序文件、settings.json、桌面与开始菜单快捷方式、卸载项。",
                  foreground="#8a3b3b").grid(row=4, column=0, columnspan=3, sticky="w", pady=(10, 0))
        ttk.Label(frame, text="如果看板还在运行，请先关闭页面再卸载。",
                  foreground="#8a3b3b").grid(row=5, column=0, columnspan=3, sticky="w")
    else:
        def browse():
            try:
                chosen = filedialog.askdirectory(initialdir=str(Path.home()))
            except Exception:
                chosen = filedialog.askdirectory()
            if chosen:
                path_var.set(str(Path(chosen) / APP_DIR_NAME))

        ttk.Button(frame, text="浏览…", command=browse).grid(row=3, column=2, padx=(6, 0), pady=(4, 0))
        ttk.Checkbutton(frame, text="创建桌面快捷方式", variable=desktop_var).grid(
            row=4, column=0, columnspan=3, sticky="w", pady=(10, 0))
        ttk.Checkbutton(frame, text="创建开始菜单快捷方式", variable=menu_var).grid(
            row=5, column=0, columnspan=3, sticky="w")

    status = ttk.Label(frame, text="", foreground="#52627a")
    status.grid(row=6, column=0, columnspan=3, sticky="w", pady=(12, 0))

    buttons = ttk.Frame(frame)
    buttons.grid(row=7, column=0, columnspan=3, sticky="e", pady=(10, 0))

    def do_work():
        target = Path(path_var.get().strip())
        if not target.is_absolute():
            messagebox.showerror(APP_NAME, "请填写绝对路径。")
            return
        try:
            if uninstall_mode:
                status.config(text="正在卸载…")
                root.update_idletasks()
                uninstall(target)
                messagebox.showinfo(APP_NAME, "卸载完成，安装目录已清理。")
                root.destroy()
                return
            status.config(text="正在安装…")
            root.update_idletasks()
            install(target, desktop=desktop_var.get(), startmenu=menu_var.get())
        except Exception as exc:
            status.config(text="操作失败")
            messagebox.showerror(APP_NAME, f"操作失败：{exc}")
            return
        root.destroy()
        if messagebox.askyesno(APP_NAME, f"安装完成：\n{target}\n\n现在启动吗？"):
            launch_app(target)

    action = "卸载" if uninstall_mode else "安装"
    ttk.Button(buttons, text=action, command=do_work).pack(side="left", padx=(0, 8))
    ttk.Button(buttons, text="取消", command=root.destroy).pack(side="left")

    root.mainloop()


def main():
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--dir", default=None)
    parser.add_argument("--silent", action="store_true")
    parser.add_argument("--uninstall", action="store_true")
    parser.add_argument("--no-desktop", action="store_true")
    parser.add_argument("--no-startmenu", action="store_true")
    parser.add_argument("--launch", action="store_true")
    args = parser.parse_args()
    if args.dir:
        install_dir = Path(args.dir)
    elif args.uninstall and getattr(sys, "frozen", False) and Path(sys.executable).stem.lower() == "uninstall":
        # uninstall.exe lives inside the installation it should remove
        install_dir = Path(sys.executable).resolve().parent
    else:
        install_dir = default_install_dir()
    if args.silent:
        if args.uninstall:
            uninstall(install_dir)
        else:
            install(install_dir, desktop=not args.no_desktop, startmenu=not args.no_startmenu)
            if args.launch:
                launch_app(install_dir)
        return
    run_gui(install_dir, desktop=not args.no_desktop, startmenu=not args.no_startmenu,
            uninstall_mode=args.uninstall)


if __name__ == "__main__":
    main()
