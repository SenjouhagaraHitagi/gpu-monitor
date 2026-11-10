# GPU Monitor

这是一个仅适用于 Windows 的本地 GPU 看板。程序通过 SSH 连接远程服务器，读取 `nvidia-smi` 和进程信息，并在浏览器中集中显示 GPU 利用率、显存、温度、功耗、进程和运行时长。

## 运行环境

- Windows 10 或更高版本；
- Python 3；
- Windows OpenSSH Client；
- 远程服务器已安装 NVIDIA 驱动和 `nvidia-smi`；
- 本机已经可以通过 SSH 密钥登录目标服务器。

程序不需要安装第三方 Python 包。

## 启动

双击：

```text
start.bat
```

启动脚本会启动本地服务并打开浏览器。也可以在 PowerShell 中手动运行：

```powershell
python app.py
```

服务地址由启动脚本显示。只有网页关闭时发送页面关闭通知，本地服务才会自动退出；后台标签页冻结、电脑睡眠或暂时没有心跳都不会触发退出。

## 采集方式

每台服务器只建立一条常驻 SSH 连接，远程按固定间隔循环执行 `nvidia-smi` 和 `ps` 并推送快照，本地解析后缓存。这样即使 SSH 建连很慢（例如家目录在 NFS 上），也只在首次连接时等待一次，后续刷新不再重复建连。

- 推送间隔默认 15 秒，与改造前每轮查询的频率一致；启动前设置环境变量 `GPU_MONITOR_INTERVAL`（秒，最小 5）可以调整；
- 连接断开后自动退避重连（5 秒起，最长 30 秒），重连期间保留上次数据并标记为“旧数据”；
- 切换内网/外网时停止另一模式的连接，避免同时维持两倍会话；
- 修改路径设置或退出程序时会结束所有常驻连接，不会在服务器上留下后台进程。

## SSH 配置

程序会自动读取当前 Windows 用户的 OpenSSH 配置文件：

```text
%USERPROFILE%\.ssh\config
```

程序使用配置中的 `Host`、`HostName`、`User`、`Port` 和 `IdentityFile`。界面显示 `HostName`，实际连接使用对应的 `Host`，因此不会绕过本机已有的 SSH 认证配置。

如果 SSH 配置不在默认位置，点击页面右上角的「路径设置」手动指定 config 文件（也可以填它所在的目录，或点「选择文件…」从系统对话框里挑），保存后写入同目录的 `settings.json`，下次启动自动生效。

服务器会根据 `HostName` 自动分为“内网”和“外网”：默认按私有 IP（`10.x`、`172.16-31.x`、`192.168.x`、`127.x` 等）判断内网，其余算外网，主机名算外网。可在「路径设置」里填「内网网段前缀」（例如 `192.168.`）覆盖该判断，保存后立即生效。服务器列表完全来自 SSH 配置，没有其他备用配置文件。

## 页面功能

- 每台服务器独立采集，单台服务器连接慢不会阻塞其他服务器；
- 页面每 15 秒读取一次最新快照，常驻 SSH 流在后台持续更新；
- GPU 卡片显示利用率、显存、温度和功耗；
- 详情页显示进程、用户名、PID、运行时长和显存；
- 只突出显示空闲和低占用 GPU；
- 低占用默认阈值为显存 40%、GPU 利用率 20%，可在页面中调整；
- 支持深色和浅色主题；
- 每台服务器提供 Cursor 和 VS Code Remote SSH 启动按钮；
- 服务器卡片支持内网/外网地址切换；
- 「路径设置」可手动指定 SSH config、Cursor、VS Code 的路径，作为自动检测的兜底，配置永久保存。

## 编辑器按钮

程序启动后会自动查找本机的 Cursor 和 VS Code：先查系统 PATH，再查 `%LOCALAPPDATA%`、`%ProgramFiles%` 等标准安装目录，不依赖任何固定盘符。要使用按钮连接服务器，需要安装对应编辑器及 Remote SSH 扩展，并确保 SSH 配置可以在 PowerShell 中正常使用。

如果按钮显示“未找到”，请安装编辑器，或将对应命令加入系统 PATH；也可以点击「路径设置」直接指定 Cursor / VS Code 的可执行文件或安装目录（支持目录，程序会自动定位其中的 exe / cmd），路径既可以手动粘贴，也可以用「选择文件…」打开 Windows 原生对话框挑选。

## 目录说明

- `app.py`：本地服务、SSH 采集和 API；
- `main.py`：打包 exe 的入口，负责启动服务并在浏览器中打开，同时处理 `--picker` 子模式；
- `launch.py`：开发时使用的启动脚本（启动服务并打开浏览器）；
- `start.bat`：Windows 启动入口；
- `web/index.html`：网页界面；
- `settings.json`：由「路径设置」生成，保存手动指定的 SSH config、Cursor、VS Code 路径；留空的项表示自动检测。该文件包含本机路径，已在 `.gitignore` 中排除，格式参考 `settings.example.json`；
- `picker.py`：为「路径设置」弹出 Windows 原生文件/目录选择框的辅助脚本，由本地服务按需调用。
- `installer.py`：安装器源码，用 PyInstaller 打包成 Setup exe。
- `launch.py` 会把运行日志写入 `launcher.log`，该文件同样不纳入版本控制。

## 安装版

`dist\GPU-Monitor-Setup-1.0.0.exe` 是免管理员的安装程序：

- 双击后选择安装位置（默认 `%LOCALAPPDATA%\Programs\GPU-Monitor`），可勾选桌面和开始菜单快捷方式；
- 安装会复制程序、创建快捷方式，并在 `HKCU` 写入卸载项（Windows“设置 → 应用”中可见）；
- 卸载方式：开始菜单「GPU Monitor 卸载」、Windows 设置里的卸载项，或再次运行安装包；
- 卸载会删除安装目录下的所有文件（包括 `settings.json`），同时清理快捷方式和卸载项；
- 安装器本身是单文件 exe，首次运行会解压到临时目录，需要几秒。
## 打包为 exe

如果不想让使用者安装 Python，可以用 PyInstaller 打成单文件 exe：

```powershell
pip install pyinstaller
pyinstaller --noconfirm --onefile --windowed --name GPU-Monitor `
  --add-data "web;web" main.py
```

按上面的命令构建时产物是 `dist\GPU-Monitor.exe`。本目录已附带两种可直接分发的成品：`dist\GPU-Monitor\`（便携版文件夹，内含 exe，已验证可用；压缩包为 `dist\GPU-Monitor-portable.zip`）和 `dist\GPU-Monitor-standalone.exe`（单文件版，部分杀毒软件可能误报或拦截，遇阻时请改用便携版）。

- 双击后会在本机启动服务并自动打开浏览器，地址仍是 `http://127.0.0.1:8767/`；
- 首次保存路径设置会在 **exe 同目录**生成 `settings.json`，所以请把 exe 放在可写目录（例如桌面或 `D:\tools`），不要放在 `Program Files`；
- 打包不包含 SSH 客户端：运行环境仍需 Windows OpenSSH Client，并且本机已能通过 SSH 密钥登录目标服务器；
- `--onefile` 模式每次启动和弹出文件选择框都要先解压到临时目录，约需 1–2 秒；介意的话把 `--onefile` 换成 `--onedir`，速度更快但产物是文件夹；
- 单文件 exe 未做代码签名，Windows SmartScreen 或杀毒软件可能提示“未知发布者”，需要为使用者说明。

## 安全说明

服务只监听 `127.0.0.1`，没有登录鉴权，请不要改成监听 `0.0.0.0` 或把端口暴露到局域网/公网。`/api/open-editor` 会在本机启动 Cursor / VS Code，`/api/pick-path` 会弹出系统文件对话框，这些能力都只应留在本机使用。

## 常见问题

### 页面显示连接失败

先在 PowerShell 中直接测试 SSH 别名，再检查用户名、端口、密钥和远程 `nvidia-smi`：

```powershell
ssh <Host别名> "nvidia-smi"
```

### GPU 有显存占用但没有进程

进程列表来自远程 `nvidia-smi` 和 `ps`。如果 SSH 用户没有权限查看其他用户进程，页面可能只能显示 GPU 资源占用。

### 修改文件后页面没有变化

重启本地服务，并在浏览器中按 `Ctrl + F5` 强制刷新。
