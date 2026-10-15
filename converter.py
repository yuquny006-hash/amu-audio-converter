# -*- coding: utf-8 -*-
"""Amu Music 音频转换器 - 转换内核（ffprobe 探测 + ffmpeg 并发转换）"""
import os
import re
import sys
import json
import time
import shutil
import array
import subprocess
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import ctypes
from platform_utils import subprocess_options


def _find_tool(name):
    """查找顺序：PATH → 软件目录(开发时) → exe 所在目录(打包后)"""
    p = shutil.which(name)
    if p:
        return p
    here = os.path.dirname(os.path.abspath(__file__))
    exe_dir = os.path.dirname(os.path.abspath(sys.executable))
    bundle_dir = getattr(sys, "_MEIPASS", "")
    suffixes = [".exe"] if sys.platform == "win32" else [""]
    for base in dict.fromkeys([here, exe_dir, bundle_dir]):  # 去重保序
        if not base:
            continue
        for suffix in suffixes:
            local = os.path.join(base, name + suffix)
            if os.path.exists(local):
                return local
    return name


FFMPEG = _find_tool("ffmpeg")
FFPROBE = _find_tool("ffprobe")

# ---------------- 输出格式定义 ----------------
# kind: lossy = 用 -b:a 比特率; lossless = 忽略比特率
# codec=None 表示交给 ffmpeg 按扩展名选
FORMATS = {
    "MP3":            {"ext": "mp3",  "kind": "lossy",     "acodec": "libmp3lame"},
    "AAC":            {"ext": "aac",  "kind": "lossy",     "acodec": "aac"},
    "FLAC":           {"ext": "flac", "kind": "lossless",  "compression": "5"},
    "WAV":            {"ext": "wav",  "kind": "lossless"},
    "M4A":            {"ext": "m4a",  "kind": "lossy",     "acodec": "aac"},
    "OGG Vorbis":     {"ext": "ogg",  "kind": "lossy",     "acodec": "libvorbis"},
    "Opus":           {"ext": "opus", "kind": "lossy",     "acodec": "libopus", "force_rate": "48000", "force_ch": "2"},
    "ALAC":           {"ext": "m4a",  "kind": "lossless",  "acodec": "alac"},
    "WMA":            {"ext": "wma",  "kind": "lossy",     "acodec": "wmav2"},
    "AC3 (Dolby)":    {"ext": "ac3",  "kind": "lossy",     "acodec": "ac3"},
    "AIFF":           {"ext": "aiff", "kind": "lossless"},
    "E-AC3 (Dolby+)": {"ext": "eac3", "kind": "lossy",     "acodec": "eac3"},
    "DTS":            {"ext": "dts",  "kind": "lossy",     "acodec": "dca", "strict": "-2"},
    "MP2":            {"ext": "mp2",  "kind": "lossy",     "acodec": "mp2"},
    "WavPack":        {"ext": "wv",   "kind": "lossless",  "acodec": "wavpack",
                       "force_filter": "asetnsamples=1152"},
    "TTA":            {"ext": "tta",  "kind": "lossless",  "acodec": "tta"},
    "aptX":           {"ext": "aptx", "kind": "lossy",     "acodec": "aptx", "force_ch": "2"},
    "SBC":            {"ext": "sbc",  "kind": "lossy",     "acodec": "sbc"},
    "TrueHD":         {"ext": "thd",  "kind": "lossless",  "acodec": "truehd", "strict": "-2"},
    "MLP":            {"ext": "thd",  "kind": "lossless",  "acodec": "truehd", "strict": "-2"},
}

BITRATES = {"自动": None, "320 kbps": "320k", "256 kbps": "256k", "224 kbps": "224k",
            "192 kbps": "192k", "160 kbps": "160k", "128 kbps": "128k",
            "96 kbps": "96k", "64 kbps": "64k", "32 kbps": "32k"}
SAMPLE_RATES = {"自动": None, "48000 Hz": "48000", "44100 Hz": "44100",
                "32000 Hz": "32000", "24000 Hz": "24000", "22050 Hz": "22050",
                "16000 Hz": "16000", "12000 Hz": "12000", "11025 Hz": "11025",
                "8000 Hz": "8000"}
CHANNELS = {"自动": None, "立体声": "2", "单声道": "1"}

# 各编码器"自动"比特率
AUTO_BITRATE = {"libmp3lame": "192k", "aac": "192k", "libvorbis": "160k",
                "libopus": "128k", "wmav2": "128k", "ac3": "640k",
                "eac3": "640k", "dca": "1536k", "mp2": "384k",
                "aptx": "384k", "sbc": "345k"}

AUDIO_EXT = {".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".opus",
             ".wma", ".aiff", ".aif", ".ape", ".wv", ".tta", ".ac3",
             ".dts", ".amr", ".g711", ".au", ".mka", ".m4b", ".caf"}


def probe_file(path, log_err=True):
    """用 ffprobe 读取文件元数据。
    注意：ffprobe JSON 输出是 UTF-8，必须显式 encoding（默认 GBK 会因
    中文/emoji 标签解码崩溃）；失败时写入 %TEMP%/amu_conv.log 便于诊断。"""
    cmd = [FFPROBE, "-v", "error", "-print_format", "json",
           "-show_format", "-show_streams", path]
    try:
        out = subprocess.run(cmd, capture_output=True, encoding="utf-8",
                             errors="replace", timeout=20,
                             **subprocess_options()).stdout
        info = json.loads(out)
    except Exception as e:
        if log_err:
            _probe_log(path, "ffprobe 异常: %r" % e)
        return None
    fmt = info.get("format", {})
    astream = None
    for s in info.get("streams", []):
        if s.get("codec_type") == "audio":
            astream = s
            break
    if astream is None:
        if log_err:
            _probe_log(path, "ffprobe 无音频流 (rc输出见上) size=%s" %
                       os.path.getsize(path) if os.path.exists(path) else "?")
        return None

    def fmt_int(v):
        try:
            n = int(v)
            return n
        except (TypeError, ValueError):
            return None

    duration = None
    try:
        duration = float(fmt.get("duration") or astream.get("duration") or 0) or None
    except (TypeError, ValueError):
        pass

    ext = os.path.splitext(path)[1].lstrip(".").upper()
    name_map = {"M4A": "M4A", "AIF": "AIFF"}
    tags = fmt.get("tags") or {}
    return {
        "format": name_map.get(ext, ext or (astream.get("codec_name") or "?").upper()),
        "size": fmt_int(fmt.get("size")),
        "duration": duration,
        "bitrate": fmt_int(fmt.get("bit_rate") or astream.get("bit_rate")),
        "sample_rate": fmt_int(astream.get("sample_rate")),
        "channels": astream.get("channels"),
        "codec": (astream.get("codec_name") or "").upper(),
        "title": tags.get("title") or "",
        "artist": tags.get("artist") or "",
    }


def _probe_log(path, msg):
    """探测失败记录到日志文件（%TEMP%/amu_conv.log），不弹窗不打断"""
    try:
        with open(os.path.join(_tempdir(), "amu_conv.log"), "a",
                  encoding="utf-8") as f:
            f.write("[%s] %s | %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"),
                                        msg, path))
    except Exception:
        pass


def _tempdir():
    import tempfile
    return tempfile.gettempdir()


# ---------- 波形峰值（应用内播放器的进度条用） ----------
_WF_CACHE = {}          # (path, mtime) -> [0~1 峰值列表]
_WF_CACHE_ORDER = []    # 简易 FIFO，防止缓存无限增长


def compute_waveform(path, buckets=240):
    """用 ffmpeg 把音频解码成 8kHz 单声道 s16le PCM，按桶取峰值。
    返回长度 <= buckets 的 0~1 浮点列表；失败返回空列表。
    8kHz 解码速度极快（10 分钟歌曲约 1 秒内），且不落盘。"""
    try:
        key = (path, os.path.getmtime(path))
    except OSError:
        return []
    if key in _WF_CACHE:
        return _WF_CACHE[key]

    cmd = [FFMPEG, "-v", "error", "-i", path,
           "-ac", "1", "-ar", "8000", "-f", "s16le", "pipe:1"]
    try:
        raw = subprocess.run(cmd, capture_output=True, timeout=60,
                             **subprocess_options()).stdout
    except Exception as e:
        _probe_log(path, "waveform 异常: %r" % e)
        return []

    n = len(raw) // 2
    if n < 8:
        return []
    samples = array.array("h", raw[:n * 2])
    if sys.byteorder != "little":
        samples.byteswap()

    step = max(1, n // buckets)
    peaks = []
    for i in range(0, n - step + 1, step):
        chunk = samples[i:i + step]
        lo, hi = min(chunk), max(chunk)
        peaks.append(min(1.0, (hi - lo) / 65536.0))
    # 归一化：安静的歌也能看出波形起伏（除以最大峰值，下限防除零）
    mx = max(peaks) or 1.0
    if mx < 1.0:
        peaks = [min(1.0, p / mx) for p in peaks]

    # 缓存管理：最多 60 条，先进先出
    while len(_WF_CACHE_ORDER) >= 60:
        old = _WF_CACHE_ORDER.pop(0)
        _WF_CACHE.pop(old, None)
    _WF_CACHE_ORDER.append(key)
    _WF_CACHE[key] = peaks
    return peaks


def _atempo_chain(speed):
    """把任意速度拆成 0.5~2.0 的 atempo 链"""
    chain, remain = [], float(speed)
    while remain > 2.0:
        chain.append("atempo=2.0")
        remain /= 2.0
    while remain < 0.5:
        chain.append("atempo=0.5")
        remain /= 0.5
    chain.append("atempo=%.4f" % remain)
    return ",".join(chain)


class Converter:
    """任务表 + 线程池调度"""

    def __init__(self):
        self.tasks = {}            # id -> task dict
        self.order = []            # 保持列表顺序
        self.lock = threading.RLock()
        self.pool = None
        self.running = False
        self.stop_flag = threading.Event()
        self.settings = {
            "format": "MP3",
            "bitrate": "自动",
            "sample_rate": "自动",
            "channels": "自动",
            "volume": 100,        # 百分比
            "speed": 1.00,
            "output_dir": os.path.join(os.path.expanduser("~"), "Desktop", "output_amu"),
            "use_source_dir": False,
            "notify_done": True,
            "auto_open_folder": True,
            "workers": "自动",
        }
        self.done_callback = None  # 全部完成时回调（用于通知）
        # 计费钩子（积分版用）：每个音频转换成功后回调 on_converted(path)。
        # 回调抛异常 → 本次任务判失败并删除输出文件（扣分失败不给输出）。
        # 无限制版保持 None，行为与 v1.0.0 一致。
        self.on_converted = None

    # ---------- 列表管理 ----------
    def add_paths(self, paths):
        added = 0
        for p in paths:
            p = os.path.normpath(p)
            if os.path.isdir(p):
                for root, _dirs, files in os.walk(p):
                    for f in sorted(files):
                        if os.path.splitext(f)[1].lower() in AUDIO_EXT:
                            added += self._add_one(os.path.join(root, f))
            elif os.path.isfile(p):
                added += self._add_one(p)
        return added

    def _add_one(self, path):
        with self.lock:
            if any(t["path"] == path for t in self.tasks.values()):
                return 0
            tid = uuid.uuid4().hex[:12]
            meta = probe_file(path)
            if meta is None:
                # ffprobe 失败时的兜底：至少显示格式与大小，其余列待转换成功后补探
                ext = os.path.splitext(path)[1].lstrip(".").upper()
                try:
                    size = os.path.getsize(path)
                except OSError:
                    size = None
                meta = {"format": ext or "?", "size": size,
                        "duration": None, "bitrate": None,
                        "sample_rate": None, "channels": None,
                        "codec": "", "title": "", "artist": ""}
            task = {"id": tid, "path": path, "name": os.path.basename(path),
                    "status": "waiting", "progress": 0, "speed_text": "",
                    "error": "", "meta": meta}
            self.tasks[tid] = task
            self.order.append(tid)
            return 1

    def remove(self, tid):
        with self.lock:
            t = self.tasks.get(tid)
            if t and t["status"] == "converting":
                return False
            if t:
                del self.tasks[tid]
                self.order.remove(tid)
            return True

    def clear(self):
        with self.lock:
            if any(t["status"] == "converting" for t in self.tasks.values()):
                return False
            self.tasks.clear()
            self.order.clear()
            return True

    def retry(self, tid):
        with self.lock:
            t = self.tasks.get(tid)
            if t and t["status"] in ("failed", "stopped", "success"):
                t.update(status="waiting", progress=0, speed_text="", error="")
                return True
            return False

    # ---------- 设置 ----------
    def update_settings(self, kv):
        with self.lock:
            for k, v in kv.items():
                if k in self.settings:
                    self.settings[k] = v

    # ---------- 转换 ----------
    def resolve_workers(self):
        w = self.settings["workers"]
        if w == "自动":
            return max(1, min(8, os.cpu_count() or 4))
        try:
            return max(1, min(8, int(w)))
        except (TypeError, ValueError):
            return 3

    def start(self):
        with self.lock:
            if self.running:
                return False
            queue = [tid for tid in self.order
                     if self.tasks[tid]["status"] in ("waiting",)]
            if not queue:
                return False
            self.running = True
            self.stop_flag.clear()
        self.pool = ThreadPoolExecutor(max_workers=self.resolve_workers())
        for tid in queue:
            self.pool.submit(self._worker, tid)
        threading.Thread(target=self._watch_done, daemon=True).start()
        return True

    def stop(self):
        self.stop_flag.set()

    def _watch_done(self):
        while True:
            with self.lock:
                converting = [t for t in self.tasks.values()
                              if t["status"] == "converting"]
                waiting = [t for t in self.tasks.values()
                           if t["status"] == "waiting"]
            if not converting and (not waiting or self.stop_flag.is_set()):
                break
            time.sleep(0.3)
        with self.lock:
            self.running = False
            finished_ids = [t["id"] for t in self.tasks.values()
                            if t["status"] in ("success", "failed", "stopped")]
        if self.done_callback:
            self.done_callback(finished_ids)

    def _worker(self, tid):
        if self.stop_flag.is_set():
            with self.lock:
                t = self.tasks.get(tid)
                if t and t["status"] == "waiting":
                    t["status"] = "stopped"
            return
        with self.lock:
            t = self.tasks.get(tid)
            if not t:
                return
            t["status"] = "converting"
            t["progress"] = 0
            t["error"] = ""
            s = dict(self.settings)
        try:
            self._convert_one(t, s)
        except StopConverter:
            with self.lock:
                t["status"] = "stopped"
        except Exception as e:
            with self.lock:
                t["status"] = "failed"
                t["error"] = str(e)[:200]

    def _output_path(self, src, s):
        fmt = FORMATS[s["format"]]
        base = os.path.splitext(os.path.basename(src))[0] + "." + fmt["ext"]
        out_dir = src if s["use_source_dir"] else s["output_dir"]
        os.makedirs(out_dir, exist_ok=True)
        out = os.path.join(out_dir, base)
        if os.path.abspath(out) == os.path.abspath(src):
            base = os.path.splitext(os.path.basename(src))[0] + "_转换." + fmt["ext"]
            out = os.path.join(out_dir, base)
        n = 1
        while os.path.exists(out):
            stem = os.path.splitext(os.path.basename(src))[0]
            out = os.path.join(out_dir, "%s(%d).%s" % (stem, n, fmt["ext"]))
            n += 1
        return out

    def _convert_one(self, t, s):
        src = t["path"]
        out = self._output_path(src, s)
        fmt = FORMATS[s["format"]]

        cmd = [FFMPEG, "-y", "-nostdin", "-i", src]
        if fmt.get("strict"):
            cmd += ["-strict", fmt["strict"]]
        # 速度/音量滤镜
        filters = []
        if fmt.get("force_filter"):
            filters.append(fmt["force_filter"])
        if abs(float(s["speed"]) - 1.0) > 1e-4:
            filters.append(_atempo_chain(s["speed"]))
        vol = float(s["volume"]) / 100.0
        if abs(vol - 1.0) > 1e-4:
            filters.append("volume=%.4f" % vol)
        if filters:
            cmd += ["-af", ",".join(filters)]
        # 声道 / 采样率（格式强制值优先级最低，用户选择优先）
        ch = CHANNELS.get(s["channels"]) or fmt.get("force_ch")
        if ch:
            cmd += ["-ac", ch]
        rate = SAMPLE_RATES.get(s["sample_rate"]) or fmt.get("force_rate")
        if rate:
            cmd += ["-ar", rate]
        # 编码器与比特率
        if fmt["kind"] == "lossy":
            cmd += ["-c:a", fmt["acodec"]]
            br = BITRATES.get(s["bitrate"])
            if br is None:  # 自动
                br = AUTO_BITRATE.get(fmt["acodec"])
            if br:
                cmd += ["-b:a", br]
        else:
            if fmt.get("acodec"):
                cmd += ["-c:a", fmt["acodec"]]
            if fmt.get("compression"):
                cmd += ["-compression_level", fmt["compression"]]
        cmd += ["-vn", "-map_metadata", "0", "-map", "0:a:0?", "-id3v2_version", "3"]

        duration = (t.get("meta") or {}).get("duration")
        cmd += ["-progress", "pipe:1", "-nostats", out]

        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, encoding="utf-8", errors="replace",
                                **subprocess_options())
        t["_proc"] = proc
        last_progress = 0
        for line in proc.stdout:
            if self.stop_flag.is_set():
                try:
                    proc.kill()
                except OSError:
                    pass
                raise StopConverter()
            line = line.strip()
            if line.startswith("out_time_us=") and duration:
                try:
                    us = int(line.split("=")[1])
                    pct = min(99, int(us / 1e6 / duration * 100))
                    if pct != last_progress:
                        with self.lock:
                            t["progress"] = pct
                        last_progress = pct
                except ValueError:
                    pass
            elif line.startswith("speed=") :
                with self.lock:
                    t["speed_text"] = line.split("=")[1].strip()
        proc.wait()
        t.pop("_proc", None)
        err = (proc.stderr.read() or "") if proc.stderr else ""
        if proc.returncode != 0:
            m = re.search(r"(Error|Invalid|Unknown|Unable|could not|Conversion failed)[^\r\n]*",
                          err, re.IGNORECASE)
            raise RuntimeError(m.group(0) if m else "ffmpeg 退出码 %d" % proc.returncode)
        if not os.path.exists(out) or os.path.getsize(out) == 0:
            raise RuntimeError("输出文件未生成")
        # 计费钩子：转换成功后扣积分；扣分失败（积分不足/断网）删除输出判失败
        if self.on_converted:
            try:
                self.on_converted(src)
            except Exception:
                try:
                    os.remove(out)
                except OSError:
                    pass
                raise
        with self.lock:
            t["status"] = "success"
            t["progress"] = 100
            t["speed_text"] = ""
            t["out_path"] = out
            # 补探元数据：添加时 ffprobe 失败（文件被占用/空文件/编码问题）的话，
            # 转换成功说明源文件此时可读，趁此机会把信息补全
            m = t.get("meta") or {}
            if not m.get("duration") or not m.get("sample_rate"):
                re_meta = probe_file(src, log_err=False)
                if re_meta:
                    t["meta"] = re_meta


class StopConverter(Exception):
    pass
