"""
Amu 桌面软件「运行登录门」参考实现（Python，标准库零依赖）
==========================================================
依赖：仅 Python 标准库（urllib / ctypes / os），无需 pip install。
Windows 上用 ctypes 调 DPAPI 做机器绑定加密存储（换用户/换机即读不出）；
非 Windows 自动降级为系统钥匙串 keyring，再降级为机器绑定 XOR 混淆。

对接的网站端点（已在 server.js 上线，域名 user.amumusic.com）：
  POST /api/auth/desktop-login   {email,password,device}            -> {ok,token,user}
  POST /api/auth/desktop-verify  {token}                            -> {ok,user} | 401/403

用法（在你桌面软件的启动处）：
    gate = AmuLoginGate()
    if gate.ensure_login():        # 有有效令牌直接进主界面
        run_main_app(gate.last_user)   # last_user 里有 nickname/email/points
    else:
        email, pwd = show_login_dialog()      # 你自己的登录 UI
        ok, msg = gate.login(email, pwd)
        if ok: run_main_app(gate.last_user)
        else: show_error(msg)

登录框记得放一个「没有账号？去注册」按钮，直接打开 REGISTER_URL（浏览器）。
退出登录：gate.logout()（删本地令牌即可，服务端令牌仍可在官网个人中心手动吊销）。
"""

import json
import os
import sys
import uuid
import platform
import urllib.request
import urllib.error

API_BASE = "https://user.amumusic.com"
REGISTER_URL = "https://user.amumusic.com/register"

# 令牌落盘位置：放用户目录（非 exe 旁边），避免装在 Program Files / /Applications 时写不进。
def _config_dir() -> str:
    """各平台规范的「用户配置目录」根，用于存放软件数据。"""
    if sys.platform == "darwin":
        # macOS 规范：~/Library/Application Support/<AppName>
        return os.path.join(os.path.expanduser("~/Library/Application Support"), "AmuTools")
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        return os.path.join(base, "AmuTools")
    # Linux/其它 POSIX：遵循 XDG 规范
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "AmuTools")


def _token_path() -> str:
    d = _config_dir()
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    return os.path.join(d, ".amu_token.bin")


def _get_keyring():
    """获取 keyring 模块；打包成 .app/.exe 后 entry point 可能收集不全，
    故在 frozen 环境下显式指定平台后端。任何失败都交给调用方降级处理。"""
    import keyring
    if getattr(sys, "frozen", False):
        try:
            if sys.platform == "darwin":
                from keyring.backends import macOS
                keyring.set_keyring(macOS.Keyring())
            elif sys.platform == "win32":
                from keyring.backends import Windows
                keyring.set_keyring(Windows.WinVaultKeyring())
        except Exception:
            pass
    return keyring


# ---------------- 令牌本地安全存储 ----------------
def _machine_key() -> bytes:
    """机器绑定的混淆密钥（仅用于防明文，真正的防护是账号要求）。"""
    raw = f"{uuid.getnode()}-{platform.node()}-{os.path.expanduser('~')}"
    return raw.encode("utf-8", "ignore")


def _dpapi_protect(data: bytes) -> bytes:
    """Windows DPAPI 加密（当前用户作用域）。失败抛异常由调用方降级。"""
    import ctypes
    from ctypes import wintypes
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    DWORD = wintypes.DWORD
    PBYTE = ctypes.POINTER(ctypes.c_byte)

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", DWORD), ("pbData", PBYTE)]

    CRYPTPROTECT_UI_FORBIDDEN = 0x01
    p_data = (ctypes.c_byte * len(data)).from_buffer_copy(data)
    blob_in = DATA_BLOB(len(data), p_data)
    p_out = DATA_BLOB(0, None)
    if not crypt32.CryptProtectData(
        ctypes.byref(blob_in), None, None, None, None,
        CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(p_out)
    ):
        raise ctypes.WinError()
    size = p_out.cbData
    buf = ctypes.cast(p_out.pbData, ctypes.POINTER(ctypes.c_byte * size)).contents
    out = bytes(buf)
    kernel32.LocalFree(p_out.pbData)
    return out


def _dpapi_unprotect(data: bytes) -> bytes:
    import ctypes
    from ctypes import wintypes
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    DWORD = wintypes.DWORD
    PBYTE = ctypes.POINTER(ctypes.c_byte)

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", DWORD), ("pbData", PBYTE)]

    CRYPTPROTECT_UI_FORBIDDEN = 0x01
    p_data = (ctypes.c_byte * len(data)).from_buffer_copy(data)
    blob_in = DATA_BLOB(len(data), p_data)
    p_out = DATA_BLOB(0, None)
    if not crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), None, None, None, None,
        CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(p_out)
    ):
        raise ctypes.WinError()
    size = p_out.cbData
    buf = ctypes.cast(p_out.pbData, ctypes.POINTER(ctypes.c_byte * size)).contents
    out = bytes(buf)
    kernel32.LocalFree(p_out.pbData)
    return out


def _xor(payload: bytes) -> bytes:
    k = _machine_key()
    return bytes(b ^ k[i % len(k)] for i, b in enumerate(payload))


def save_token(token: str):
    payload = token.encode("utf-8")
    path = _token_path()
    try:
        if sys.platform == "win32":
            payload = _dpapi_protect(payload)
        else:
            try:
                kr = _get_keyring()
                kr.set_password("AmuTools", "login_token", token)
                return
            except Exception:
                payload = _xor(payload)  # 降级：机器绑定混淆（非强加密，仅防明文）
    except Exception:
        payload = _xor(payload)  # 终极降级
    with open(path, "wb") as f:
        f.write(payload)
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass


def load_token() -> str | None:
    if sys.platform != "win32":
        try:
            t = _get_keyring().get_password("AmuTools", "login_token")
            if t:
                return t
        except Exception:
            pass
    path = _token_path()
    if not os.path.exists(path):
        return None
    with open(path, "rb") as f:
        payload = f.read()
    try:
        if sys.platform == "win32":
            try:
                payload = _dpapi_unprotect(payload)
            except Exception:
                payload = _xor(payload)  # 与 save 对称降级
        else:
            payload = _xor(payload)
    except Exception:
        return None
    return payload.decode("utf-8", "ignore") or None


def clear_token():
    if sys.platform != "win32":
        try:
            _get_keyring().delete_password("AmuTools", "login_token")
        except Exception:
            pass
    try:
        os.remove(_token_path())
    except Exception:
        pass


# ---------------- HTTP 封装 ----------------
def _post(path: str, body: dict) -> tuple[int, dict]:
    url = API_BASE + path
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read().decode("utf-8", "ignore") or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8", "ignore") or "{}")
        except Exception:
            return e.code, {}
    except Exception:
        return 0, {}


class AmuLoginGate:
    def __init__(self, device_name: str | None = None):
        self.device = device_name or platform.node() or "desktop"
        self.last_user = None  # 最近一次验活/登录拿到的用户信息

    def verify(self, token: str) -> bool:
        """验活：令牌有效且账号未封禁返回 True，并把 user 存进 self.last_user。"""
        code, data = _post("/api/auth/desktop-verify", {"token": token})
        if code == 200 and data.get("ok") is True:
            self.last_user = data.get("user")
            return True
        self.last_user = None
        return False

    def login(self, email: str, password: str) -> tuple[bool, str]:
        """密码登录拿令牌。返回 (成功, 错误信息)。成功已存本地。"""
        code, data = _post(
            "/api/auth/desktop-login",
            {"email": email, "password": password, "device": self.device},
        )
        if code == 200 and data.get("ok") and data.get("token"):
            save_token(data["token"])
            self.last_user = data.get("user")
            return True, ""
        return False, data.get("error", "登录失败，请稍后再试")

    def ensure_login(self) -> bool:
        """
        启动门：有本地令牌且验活通过 → True（直接进主界面，last_user 可用）；
        否则返回 False（调用方应弹登录框，登录成功后再进）。
        """
        tok = load_token()
        if tok and self.verify(tok):
            return True
        if tok:
            clear_token()  # 令牌失效，清理
        return False

    def reverify(self) -> bool:
        """运行期定时重验（如每 24h 或主功能触发前）。失败清本地令牌。"""
        tok = load_token()
        if tok and self.verify(tok):
            return True
        clear_token()
        return False

    def logout(self):
        clear_token()
        self.last_user = None


# ---------------- 演示（替换成你自己的登录 UI）----------------
if __name__ == "__main__":
    gate = AmuLoginGate()
    if not gate.ensure_login():
        print("需要登录。没有账号请先到：", REGISTER_URL)
        email = input("邮箱：").strip()
        pwd = input("密码：").strip()
        ok, msg = gate.login(email, pwd)
        if not ok:
            print("登录失败：", msg)
            sys.exit(1)
    print("登录成功，进入主界面。当前登录：", (gate.last_user or {}).get("nickname") or (gate.last_user or {}).get("email"))
