# -*- coding: utf-8 -*-
"""Amu Music 音频转换器 - 主程序入口"""
import os
import sys
import threading
import mimetypes
import http.server
import urllib.parse
import webview

from converter import (Converter, FORMATS, BITRATES, SAMPLE_RATES, CHANNELS,
                       AUDIO_EXT, compute_waveform)
import edition
import amu_gate
import amu_login_gate
from platform_utils import open_in_file_manager

APP_DIR = os.path.dirname(os.path.abspath(__file__))
IS_RUNNING = getattr(sys, "frozen", False)  # PyInstaller 打包后为 True
IS_POINTS = (edition.EDITION == "points")

# 仅开发态（非打包）允许用环境变量把 API 指到本地 mock 服务器做端到端测试；
# 打包版忽略该变量，防止登录凭据被重定向。
if not IS_RUNNING and os.environ.get("AMU_API_BASE"):
    amu_login_gate.API_BASE = os.environ["AMU_API_BASE"].rstrip("/")


def res_path(*names):
    """兼容 PyInstaller 打包后的资源路径"""
    base = getattr(sys, "_MEIPASS", APP_DIR)
    return os.path.join(base, *names)


# 重要：pywebview 会递归枚举 js_api 对象的所有公开属性。
# 如果把 Window / Converter 挂在 Api 实例上，枚举会陷入
# WinForms/WebView2 的 COM 对象树，_createApi 永远完不成，
# 前端 pywebview.api 将一直是空对象。因此这里用模块级引用，
# Api 实例上只保留纯方法。
_CONV = Converter()
_WIN = None


def _billing_consume(src_path):
    """积分版计费钩子：每个音频转换成功后扣 10 积分。
    扣分失败抛异常 → converter 判该任务失败并删除输出文件。"""
    ok, code, data = amu_gate.consume(note=os.path.basename(src_path))
    if not ok:
        raise RuntimeError(data.get("error") or "扣积分失败，本次未保存输出")


# ---------- 本地音频流服务（应用内播放器用） ----------
# WebView2 默认禁止 file:// 页面加载 file:// 媒体，所以起一个
# 仅监听 127.0.0.1 随机端口的 HTTP 服务来流式供音频（支持 Range 拖动进度）。
class _AudioHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        q = urllib.parse.urlparse(self.path)
        if q.path != "/audio":
            self.send_error(404)
            return
        path = urllib.parse.parse_qs(q.query).get("path", [""])[0]
        if not path or not os.path.isfile(path):
            self.send_error(404)
            return
        size = os.path.getsize(path)
        rng = self.headers.get("Range")
        start, end = 0, size - 1
        if rng and rng.startswith("bytes="):
            try:
                s, e = rng[6:].split("-")
                start = int(s) if s else 0
                end = int(e) if e else size - 1
            except ValueError:
                start, end = 0, size - 1
        start = max(0, min(start, size - 1))
        end = max(start, min(end, size - 1))
        length = end - start + 1
        self.send_response(206 if rng else 200)
        self.send_header("Content-Type",
                         mimetypes.guess_type(path)[0] or "application/octet-stream")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if rng:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.end_headers()
        try:
            with open(path, "rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(65536, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (ConnectionAbortedError, BrokenPipeError):
            pass  # 播放器拖动进度会中断旧请求，正常

    def log_message(self, *args):
        pass  # 静默，不刷控制台


_AUDIO_PORT = 0


def _start_audio_server():
    global _AUDIO_PORT
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _AudioHandler)
    _AUDIO_PORT = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()


def _on_all_done(ids):
    """全部转换完成时，按设置自动打开输出文件夹。

    - 批量：_watch_done 只在所有任务都结束（成功/失败/停止）后才回调，
      因此这里天然就是“等到最后一个转换完”的时机，只打开一次文件夹。
    - 输出到统一目录：直接打开该目录。
    - 源目录模式（每个文件存回各自来源）：打开最后一个成功任务的来源目录。
    - 若没有任何成功项（比如全失败/手动停止）则不打开。"""
    c = _CONV
    with c.lock:
        auto = c.settings.get("auto_open_folder", False)
        use_src = c.settings.get("use_source_dir", False)
        out_dir = c.settings.get("output_dir", "")
        tasks = [c.tasks[i] for i in ids if i in c.tasks]
        success = [t for t in tasks if t["status"] == "success"]
    if not auto or not success:
        return
    if use_src:
        folder = os.path.dirname(success[-1]["path"])
    else:
        folder = out_dir
    if folder and os.path.isdir(folder):
        open_in_file_manager(folder)


class Api:
    # ---------- 提供给前端 ----------
    def get_state(self):
        c = _CONV
        with c.lock:
            tasks = []
            for tid in c.order:
                t = c.tasks[tid]
                tasks.append({k: t.get(k) for k in
                              ("id", "name", "path", "status", "progress",
                               "speed_text", "error", "meta", "out_path")})
            settings = dict(c.settings)
            running = c.running
        return {"tasks": tasks, "settings": settings, "running": running,
                "autoWorkers": c.resolve_workers(),
                "edition": edition.EDITION,
                "user": amu_gate.pub_user() if IS_POINTS else None,
                "costPerFile": amu_gate.COST_PER_FILE if IS_POINTS else 0,
                "options": {"formats": list(FORMATS.keys()),
                            "bitrates": list(BITRATES.keys()),
                            "sample_rates": list(SAMPLE_RATES.keys()),
                            "channels": list(CHANNELS.keys())}}

    def add_files_dialog(self):
        files = _WIN.create_file_dialog(
            webview.OPEN_DIALOG, allow_multiple=True,
            # 注意：pywebview 校验文件过滤器，扩展名必须用分号分隔（空格会抛 ValueError）
            file_types=["音频文件 (" + ";".join("*" + e for e in sorted(AUDIO_EXT)) + ")",
                        "所有文件 (*.*)"])
        if files:
            return _CONV.add_paths(list(files))
        return 0

    def add_folder_dialog(self):
        folder = _WIN.create_file_dialog(webview.FOLDER_DIALOG)
        if folder:
            return _CONV.add_paths([folder[0]])
        return 0

    def add_dropped_paths(self, paths):
        return _CONV.add_paths(list(paths))

    def remove_task(self, tid):
        return _CONV.remove(tid)

    def retry_task(self, tid):
        return _CONV.retry(tid)

    def clear_list(self):
        return _CONV.clear()

    def update_settings(self, kv):
        _CONV.update_settings(kv)
        return True

    def start(self):
        if IS_POINTS:
            if not amu_gate.ST["ok"]:
                return {"ok": False, "error": "请先登录 Amu 账号后再转换"}
            pts = (amu_gate.ST.get("user") or {}).get("points") or 0
            if pts < amu_gate.COST_PER_FILE:
                return {"ok": False, "error":
                        "积分不足（余额 %s）：每转换 1 个音频消耗 %d 积分，可到官网每日签到获取积分"
                        % (pts, amu_gate.COST_PER_FILE)}
        if not _CONV.start():
            return {"ok": False, "error": "没有等待中的任务"}
        return {"ok": True, "error": ""}

    # ---------- 积分版：登录门 ----------
    def gate_login(self, email, password):
        if not IS_POINTS:
            return {"ok": True, "error": ""}
        email = (email or "").strip().lower()
        if not email or not password:
            return {"ok": False, "error": "请输入邮箱和密码"}
        ok, err = amu_gate.login(email, password)
        return {"ok": ok, "error": err or ""}

    def gate_logout(self):
        if IS_POINTS:
            amu_gate.logout()
        return True

    def open_register(self):
        """打开官网注册页（默认浏览器）"""
        import webbrowser
        webbrowser.open(amu_login_gate.REGISTER_URL)
        return True

    def stop(self):
        _CONV.stop()
        return True

    def choose_output_dir(self):
        folder = _WIN.create_file_dialog(webview.FOLDER_DIALOG)
        if folder:
            _CONV.update_settings({"output_dir": folder[0]})
            return folder[0]
        return None

    def get_waveform(self, path):
        """返回音频波形峰值列表（0~1 浮点），供播放器画波形进度条"""
        if not path or not os.path.isfile(path):
            return []
        return compute_waveform(os.path.normpath(path))

    def audio_url(self, path):
        """返回本地音频流 URL（播放器 <audio> 用，支持 Range 拖动进度）"""
        if not path or not os.path.isfile(path):
            return ""
        return "http://127.0.0.1:%d/audio?path=%s" % (
            _AUDIO_PORT, urllib.parse.quote(os.path.normpath(path)))

    def open_path(self, path, reveal=False):
        path = os.path.normpath(path)
        if not os.path.exists(path):
            return False
        return open_in_file_manager(path, reveal=reveal)

    def notify_info(self):
        c = _CONV
        with c.lock:
            ok = sum(1 for t in c.tasks.values() if t["status"] == "success")
            fail = sum(1 for t in c.tasks.values() if t["status"] == "failed")
            stop = sum(1 for t in c.tasks.values() if t["status"] == "stopped")
            out_dir = c.settings["output_dir"]
            use_src = c.settings["use_source_dir"]
        return {"success": ok, "failed": fail, "stopped": stop,
                "out_dir": None if use_src else out_dir}


def make_window():
    window = webview.create_window(
        "Amu Music 音频转换器" + ("（积分版）" if IS_POINTS else ""),
        res_path("web", "index.html"),
        js_api=Api(),
        width=1100, height=680,
        min_size=(940, 600),
        background_color="#FFFFFF",
    )
    return window


def main():
    global _WIN
    _CONV.done_callback = _on_all_done
    if IS_POINTS:
        _CONV.on_converted = _billing_consume  # 转换成功后扣积分
        amu_gate.startup()          # 启动门：本地令牌验活 / 72h 离线宽限
        amu_gate.start_reverify()   # 运行期每小时重验
    _start_audio_server()  # 应用内播放器的本地音频流服务
    _WIN = make_window()
    icon_name = "amu_icon.icns" if sys.platform == "darwin" else "amu_icon.ico"
    icon = res_path("assets", icon_name)
    if not os.path.isfile(icon):
        icon = None  # 打包遗漏时回退到 exe 自带图标
    # 可选远程调试端口（CDP）：设置环境变量 AMU_DEBUG_PORT=9223 时开启，
    # 用于自动化端到端测试；默认关闭，不影响正常使用
    dbg_port = os.environ.get("AMU_DEBUG_PORT")
    if dbg_port and dbg_port.isdigit():
        webview.settings["REMOTE_DEBUGGING_PORT"] = int(dbg_port)
    webview.start(debug=not IS_RUNNING, icon=icon,
                  private_mode=False)  # 关闭隐私模式，主题等偏好才能存入 localStorage


if __name__ == "__main__":
    main()
