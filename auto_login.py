# -*- coding: utf-8 -*-
"""
校园网 Wi-Fi 自动登录脚本
=========================

流程：
  1. 连接 WLAN 热点 "JOU"
  2. 等待认证门户(captive portal)出现，自动探测门户地址
  3. 用浏览器打开门户，自动填入账号 / 密码，选择运营商（如"中国联通"）
  4. 提交并确认联网成功，关闭浏览器，并顺手关掉 Windows 自己弹出的那个门户页

用法：
    python auto_login.py              # 正常执行
    python auto_login.py --dump       # 调试：保存门户页面 HTML 与截图到 debug/
    python auto_login.py --debug      # 打印详细日志到控制台

首次运行会在脚本目录自动生成 config.ini，请在里填账号密码。
"""

from __future__ import annotations

import argparse
import base64
import configparser
import ctypes
import ctypes.wintypes as wt
import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import requests
from selenium import webdriver
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import Select

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.ini"
LOG_DIR = BASE_DIR / "logs"
DEBUG_DIR = BASE_DIR / "debug"

# 用于探测"是否已联网 / 是否被劫持到门户"的探测地址
# 正常联网时返回固定内容；被门户劫持时会 302 跳转到登录页
PROBE_URLS = [
    "http://www.msftconnecttest.com/connecttest.txt",
    "http://connectivitycheck.gstatic.com/generate_204",
]

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0"
)

# 登录成功 / 失败 的页面上可能出现的提示词
SUCCESS_WORDS = ["成功", "已登录", "注销", "退出", "欢迎", "success", "logout"]
ERROR_WORDS = ["密码错误", "账号或密码", "用户名或密码", "密码有误", "错误", "失败", "error", "invalid"]

CONFIG_TEMPLATE = """[account]
# 校园网账号与密码（务必填写）
username =
password =

# 运营商选择。脚本会按下面的关键词在页面里模糊匹配一项：
# 例如填 "中国联通" 就能匹配 "中国联通/联通/Unicom" 等写法
isp = 中国联通

[network]
# 要连接的 WLAN 热点名称
ssid = JOU
# 无线网卡接口名（netsh wlan show interfaces 里的"名称"）
interface = WLAN
# 等待连接上 Wi-Fi 的时间（秒）
connect_timeout = 45
# 等待认证门户出现的时间（秒）
portal_timeout = 40
# 提交后等待联网成功的时间（秒）
login_timeout = 30

[browser]
# 浏览器内核：edge 或 chrome
engine = edge
# 是否无头运行（不显示窗口）。设 false 可以看到自动填充过程
headless = false
# 登录成功后停留几秒再关闭浏览器
keep_open_seconds = 2
# 登录成功后，是否关闭 Windows 连 Wi-Fi 时自动弹出的那个门户登录页窗口
# （那个窗口是系统用默认浏览器弹的，不属于脚本，driver.quit() 关不掉）
close_portal_window = true
"""

log = logging.getLogger("campus")


# --------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------

def setup_logging(verbose: bool) -> None:
    LOG_DIR.mkdir(exist_ok=True)
    log.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")

    fh = logging.FileHandler(LOG_DIR / "auto_login.log", encoding="utf-8")
    fh.setFormatter(fmt)
    log.addHandler(fh)

    # pythonw.exe 无控制台时 sys.stdout 为 None，此时跳过控制台日志
    if verbose and sys.stdout is not None:
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        log.addHandler(sh)


def load_config(path: Path) -> configparser.ConfigParser:
    """读取 config.ini，不存在则生成模板并退出。"""
    if not path.exists():
        path.write_text(CONFIG_TEMPLATE, encoding="utf-8")
        log.warning("已生成配置文件：%s —— 请填入账号和密码后重新运行。", path)
        print(f"\n已生成配置文件：{path}\n请打开它填入账号(username)和密码(password)后重新运行。\n")
        sys.exit(2)

    cfg = configparser.ConfigParser()
    cfg.read(path, encoding="utf-8")

    if not cfg.get("account", "username", fallback="").strip():
        log.error("config.ini 里的 username 为空")
        print(f"请先在 {path} 里填写 username 和 password。")
        sys.exit(2)
    return cfg


def run_hidden(cmd: list[str], timeout: int = 30) -> tuple[int, str]:
    """执行命令并返回 (返回码, 输出文本)。兼容中文 Windows 的 GBK 输出。"""
    flags = 0
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        p = subprocess.run(
            cmd, capture_output=True, timeout=timeout,
            creationflags=flags, shell=False,
        )
    except subprocess.TimeoutExpired:
        return 1, ""

    raw = (p.stdout or b"") + (p.stderr or b"")
    for enc in ("utf-8", "gbk", "cp936"):
        try:
            return p.returncode, raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return p.returncode, raw.decode("utf-8", errors="replace")


# --------------------------------------------------------------------------
# 网络：连接 Wi-Fi / 探测门户
# --------------------------------------------------------------------------

def current_ssid(interface: str) -> str | None:
    """返回指定网卡当前连接的 SSID；未连接返回 None。

    netsh 的输出在中文系统上是 GBK，但 'SSID' 字样始终是 ASCII，可按此解析。
    输出按空行分成若干网卡块，每块第一行是该网卡名称（标签本身随系统语言变化，
    但取值就是网卡名），因此按"值"来定位目标网卡即可跨语言通用。
    """
    _, out = run_hidden(["netsh", "wlan", "show", "interfaces"])
    fallback: str | None = None

    for block in re.split(r"\n\s*\n", out):
        lines = [ln for ln in block.splitlines() if ln.strip()]
        if not lines:
            continue

        # 收集该块里所有 "标签: 值" 的取值，用于判断这是不是目标网卡
        values = [re.split(r"[:：]", ln, maxsplit=1)[1].strip()
                  for ln in lines if re.search(r"[:：]", ln)]

        ssid = None
        for ln in lines:
            if "BSSID" in ln.upper():
                continue
            m = re.search(r"(?<![A-Za-z])SSID\s*[:：]\s*(.+?)\s*$", ln)
            if m:
                ssid = m.group(1).strip()
                break

        if ssid is None:
            continue
        if interface and interface in values:
            return ssid          # 命中指定网卡
        if fallback is None:
            fallback = ssid      # 网卡名对不上时的兜底

    return fallback


def connect_wifi(ssid: str, interface: str, timeout: int) -> bool:
    """连接指定 Wi-Fi，带重试，直到探测到网络响应为止。"""
    if current_ssid(interface) == ssid:
        log.info("已经连接在 %s 上，跳过连接步骤", ssid)
        return True

    log.info("正在连接 Wi-Fi：%s", ssid)
    deadline = time.time() + timeout
    attempt = 0

    while time.time() < deadline:
        attempt += 1
        code, out = run_hidden(
            ["netsh", "wlan", "connect", f"name={ssid}", f"interface={interface}"],
            timeout=15,
        )
        if code != 0 or "失败" in out or "failed" in out.lower():
            log.debug("第 %d 次连接返回：%s", attempt, out.strip()[:200])

        # 等待网卡拿到 IP / 门户响应：只要探测请求有响应（哪怕是跳转）就算连上了
        for _ in range(6):
            time.sleep(2)
            if current_ssid(interface) == ssid:
                log.info("已关联到 %s，正在等待网络就绪…", ssid)
                # 再探测一次，确认链路可用
                online, portal = probe_internet()
                if online or portal:
                    log.info("网络就绪（online=%s, portal=%s）", online, portal or "无")
                    return True
        log.debug("第 %d 次尝试后仍未就绪，重试…", attempt)

    log.error("连接 %s 超时（%d 秒）", ssid, timeout)
    return False


def probe_internet() -> tuple[bool, str | None]:
    """探测网络状态。

    返回 (是否已能正常上网, 门户登录页地址或 None)。
    """
    session = requests.Session()
    session.trust_env = False  # 忽略系统代理，避免误判

    for url in PROBE_URLS:
        try:
            r = session.get(
                url, timeout=6, allow_redirects=False,
                headers={"User-Agent": UA, "Cache-Control": "no-cache"},
            )
        except requests.RequestException as e:
            log.debug("探测 %s 失败：%s", url, e)
            continue

        # 正常联网
        if r.status_code in (200, 204):
            if "connecttest" in url and "Microsoft Connect Test" not in r.text:
                # 返回 200 但内容不对 —— 门户用 200 伪装劫持
                pass
            else:
                return True, None

        # 被门户劫持
        if r.status_code in (301, 302, 303, 307, 308):
            location = r.headers.get("Location", "")
            if location:
                log.info("检测到认证门户跳转：%s", location)
                return False, location

    return False, None


def wait_for_portal(timeout: int) -> str | None:
    """在 timeout 秒内轮询，直到出现门户地址或网络已通。

    返回门户地址；若已经能上网则返回 None（调用方据此判断无需登录）。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        online, portal = probe_internet()
        if portal:
            return portal
        if online:
            return None
        time.sleep(2)
    return None


def resolve_portal_url(portal: str) -> str:
    """把跳转地址补全为可访问的完整 URL。"""
    if portal.startswith("//"):
        return "http:" + portal
    if not portal.lower().startswith(("http://", "https://")):
        return "http://" + portal
    return portal


# --------------------------------------------------------------------------
# 浏览器自动化
# --------------------------------------------------------------------------

def build_driver(engine: str, headless: bool):
    """创建 WebDriver；驱动未缓存时自动降级重试。"""
    if engine in ("edge", "msedge"):
        opts = webdriver.EdgeOptions()
        make = webdriver.Edge
    elif engine in ("chrome", "google-chrome"):
        opts = webdriver.ChromeOptions()
        make = webdriver.Chrome
    else:
        raise ValueError(f"不支持的浏览器内核：{engine}")

    opts.add_argument("--start-maximized")
    opts.add_argument("--disable-notifications")
    opts.add_argument("--disable-features=msEdgeSidebarV2,Translate")
    opts.add_experimental_option("excludeSwitches", ["enable-logging", "enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    if headless:
        opts.add_argument("--headless=new")
        opts.add_argument("--window-size=1280,900")

    try:
        return make(options=opts)
    except WebDriverException as e:
        # 断网开机场景：Selenium Manager 无法联网下载驱动，改用本地缓存
        log.warning("首次启动 WebDriver 失败（%s），尝试离线模式…", str(e).splitlines()[0])
        os.environ["SE_OFFLINE"] = "true"
        return make(options=opts)


def _is_visible(el) -> bool:
    try:
        return el.is_displayed() and el.is_enabled()
    except Exception:
        return False


def _first_visible(driver, selectors: list[str]):
    for css in selectors:
        try:
            for el in driver.find_elements(By.CSS_SELECTOR, css):
                if _is_visible(el):
                    return el
        except Exception:
            continue
    return None


def _type_into(el, value: str) -> None:
    """模拟真人输入，兼容 Vue/React 这类框架绑定的输入框。"""
    el.click()
    try:
        el.send_keys(Keys.CONTROL, "a")
        el.send_keys(Keys.DELETE)
    except Exception:
        try:
            el.clear()
        except Exception:
            pass
    el.send_keys(value)
    # 触发框架的 change 事件
    try:
        el.dispatch_event  # noqa: B018  (Selenium 4.15+ 才有；没有则走下面)
        el._parent.execute_script(
            "arguments[0].dispatchEvent(new Event('input',{bubbles:true}));"
            "arguments[0].dispatchEvent(new Event('change',{bubbles:true}));",
            el,
        )
    except Exception:
        pass


def select_isp(driver, keywords: list[str]) -> bool:
    """选择运营商：先找原生 <select>，再退化为点击单选框/列表项。"""
    # 1) 原生下拉框
    for sel_el in driver.find_elements(By.TAG_NAME, "select"):
        try:
            s = Select(sel_el)
            for opt in s.options:
                text = (opt.text or "").strip()
                if any(k.lower() in text.lower() for k in keywords):
                    s.select_by_visible_text(text)
                    log.info("已选择运营商：%s", text)
                    return True
        except Exception:
            continue

    # 2) 可点击元素（radio / label / li / div / span / a）
    xpath = "|".join(
        f"//label[contains(normalize-space(.),'{k}')]"
        f"|//li[contains(normalize-space(.),'{k}')]"
        f"|//span[contains(normalize-space(.),'{k}')]"
        f"|//a[contains(normalize-space(.),'{k}')]"
        f"|//div[contains(normalize-space(.),'{k}')]"
        for k in keywords
    )
    try:
        candidates = [e for e in driver.find_elements(By.XPATH, xpath) if _is_visible(e)]
    except Exception:
        candidates = []

    if candidates:
        # 取最内层（没有同样命中关键词的子元素）那个，避免点到外层大容器
        def depth(e):
            return len(e.find_elements(By.XPATH, "./ancestor::*"))

        target = max(candidates, key=depth)
        try:
            target.click()
            log.info("已点击运营商选项：%s", (target.text or "").strip()[:20])
            return True
        except Exception as e:
            log.debug("点击运营商选项失败：%s", e)

    log.warning("未能选择运营商（关键词：%s），继续尝试登录", keywords)
    return False


def fill_and_submit(driver, username: str, password: str, keywords: list[str]) -> None:
    driver.implicitly_wait(0)

    def find(css, what):
        el = _first_visible(driver, css)
        if el is None:
            raise RuntimeError(f"页面上找不到{what}输入框")
        return el

    # 先选运营商（部分门户会据此调整账号规则），再填账号密码
    select_isp(driver, keywords)
    time.sleep(0.5)

    user_el = find([
        "input[type='text']", "input[type='email']", "input[type='tel']",
        "input[name*='user' i]", "input[id*='user' i]",
        "input[name*='account' i]", "input[name*='name' i]", "input[id*='name' i]",
        "input[type='number']",
    ], "账号")

    # 排除隐藏的账号框，保证拿到的是第一个可见的
    _type_into(user_el, username)
    log.info("已填写账号")

    pwd_el = find(["input[type='password']"], "密码")
    _type_into(pwd_el, password)
    log.info("已填写密码")

    # 优先点密码框所在表单里的提交按钮
    btn = None
    try:
        form = pwd_el.find_element(By.XPATH, "./ancestor::form[1]")
        for css in ["button[type='submit']", "input[type='submit']", "button", "a"]:
            for b in form.find_elements(By.CSS_SELECTOR, css):
                if _is_visible(b):
                    btn = b
                    break
            if btn:
                break
    except Exception:
        pass

    if btn is None:
        for css in [
            "button[type='submit']", "input[type='submit']",
            "button[id*='login' i]", "a[id*='login' i]",
            "button[class*='login' i]", "button[class*='submit' i]",
        ]:
            btn = _first_visible(driver, [css])
            if btn:
                break

    if btn is None:
        # 最后按文字找
        xp = ("//button|//a|//input[@type='button']|//span[contains(@class,'btn')]")
        for b in driver.find_elements(By.XPATH, xp):
            text = (b.text or b.get_attribute("value") or "").strip()
            if any(w in text for w in ["登录", "登陆", "连接", "确定", "提交", "Login", "Sign in"]):
                if _is_visible(b):
                    btn = b
                    break

    if btn is None:
        log.warning("未找到登录按钮，改为在密码框回车提交")
        pwd_el.send_keys(Keys.ENTER)
        return

    btn.click()
    log.info("已提交登录表单")


# --------------------------------------------------------------------------
# 收尾：关掉 Windows 自己弹出来的门户登录页
# --------------------------------------------------------------------------
#
# 连上强制门户后，Windows 的 NCSI 会探测到"被劫持到登录页"，于是用**默认浏览器**
# 弹一个门户窗口（本机默认浏览器是 Chrome，所以看到的就是 Chrome 窗口）。
# 那个窗口不是 Selenium 拉起来的，driver.quit() 管不到它 —— 登录完成后它就留在
# 桌面上，也就是所谓"残留的网页"。下面负责在登录成功后把它找出来关掉。
#
# 判断依据按可靠度排序：
#   1) 进程命令行里带着门户域名 —— 铁证。Windows 是 `chrome.exe "http://<门户>/..."`
#      这么拉起浏览器的，你自己的 Chrome 命令行里不会有门户地址。
#   2) 窗口标题命中门户特征 —— 门户页的标题（脚本里能直接读到）或固定字样。
#   3) 其余"脚本开跑后新出现"的浏览器窗口 —— 只记日志、不关，宁可漏关也不误伤
#      你自己的标签页。

WM_CLOSE = 0x0010

# 可能被 Windows 拉去弹门户页的浏览器。刻意不含 msedgewebview2.exe：
# 那是系统组件（搜索、小组件等）的宿主，关了会出别的问题。
BROWSER_EXES = {
    "chrome.exe", "msedge.exe", "firefox.exe", "brave.exe",
    "opera.exe", "vivaldi.exe", "360se.exe", "360chrome.exe",
}

# 门户登录页标题里常见的固定字样，作为兜底匹配
PORTAL_TITLE_HINTS = ("上网登录页", "门户登录", "portal login")

_win32 = ctypes.WinDLL("user32", use_last_error=True)
_k32 = ctypes.WinDLL("kernel32", use_last_error=True)

# 显式声明参数类型：HWND 在 64 位下是 8 字节，不声明的话 ctypes 会按 int 截断
_win32.IsWindowVisible.argtypes = [wt.HWND]
_win32.GetWindowTextLengthW.argtypes = [wt.HWND]
_win32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
_win32.GetClassNameW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
_win32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
_win32.PostMessageW.argtypes = [wt.HWND, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
_k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
_k32.OpenProcess.restype = wt.HANDLE
_k32.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, wt.LPWSTR,
                                            ctypes.POINTER(wt.DWORD)]
_k32.CloseHandle.argtypes = [wt.HANDLE]


def _visible_windows() -> dict[int, tuple[int, str, str]]:
    """列出可见且有标题的顶层窗口：{窗口句柄: (进程号, 标题, 窗口类名)}。"""
    found: dict[int, tuple[int, str, str]] = {}
    callback = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)

    def visit(hwnd, _):
        if not _win32.IsWindowVisible(hwnd):
            return True
        length = _win32.GetWindowTextLengthW(hwnd)
        if length == 0:
            return True
        title = ctypes.create_unicode_buffer(length + 1)
        _win32.GetWindowTextW(hwnd, title, length + 1)
        cls = ctypes.create_unicode_buffer(256)
        _win32.GetClassNameW(hwnd, cls, 256)
        pid = wt.DWORD()
        _win32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        found[int(hwnd)] = (pid.value, title.value, cls.value)
        return True

    _win32.EnumWindows(callback(visit), 0)
    return found


def _pid_exe(pid: int) -> str:
    """按进程号取可执行文件名（小写）；取不到返回空串。"""
    handle = _k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(32768)
        size = wt.DWORD(32768)
        if _k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return buf.value.rsplit("\\", 1)[-1].lower()
    finally:
        _k32.CloseHandle(handle)
    return ""


def _pid_cmdline(pid: int) -> str:
    """取某进程的完整命令行，失败返回空串。

    借 PowerShell 查，结果用 base64 吐出来 —— 输出全是 ASCII，不受控制台
    代码页影响，也不用落临时文件。

    注意别写成 `if (...) { ... } | Out-File`：PowerShell 会报"不能使用空管道
    元素"，静默拿不到任何东西。
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
        log.debug("读取 pid=%s 的命令行失败：%s", pid, e)
        return ""

    raw = (proc.stdout or b"").strip()
    if not raw:
        return ""
    try:
        return base64.b64decode(raw).decode("utf-8", errors="replace")
    except Exception:
        return ""


def close_stray_portal_windows(portal_url: str, portal_title: str,
                               baseline: set[int]) -> int:
    """关掉 Windows 自动弹出的门户登录页窗口，返回关掉的数量。

    baseline 是脚本开跑前就存在的窗口句柄集合，用来识别"新冒出来的"窗口。
    """
    host = urlparse(portal_url).hostname or ""
    matchers = [m.lower() for m in (host, portal_title.strip(), *PORTAL_TITLE_HINTS)
                if len(m) >= 4]

    closed = 0
    cmdline_cache: dict[int, str] = {}

    for hwnd, (pid, title, _cls) in _visible_windows().items():
        if _pid_exe(pid) not in BROWSER_EXES:
            continue

        lowered = title.lower()
        title_hit = any(m in lowered for m in matchers)

        if pid not in cmdline_cache:
            cmdline_cache[pid] = _pid_cmdline(pid).lower()
        cmd_hit = bool(host) and host.lower() in cmdline_cache[pid]

        if title_hit or cmd_hit:
            log.info("关闭 Windows 自动弹出的门户窗口：标题=%r（%s）",
                     title, "标题命中" if title_hit else "命令行命中")
            _win32.PostMessageW(wt.HWND(hwnd), WM_CLOSE, 0, 0)
            closed += 1
        elif hwnd not in baseline:
            log.warning("有个新出现的浏览器窗口无法确认是不是门户（标题=%r），"
                        "没有自动关；不是你开的话手动关一下", title)

    if closed:
        log.info("共关闭 %d 个门户窗口", closed)
    return closed


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------

def dump_debug(driver) -> None:
    """保存门户页面快照，便于排查选择器问题。"""
    DEBUG_DIR.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    try:
        (DEBUG_DIR / f"portal_{stamp}.html").write_text(driver.page_source, encoding="utf-8")
        driver.save_screenshot(str(DEBUG_DIR / f"portal_{stamp}.png"))
        log.info("已保存调试快照到 %s", DEBUG_DIR)
    except Exception as e:
        log.warning("保存调试快照失败：%s", e)


def wait_online(timeout: int) -> bool:
    """轮询确认是否真正联网成功。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        online, _ = probe_internet()
        if online:
            return True
        time.sleep(2)
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="校园网 Wi-Fi 自动登录")
    parser.add_argument("--config", default=str(CONFIG_PATH), help="配置文件路径")
    parser.add_argument("--debug", action="store_true", help="输出详细日志到控制台")
    parser.add_argument("--dump", action="store_true", help="保存门户页面 HTML/截图用于调试")
    args = parser.parse_args()

    setup_logging(args.debug)
    log.info("=" * 60)
    log.info("校园网自动登录启动")

    cfg = load_config(Path(args.config))

    username = cfg.get("account", "username").strip()
    password = cfg.get("account", "password").strip()
    if not password:
        log.error("config.ini 里的 password 为空")
        print("请先在 config.ini 里填写 password。")
        return 2

    ssid = cfg.get("network", "ssid", fallback="JOU").strip()
    iface = cfg.get("network", "interface", fallback="WLAN").strip()
    connect_timeout = cfg.getint("network", "connect_timeout", fallback=45)
    portal_timeout = cfg.getint("network", "portal_timeout", fallback=40)
    login_timeout = cfg.getint("network", "login_timeout", fallback=30)

    engine = cfg.get("browser", "engine", fallback="edge").strip()
    headless = cfg.getboolean("browser", "headless", fallback=False)
    keep_open = cfg.getint("browser", "keep_open_seconds", fallback=2)
    close_portal = cfg.getboolean("browser", "close_portal_window", fallback=True)

    # 运营商关键词：把配置里的写法拆开，并补上常见同义写法
    isp_raw = cfg.get("account", "isp", fallback="中国联通").strip()
    keywords = [k.strip() for k in re.split(r"[,，/、]", isp_raw) if k.strip()]
    keywords += ["联通", "连通", "Unicom"]          # 常见叫法/错别字兜底
    keywords = list(dict.fromkeys(keywords))        # 去重保序

    # 记下开跑前就存在的窗口。Windows 弹的门户页窗口是"新冒出来"的，
    # 有这个基线才能把它和用户自己早就开着的窗口区分开。
    baseline: set[int] = set()
    if close_portal:
        try:
            baseline = set(_visible_windows())
            log.debug("窗口基线：%d 个", len(baseline))
        except Exception as e:
            log.debug("窗口基线快照失败：%s", e)

    # --- 1. 连接 Wi-Fi ---
    if not connect_wifi(ssid, iface, connect_timeout):
        log.error("Wi-Fi 连接失败，脚本退出")
        return 1

    # --- 2. 检查是否已经能上网 ---
    online, portal = probe_internet()
    if online:
        log.info("当前已能正常上网，无需登录，脚本结束")
        return 0

    if not portal:
        log.info("等待认证门户出现…")
        portal = wait_for_portal(portal_timeout)
    if not portal:
        online, _ = probe_internet()
        if online:
            log.info("网络已通，无需登录")
            return 0
        log.error("未探测到认证门户，脚本退出")
        return 1

    portal_url = resolve_portal_url(portal)
    log.info("门户地址：%s", portal_url)

    # --- 3. 浏览器自动登录 ---
    driver = None
    login_ok = False        # 只有真登录成功才去清门户窗口
    portal_title = ""       # 门户页标题，用来认出 Windows 弹的那个窗口
    try:
        driver = build_driver(engine, headless)
        driver.get(portal_url)

        # 等页面基本渲染完
        time.sleep(2)

        # 记下门户页标题：Windows 弹的那个浏览器窗口标题也是它
        portal_title = (driver.title or "").strip()
        log.debug("门户页标题：%r", portal_title)

        if args.dump:
            dump_debug(driver)

        fill_and_submit(driver, username, password, keywords)

        # --- 4. 确认登录结果 ---
        if wait_online(login_timeout):
            log.info("登录成功，网络已连通")
            login_ok = True
            if keep_open > 0:
                time.sleep(keep_open)
            return 0

        # 未连通：记录现场并判断是否被拒绝
        log.error("提交后 %d 秒内仍未能上网", login_timeout)
        if args.dump:
            dump_debug(driver)
        try:
            body = driver.find_element(By.TAG_NAME, "body").text
            hit = [w for w in ERROR_WORDS if w in body]
            if hit:
                log.error("页面提示疑似错误信息：%s", "、".join(hit))
                log.error("页面文字片段：%s", body.strip().replace("\n", " ")[:200])
        except Exception:
            pass
        return 1

    except WebDriverException as e:
        log.error("浏览器自动化失败：%s", str(e).splitlines()[0])
        log.error("若为驱动缺失，请先联网手动跑一次脚本以下载驱动；本次改为手动打开门户")
        try:
            import webbrowser
            webbrowser.open(portal_url)
        except Exception:
            pass
        return 1

    except Exception as e:
        log.exception("未预期的错误：%s", e)
        return 1

    finally:
        if driver is not None:
            try:
                driver.quit()
                log.info("已关闭浏览器")
            except Exception:
                pass
        # 这时脚本自己的浏览器已经退了，剩下的门户窗口只会是 Windows 弹的那个
        if login_ok and close_portal:
            try:
                close_stray_portal_windows(portal_url, portal_title, baseline)
            except Exception as e:
                log.debug("清理门户窗口失败：%s", e)
        log.info("脚本执行结束")


if __name__ == "__main__":
    sys.exit(main())
