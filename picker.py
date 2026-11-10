"""Native path picker used by the dashboard's manual path settings.

The dashboard is a local-only tool, so it can open a real Windows file/folder
dialog on the machine it runs on. The chosen absolute path is printed as JSON
on stdout; an empty path means the user cancelled.

This runs as its own short-lived process so Tk always owns its main thread and
a stuck dialog can never block the HTTP server itself.
"""
import argparse
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("file", "folder"), default="file")
    parser.add_argument("--kind", choices=("ssh_config", "editor"), default="editor")
    parser.add_argument("--title", default="选择路径")
    parser.add_argument("--initial", default="")
    parser.add_argument("--auto-close-ms", type=int, default=0, help="只用来自动化测试")
    args = parser.parse_args()

    import tkinter as tk
    from tkinter import filedialog

    initial = args.initial if args.initial and Path(args.initial).exists() else ""
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    if args.auto_close_ms:
        root.after(args.auto_close_ms, root.destroy)

    options = {"parent": root, "title": args.title}
    if initial:
        options["initialdir"] = initial

    path = ""
    try:
        if args.mode == "folder":
            path = filedialog.askdirectory(mustexist=False, **options)
        else:
            if args.kind == "ssh_config":
                filetypes = [("SSH config", "config"), ("所有文件", "*.*")]
            else:
                filetypes = [("可执行文件", "*.exe *.cmd"), ("所有文件", "*.*")]
            path = filedialog.askopenfilename(filetypes=filetypes, **options)
    except tk.TclError:
        path = ""
    finally:
        try:
            root.destroy()
        except tk.TclError:
            pass
    print(json.dumps({"path": str(Path(path)) if path else ""}, ensure_ascii=False))


if __name__ == "__main__":
    main()
