# -*- coding: utf-8 -*-
"""会话自动续期：服务端拒绝当前 token 时自动重新登录并重发一次。

设计边界（与项目既有约定一致，改动前请先读这段）：

1. **判定点在业务 code 层，不在 HTTP 状态码。** 实测该校服务端用
   `HTTP 200` + 明文 `{"msg":"请求访问：/run/getHomeRunInfo，认证失败，无法访问系统资源",
   "code":401}` 表示 token 失效——HTTP 层根本没有 401。因此续期逻辑挂在
   `YunClient.post()`（`yun_http.YunClient._auth_failed`）而不是传输层包装器上。
2. **认证失败发生在业务逻辑之前（请求没有被处理、没有写任何数据）**，所以
   "换个新 token 重发一次"是安全的——这与项目明令禁止的"超时后自动重发"不是同一件事：
   后者结果未知、可能已经入账。传输层失败（TransportOutcomeUnknown）**绝不重试**。
3. **只有"带着 token 却被拒"才触发重登**。登录请求自身不带 token，若它被拒说明是
   凭据/学校配置问题——重试没有意义且可能撞账号锁定，一律不重试。
4. 每次请求**最多续期重试一次**，且必须确认 token 确实换了新值，否则不再发第二次。
5. 非认证失败的响应（业务 code、HTTP 状态）全部原样交给调用方，不改语义。

凭据取值优先级（用户指定）：
    config.ini 里写死了 → 用配置值
    否则               → 用"上次登录实际用过"的值（内存记住，可持久化）
    都没有             → 配置内兜底（Xiaomi / 14），并明确打印，不静默猜测
"""
from __future__ import annotations

import time
from typing import Any, Callable, Dict, Optional

import requests


# ---------------------------------------------------------------- 身份取值
DEFAULT_DEVICE_NAME = "Xiaomi"   # 与 tools/Login.py 的交互默认值一致
DEFAULT_SYS_EDITION = "14"       # 同上


def resolve_identity(conf, last_used: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """解析本次登录要用的设备身份。

    优先级：config.ini 写死的值 → 上次实际用过的值 → 留空（由 identity_for_login 兜底）。
    sys_version 优先于 sys_edition（与 yun_http/Login 的语义一致）。
    返回值附 used_for: 说明每项来源（便于日志与测试断言）。
    """
    last_used = last_used or {}
    out: Dict[str, str] = {}
    source: Dict[str, str] = {}

    for key in ("device_name", "sys_edition", "sys_version", "device_id", "uuid"):
        value = (conf.get("User", key, fallback="") or "").strip()
        if value:
            out[key] = value
            source[key] = "config"
            continue
        carried = str(last_used.get(key, "") or "").strip()
        if carried:
            out[key] = carried
            source[key] = "last_used"
    out["used_for"] = ",".join(f"{k}={source.get(k, 'none')}" for k in source)
    return out


def identity_for_login(conf, last_used: Optional[Dict[str, str]] = None,
                       log: Optional[Callable[[str], None]] = None) -> Dict[str, str]:
    """同上，但补齐兜底值，保证自动重登永远不需要交互式输入。

    used_for 会补记 resolved/filled，便于日志核对"这个身份到底从哪来的"。
    """
    ident = resolve_identity(conf, last_used)
    resolved, filled = [], []
    if not ident.get("device_name"):
        carried = str((last_used or {}).get("device_name") or "").strip()
        ident["device_name"] = carried or DEFAULT_DEVICE_NAME
        (resolved if carried else filled).append(f"device_name={ident['device_name']}")
    if not ident.get("sys_edition") and not ident.get("sys_version"):
        carried = str((last_used or {}).get("sys_edition") or "").strip()
        ident["sys_edition"] = carried or DEFAULT_SYS_EDITION
        (resolved if carried else filled).append(f"sys_edition={ident['sys_edition']}")
    used = ident.get("used_for", "")
    extra = ",".join(f"{k.split('=')[0]}={'resolved' if k in resolved else 'filled'}"
                     for k in resolved + filled)
    ident["used_for"] = ",".join(x for x in (used, extra) if x)
    if filled and log:
        log(f"[续期] 设备身份缺省，使用配置内兜底值（自动重登不做交互输入）：{', '.join(filled)}")
    if resolved and log:
        log(f"[续期] 设备身份未写死，沿用上次使用值：{', '.join(resolved)}")
    return ident


# ---------------------------------------------------------------- 续期策略（注入 YunClient）
class ReauthPolicy:
    """token 被拒时的续期策略，作为 `recover` 回调注入 `yun_http.YunClient`。

    `recover` 由调用方提供（main.recover_token）：完成重新登录并把新 token 同步到
    内存客户端，成功返回新 token，失败返回 None。本类负责"要不要重试"的判定与日志，
    真正的重发由 YunClient.post 执行（同一个 SM4 key、不换加密上下文）。
    """

    def __init__(self, recover: Callable[[], Optional[str]],
                 current_token: Callable[[], str],
                 log: Optional[Callable[[str], None]] = None):
        self._recover = recover
        self._current_token = current_token
        self._log = log or (lambda s: None)
        self.calls = 0

    def __call__(self) -> Optional[str]:
        self.calls += 1
        before = str(self._current_token() or "")
        token = self._recover()
        if not token:
            return None
        if str(token) == before:
            self._log("[续期] 重登返回的 token 与旧值相同：不重发，避免重复提交")
            return None
        return token


# ---------------------------------------------------------------- 登录重试策略
# 只对这些异常重试：它们是"没问到"，不是"问到了被拒绝"
RETRYABLE_LOGIN_ERRORS = (
    requests.exceptions.ConnectionError,
    requests.exceptions.Timeout,
)


def retryable_login_exception(exc: BaseException) -> bool:
    """登录失败是否值得重试：仅传输层（连接失败/超时）值得，凭据/业务失败一律不重试。"""
    return isinstance(exc, RETRYABLE_LOGIN_ERRORS)


def login_with_retries(login: Callable[[], Any],
                       attempts: int = 2, delay: float = 3.0,
                       sleep: Callable[[float], None] = time.sleep,
                       log: Optional[Callable[[str], None]] = None) -> Any:
    """登录重试：只对传输层失败重试，凭据错误/业务拒绝立即返回。

    login() 的既有约定：成功返回结果元组，失败返回 None（Login.main 的语义）。
    - 返回 None（凭据错、学校配置缺失等确定性失败）→ 不重试，立即返回。
    - 抛传输层异常且还有次数 → 等待 delay 后重试。
    - 抛非传输层异常 → 原样抛出，不重试。
    """
    log = log or (lambda s: None)
    total = max(1, int(attempts))
    last_exc: Optional[BaseException] = None
    for i in range(1, total + 1):
        try:
            result = login()
        except RETRYABLE_LOGIN_ERRORS as exc:
            last_exc = exc
            if i < total:
                log(f"[登录] 第 {i}/{total} 次失败（传输层，结果未知不重发业务请求）："
                    f"{type(exc).__name__}: {exc}；{delay:g}s 后重试登录")
                sleep(delay)
                continue
            log(f"[登录] 已重试 {total} 次仍失败：{type(exc).__name__}: {exc}")
            raise
        if result is None:
            log("[登录] 凭据/配置被服务端拒绝（返回 None）：不重试（重试可能触发锁定/验证码）")
            return None
        if i > 1:
            log(f"[登录] 第 {i} 次尝试成功")
        return result
    raise last_exc if last_exc else RuntimeError("登录重试异常退出")

