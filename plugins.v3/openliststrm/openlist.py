"""OpenList HTTP API 客户端（只读）。

契约来源：OpenListTeam/OpenList 官方后端源码
  - 路由：server/router.go
      api.POST("/auth/login", handles.Login)          # 账号密码
      api.POST("/auth/login/hash", handles.LoginHash) # 已哈希密码
      fsAndShare: g.Any("/list", handles.FsListSplit)
                  g.Any("/get",  handles.FsGetSplit)
      g.GET("/d/*path", ..., handles.Down)            # 直链，无需登录
  - 请求体：server/handles/fsread.go  ListReq{PageReq, Path, Password, Refresh}
  - 分页：internal/model/req.go  PageReq.Validate(): per_page < 1 -> MaxInt
      => 传 per_page=0 即一次返回目录全部条目，无需翻页
  - 认证：server/middlewares/auth.go
      token := c.GetHeader("Authorization")  # 直接放 token 原文，不加 "Bearer "
      空 token 会退化为 guest（guest 被禁用时返回 401）
  - 响应：{ "code": 200, "message": "success", "data": {...} }

本模块不依赖 MoviePilot 宿主，便于单元测试；HTTP 传输通过 `transport` 注入。
"""

from __future__ import annotations

import time
from typing import Any, Callable, NamedTuple, Optional

from .strmutil import iter_entries, response_ok

# 传输层签名：
#   transport(method, url, *, json=None, headers=None) -> (status_code, body_dict|None)
# 为兼容旧的二元组返回值，内部统一用 `_unpack_response` 归一化。
Transport = Callable[..., Any]

# 目录条目数量上限保护，避免异常目录（或递归环路）导致内存膨胀
DEFAULT_MAX_ENTRIES = 200000

# 瞬时错误的重试次数与退避基数（秒）
DEFAULT_RETRY_ATTEMPTS = 3
RETRY_BACKOFF = 0.8

# OpenList 对「路径不存在」返回 code=500 而非 404，需按 message 识别
_NOT_FOUND_HINTS = (
    "object not found",
    "not found",
    "no such file",
)


class HttpResult(NamedTuple):
    """一次 HTTP 调用的归一化结果。"""

    status: int
    body: Optional[dict]
    raw: str = ""


def _unpack_response(result: Any) -> HttpResult:
    """把传输层返回值归一化成 HttpResult。

    同时接受 `(status, body)` 与 `(status, body, raw)` 两种形状，
    这样注入的自定义传输实现不必跟着升级。
    """
    if isinstance(result, HttpResult):
        return result
    if isinstance(result, tuple):
        if len(result) >= 3:
            return HttpResult(int(result[0] or 0), result[1], str(result[2] or ""))
        if len(result) == 2:
            return HttpResult(int(result[0] or 0), result[1], "")
    return HttpResult(0, None, "")


def _is_not_found(message: str) -> bool:
    """判断错误信息是否表示「路径/对象不存在」。"""
    lowered = str(message or "").lower()
    return any(hint in lowered for hint in _NOT_FOUND_HINTS)


def _is_transient_status(status: int) -> bool:
    """判断 HTTP 状态码是否属于「重试可能成功」的瞬时错误。

    - 0：连接层失败（超时、对端重置）
    - 429：限流
    - 5xx：服务端临时故障

    实测：上游网盘（如 115）超时时 OpenList 会返回 **554** 且响应体为空，
    这类错误重试往往就能成功，不应判定为永久失败。
    """
    if status == 0:
        return True
    if status == 429:
        return True
    return status >= 500


class OpenListError(Exception):
    """OpenList 调用失败，携带可读原因，供上层写日志。"""

    def __init__(self, message: str, *, transient: bool = False) -> None:
        super().__init__(message)
        # 标记是否为「可重试」的瞬时错误，供调用方决定重试或跳过
        self.transient = bool(transient)


class OpenListClient:
    """封装登录、列目录、取直链的最小只读客户端。"""

    def __init__(
        self,
        base_url: str,
        token: str = "",
        username: str = "",
        password: str = "",
        otp_code: str = "",
        transport: Optional[Transport] = None,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        retry_attempts: int = DEFAULT_RETRY_ATTEMPTS,
    ) -> None:
        self.base_url = str(base_url or "").strip().rstrip("/")
        self._token = str(token or "").strip()
        self._username = str(username or "").strip()
        self._password = str(password or "")
        self._otp_code = str(otp_code or "").strip()
        self._transport = transport
        self._max_entries = max(1, int(max_entries))
        self._retry_attempts = max(1, int(retry_attempts))

    # ------------------------------------------------------------------ 内部
    def _once(self, method: str, path: str, payload: Optional[dict]) -> HttpResult:
        """发起一次请求并归一化结果（不抛异常）。"""
        if not self.base_url:
            raise OpenListError("未配置 OpenList 地址")
        if self._transport is None:
            raise OpenListError("未注入 HTTP 传输实现")
        url = f"{self.base_url}{path}"
        headers = {"Content-Type": "application/json"}
        if self._token:
            # OpenList 要求 token 原文；带 "Bearer " 前缀会导致 JWT 解析失败
            headers["Authorization"] = self._token
        try:
            return _unpack_response(
                self._transport(method, url, json=payload, headers=headers)
            )
        except Exception as err:  # noqa: BLE001 - 传输层异常统一转成瞬时失败
            return HttpResult(0, None, f"{type(err).__name__}: {err}")

    def _request(self, method: str, path: str, json: Optional[dict] = None,
                 *, retry: bool = True) -> Optional[dict]:
        """发起请求并返回解析后的 JSON。

        失败时抛出 `OpenListError`，并把 HTTP 状态码/响应片段写进信息——
        网盘后端超时时 OpenList 会返回非 JSON 的错误页（如 HTTP 554 空响应），
        只说「非 JSON」无法定位问题。

        :param retry: 是否对瞬时错误（5xx/554/连接失败）自动重试
        """
        attempts = self._retry_attempts if retry else 1
        last: Optional[OpenListError] = None

        for attempt in range(attempts):
            result = self._once(method, path, json)
            if result.status and result.body is not None:
                return result.body

            transient = _is_transient_status(result.status)
            detail = f"HTTP {result.status}" if result.status else "连接失败"
            if result.raw:
                detail += f"，响应片段：{result.raw[:120]}"
            last = OpenListError(
                f"请求失败或响应非 JSON（{detail}）：{method} {path}",
                transient=transient,
            )
            if not transient or attempt == attempts - 1:
                raise last
            time.sleep(RETRY_BACKOFF * (attempt + 1))

        if last is not None:
            raise last
        return None

    def _ensure_token(self) -> None:
        """未提供 token 时用账号密码换取 token。"""
        if self._token:
            return
        if not self._username or not self._password:
            raise OpenListError("未配置 OpenList Token，也未提供用户名/密码")
        payload: dict[str, Any] = {
            "username": self._username,
            "password": self._password,
        }
        if self._otp_code:
            payload["otp_code"] = self._otp_code
        # /api/auth/login 会对明文密码做 StaticHash 后再校验
        body = self._request("POST", "/api/auth/login", json=payload)
        if body is None:
            raise OpenListError("登录请求无响应")
        if not response_ok(body):
            raise OpenListError(f"登录失败：{body.get('message') or body}")
        data = body.get("data") or {}
        token = data.get("token") if isinstance(data, dict) else None
        if not token:
            raise OpenListError("登录响应中未找到 token")
        self._token = str(token).strip()

    # ------------------------------------------------------------------ 公开
    def login(self) -> None:
        """显式登录（幂等）。"""
        self._ensure_token()

    def _request_with_retry(self, method: str, path: str, json: Optional[dict] = None,
                            attempts: int = DEFAULT_RETRY_ATTEMPTS) -> Optional[dict]:
        """兼容旧调用方：重试逻辑现已内建在 `_request` 中。"""
        return self._request(method, path, json=json,
                             retry=attempts > 1)

    def list_dir(self, remote_path: str, password: str = "",
                 allow_relogin: bool = True, tolerate_not_found: bool = True,
                 retry_transient: bool = True) -> list[dict]:
        """列出目录全部条目。

        使用 `per_page=0`：OpenList 的 `PageReq.Validate()` 会把 `<1` 转成 `MaxInt`，
        即一次返回整个目录，避免逐页翻页带来的重复请求。

        :param allow_relogin:    收到 401 时自动重新登录并重试一次
        :param tolerate_not_found: 路径不存在时返回空列表而非抛异常
                                   （OpenList 对不存在的路径返回 code=500 `object not found`）
        :param retry_transient:  对 5xx/554/连接失败等瞬时错误自动重试
        """
        self._ensure_token()
        payload: dict[str, Any] = {
            "path": remote_path or "/",
            "page": 1,
            "per_page": 0,          # 0 => 全部
            "refresh": False,       # 只读遍历必须 false，否则无写权限会 403
            "password": password or "",
        }
        body = self._request("POST", "/api/fs/list", json=payload,
                             retry=retry_transient)
        if body is None:
            raise OpenListError(f"列目录无响应：{remote_path}")

        if not response_ok(body):
            code = body.get("code")
            message = str(body.get("message") or body)

            # token 失效 / 过期 / 服务端重启都会返回 401，重登一次再试
            if str(code) == "401":
                if allow_relogin:
                    self._token = ""
                    self._ensure_token()
                    return self.list_dir(remote_path, password=password,
                                         allow_relogin=False,
                                         tolerate_not_found=tolerate_not_found,
                                         retry_transient=retry_transient)
                raise OpenListError(f"未授权（token 失效或未登录）：{message}")

            # 路径不存在是 OpenList 的常规返回（code=500 + "object not found"），
            # 属正常情况，不应中断整轮扫描
            if tolerate_not_found and _is_not_found(message):
                return []

            # 上游网盘故障时的 code=500（非 not found）也算瞬时错误，
            # 交给调用方跳过该目录而非中止整轮扫描
            transient = str(code) in ("500", "502", "503", "504")
            if str(code) in ("403",):
                raise OpenListError(f"无权限或目录受密码保护：{remote_path} -> {message}")
            raise OpenListError(f"列目录失败：{remote_path} -> {message}",
                                transient=transient)

        entries = iter_entries(body)
        if len(entries) > self._max_entries:
            raise OpenListError(
                f"目录条目数 {len(entries)} 超过上限 {self._max_entries}，请检查：{remote_path}"
            )
        return entries

    def fetch_base_path(self) -> str:
        """读取当前账号的 base_path。

        OpenList 的 `base_path` 只影响 `/api/*`（服务端会用它拼接真实路径），
        不影响 `/d/` 直链。因此用 API 列目录时不需要它，但生成 strm 的 `/d/` 路径
        必须补上该前缀，否则链接会指向错误位置。
        """
        body = self._request("GET", "/api/me")
        if not body or not response_ok(body):
            return "/"
        data = body.get("data")
        if isinstance(data, dict):
            return str(data.get("base_path") or "/") or "/"
        return "/"

    def get_file(self, remote_path: str, password: str = "") -> Optional[dict]:
        """获取单个文件的详情（含 raw_url），失败返回 None。"""
        self._ensure_token()
        body = self._request(
            "POST", "/api/fs/get",
            json={"path": remote_path, "password": password or ""},
        )
        if body is None or not response_ok(body):
            return None
        data = body.get("data")
        return data if isinstance(data, dict) else None

    def list_dir_raw(self, remote_path: str, password: str = "") -> dict:
        """返回原始响应体，便于排查字段差异。"""
        return self._request(
            "POST", "/api/fs/list",
            json={"path": remote_path, "page": 1, "per_page": 0,
                  "refresh": False, "password": password or ""},
        ) or {}

    def direct_url(self, remote_path: str) -> str:
        """构造文件的 OpenList 直链（`/d/<逐段编码路径>`）。

        与 strm 内容使用同一套构造逻辑，确保下载走的是同一个稳定入口。
        """
        from .strmutil import build_direct_url

        return build_direct_url(self.base_url, remote_path)
