# -*- coding: utf-8 -*-
"""积分版运行门：启动验活 / 72h 断网宽限 / 运行期每小时重验 / 按次扣积分。

设计对齐伴奏分离器已上线方案：
- 令牌体系 = 会员站 local_tokens（amu_login_gate 负责 DPAPI 加密落盘）
- 扣分走 POST /api/consume-point，nonce 幂等防超时重试重复扣
- 扣分时机由调用方决定（本软件：每个音频转换成功后扣，失败不扣）
"""
import json
import os
import time
import threading
import uuid

import amu_login_gate

# ── 计费参数（改这里即全网调价）──
COST_PER_FILE = 10            # 每个音频转换成功消耗积分
CONSUME_TASK = 'converter'    # 积分流水记账类型（会员站可见）

# ── 宽限与重验 ──
GATE_REVERIFY_SECS = 3600     # 运行期每小时重验一次
GATE_GRACE = 72 * 3600        # 断网宽限 72h（以最近一次验活成功计）

GATE_CACHE = os.path.join(amu_login_gate._config_dir(), ".amu_gate_cache.json")

ST = {"ok": False, "user": None, "reason": ""}  # reason: "" | "offline" | "nologin"


def _cache_read():
    try:
        with open(GATE_CACHE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _cache_write(user):
    try:
        os.makedirs(os.path.dirname(GATE_CACHE), exist_ok=True)
        with open(GATE_CACHE, "w", encoding="utf-8") as f:
            json.dump({"t": time.time(), "user": user}, f, ensure_ascii=False)
    except Exception:
        pass


def _verify_token(tok):
    """验一个令牌。返回 (code, data, user)。code=0 表示网络不通。"""
    code, data = amu_login_gate._post("/api/auth/desktop-verify", {"token": tok})
    user = (data.get("user") or {}) if (code == 200 and data.get("ok")) else None
    return code, data, user


def unlock(user):
    ST["ok"] = True
    ST["user"] = user or {}
    ST["reason"] = ""
    _cache_write(ST["user"])


def lock(reason=""):
    ST["ok"] = False
    ST["user"] = None
    ST["reason"] = reason


def startup():
    """启动门：本地令牌验活通过→解锁；网络不通且 72h 内验活过→离线宽限。"""
    tok = amu_login_gate.load_token()
    if not tok:
        lock()
        return
    code, data, user = _verify_token(tok)
    if user is not None:
        unlock(user)
    elif code == 0:  # 连不上验证服务器 → 离线宽限判定
        c = _cache_read()
        if c.get("t") and time.time() - c["t"] < GATE_GRACE:
            unlock(c.get("user") or {})
            ST["reason"] = "grace"
        else:
            lock("offline")
    else:  # 令牌被明确拒绝（过期/吊销/封禁）
        amu_login_gate.clear_token()
        lock()


def reverify_loop():
    """运行期定时重验（守护线程）：只有服务端明确拒绝才落锁，网络抖动不误伤。"""
    while True:
        time.sleep(GATE_REVERIFY_SECS)
        if not ST["ok"]:
            continue
        tok = amu_login_gate.load_token()
        if not tok:
            lock()
            continue
        code, data, user = _verify_token(tok)
        if user is not None:
            ST["user"] = user
            _cache_write(user)
        elif code != 0:
            amu_login_gate.clear_token()
            lock()


def start_reverify():
    threading.Thread(target=reverify_loop, daemon=True).start()


def login(email, password):
    """密码登录。返回 (ok, error)。成功后自动解锁。"""
    gate = amu_login_gate.AmuLoginGate()
    ok, err = gate.login(email, password)
    if ok:
        unlock(gate.last_user)
    return ok, err


def logout():
    amu_login_gate.clear_token()
    lock()


def pub_user():
    """给前端的用户信息（不含敏感字段）。未登录返回 None。"""
    if not ST["ok"]:
        return None
    u = ST.get("user") or {}
    return {"nickname": u.get("nickname"), "email": u.get("email"),
            "points": u.get("points")}


def consume(note="", cost=None):
    """转换成功后扣积分。返回 (ok, http_code, data)。
    - 扣分成功 → 同步刷新本地余额（界面实时可见）
    - 402 积分不足 / 令牌失效 → 调用方应判该任务失败（不保存输出）
    - 网络不通 → 同样拒绝（按次付费工具不允许离线白嫖）
    """
    tok = amu_login_gate.load_token()
    if not tok:
        return False, 401, {"error": "登录状态已失效，请重新登录后再使用"}
    cost = COST_PER_FILE if cost is None else int(cost)
    body = {"token": tok, "task": CONSUME_TASK, "cost": cost,
            "nonce": uuid.uuid4().hex}  # nonce 防超时重试重复扣
    if note:
        body["note"] = note[:120]
    try:
        code, data = amu_login_gate._post("/api/consume-point", body)
    except Exception:
        return False, 0, {"error": "暂时连不上积分服务器，请检查网络后重试（本版本按次计费，需联网扣积分）"}
    if code == 200 and data.get("ok"):
        if isinstance(ST.get("user"), dict):
            ST["user"]["points"] = data.get("balance")
            _cache_write(ST["user"])
        return True, code, data
    if not isinstance(data, dict):
        data = {}
    if not data.get("error"):
        if code == 0:
            data["error"] = "连不上积分服务器，请检查网络后重试（按次计费需联网扣积分）"
        elif code in (500, 502, 503, 504):
            data["error"] = "积分服务器暂时异常（%s），请稍后重试；已扣的分不会重复扣" % code
        else:
            data["error"] = "扣积分未完成（服务器返回 %s），请稍后重试" % code
    if code == 402:
        bal = data.get("balance")
        data["error"] = ("积分不足（当前余额 %s）：每转换 1 个音频消耗 %d 积分，"
                         "可到官网每日签到或充值获取积分" % (bal if bal is not None else "?", cost))
    data["code"] = code
    return False, code, data
