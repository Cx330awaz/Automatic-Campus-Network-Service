# -*- coding: utf-8 -*-
"""
诊断工具：抓取"连上 JOU 后 Windows 自动弹出的登录页窗口"
=========================================================

用途：
    在脚本写死"登录后关掉哪个窗口"之前，先搞清楚弹出来的到底是什么。
    本工具会持续监视系统，把**新出现的进程**（含完整命令行）和
    **新出现的可见窗口**（含窗口类名、所属进程）记录下来。

用法：
    python diag_popup.py              # 监视 150 秒
    python diag_popup.py 300          # 监视 300 秒

步骤：
    1. 先运行本工具（它会先拍一张"基线"快照）
    2. 看到 "基线已记录" 之后，去断开 JOU 再重新连上
    3. 等弹出的登录页出现（先别关它）
    4. 等工具自己结束，把 diag/ 下生成的 log 文件内容发出来

说明：只在同权限级别下能读到其它进程的命令行；浏览器/门户窗口都是当前
用户启动的，正常能读到。
"""

from __future__ import annotations

import base64
import ctypes
import ctypes.wintypes as wt
import subprocess
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DIAG_DIR = BASE_DIR / "diag"

u32 = ctypes.WinDLL("user32", use_last_error=True)
k32 = ctypes.WinDLL("kernel32", use_last_error=True)

TH32CS_SNAPPROCESS = 0x00000002
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

# 常见的浏览器 / 门户宿主进程关键字，用于在输出里高亮
BROWSER_HINTS = (
    "chrome", "msedge", "firefox", "iexplore", "brave", "opera",
    "webview", "widgets", "explorer", "wlan", "wcmsvc", "dllhost",
)


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wt.DWORD),
        ("cntUsage", wt.DWORD),
        ("th32ProcessID", wt.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", wt.DWORD),
        ("cntThreads", wt.DWORD),
        ("th32ParentProcessID", wt.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wt.DWORD),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


def snapshot_processes() -> dict[int, str]:
    """返回 {pid: 可执行文件名}。"""
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == INVALID_HANDLE_VALUE:
        return {}

    entry = PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
    out: dict[int, str] = {}
    try:
        ok = k32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            pid = entry.th32ProcessID
            if pid not in (0, 4):
                out[pid] = entry.szExeFile
            ok = k32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        k32.CloseHandle(snap)
    return out


def window_class(hwnd) -> str:
    buf = ctypes.create_unicode_buffer(256)
    u32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def pid_exe(pid: int) -> str:
    """按 pid 取可执行文件全路径，取不到就返回空。"""
    h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(32768)
        size = wt.DWORD(32768)
        if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return buf.value
    finally:
        k32.CloseHandle(h)
    return ""


def snapshot_windows() -> dict[int, tuple[int, str, str]]:
    """返回 {hwnd: (pid, 标题, 窗口类名)}，只看可见且有标题的顶层窗口。"""
    out: dict[int, tuple[int, str, str]] = {}
    CB = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)

    def cb(hwnd, _):
        if not u32.IsWindowVisible(hwnd):
            return True
        n = u32.GetWindowTextLengthW(hwnd)
        if n == 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        u32.GetWindowTextW(hwnd, buf, n + 1)
        pid = wt.DWORD()
        u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        out[int(hwnd)] = (pid.value, buf.value, window_class(hwnd))
        return True

    u32.EnumWindows(CB(cb), 0)
    return out


def process_cmdline(pid: int) -> str:
    """借 PowerShell 读某个进程的完整命令行。

    结果用 base64 吐出来 —— 输出全是 ASCII，不受控制台代码页影响，
    也不用落临时文件（脚本目录带中文，落文件很容易被编码搞乱）。

    注意别写成 `if (...) { ... } | Out-File`：PowerShell 会报"不能使用空管道
    元素"并静默失败，什么都拿不到。
    """
    query = (
        f"$p = Get-CimInstance Win32_Process -Filter 'ProcessId={pid}';"
        "if ($p -and $p.CommandLine) {"
        " [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($p.CommandLine)) }"
    )
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-Command", query],
            capture_output=True, timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as e:
        return f"<读取命令行失败: {e}>"

    raw = (proc.stdout or b"").strip()
    if not raw:
        return ""
    try:
        return base64.b64decode(raw).decode("utf-8", errors="replace")
    except Exception:
        return ""


def main() -> int:
    duration = int(sys.argv[1]) if len(sys.argv) > 1 else 150
    DIAG_DIR.mkdir(exist_ok=True)

    sys.stdout.reconfigure(errors="replace")
    stamp = time.strftime("%Y%m%d_%H%M%S")
    log_path = DIAG_DIR / f"popup_{stamp}.log"
    lines: list[str] = []

    def emit(text: str = "") -> None:
        lines.append(text)
        print(text)

    emit("==================== 残留门户口径抓取 ====================")
    emit(f"开始时间：{time.strftime('%Y-%m-%d %H:%M:%S')}   监视时长：{duration} 秒")
    emit(f"日志文件：{log_path}")
    emit()

    # ---- 基线 ----
    base_procs = snapshot_processes()
    base_wins = snapshot_windows()
    emit(f"基线已记录：进程 {len(base_procs)} 个，可见窗口 {len(base_wins)} 个")
    emit()
    emit(">>> 现在请断开 JOU，再重新连上；等弹出的登录页出现后先不要关它。 <<<")
    emit()

    known_pids: dict[int, str] = dict(base_procs)
    new_pids: dict[int, str] = {}
    known_hwnds = set(base_wins)

    deadline = time.time() + duration
    while time.time() < deadline:
        left = int(deadline - time.time())

        # 新进程
        for pid, exe in snapshot_processes().items():
            if pid not in known_pids:
                known_pids[pid] = exe
                new_pids[pid] = exe
                hit = any(h in exe.lower() for h in BROWSER_HINTS)
                mark = "  <<< 命中关键字" if hit else ""
                emit(f"[新进程] {time.strftime('%H:%M:%S')} pid={pid} {exe}{mark}")
                cmd = process_cmdline(pid)
                if cmd:
                    emit(f"         命令行：{cmd}")
                path = pid_exe(pid)
                if path and not cmd:
                    emit(f"         路径：{path}")

        # 新窗口
        for hwnd, (pid, title, cls) in snapshot_windows().items():
            if hwnd not in known_hwnds:
                known_hwnds.add(hwnd)
                exe = known_pids.get(pid, "?")
                emit(f"[新窗口] {time.strftime('%H:%M:%S')} pid={pid} {exe}")
                emit(f"         标题：{title}")
                emit(f"         类名：{cls}")

        print(f"\r...剩余 {left:>3} 秒", end="", flush=True)
        time.sleep(0.7)

    print()
    emit()
    emit("==================== 汇总 ====================")
    if new_pids:
        emit(f"共捕获 {len(new_pids)} 个新进程：")
        for pid, exe in new_pids.items():
            emit(f"  {exe}  (pid={pid})")
    else:
        emit("未捕获到任何新进程。")
    emit()
    emit("请把上面全部内容发出来，重点是【新窗口】和【新进程】那几段。")
    emit(f"（同样内容已存到 {log_path}）")

    log_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n日志已写入：{log_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
