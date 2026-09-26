"""OpenList 目录树遍历与 strm 生成的纯逻辑工具。

本模块只依赖标准库，便于单元测试；网络请求由插件主类注入，不在导入期发生
任何副作用。设计目标是：不依赖 WebDAV / 本地挂载，直接通过 OpenList HTTP API
读取目录树并产出 strm 内容。
"""

from __future__ import annotations

import posixpath
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Iterable

# 默认纳入的视频扩展名（小写，含点）→ 生成 .strm。可在插件配置中覆盖。
DEFAULT_VIDEO_EXT = [
    ".mp4", ".mkv", ".ts", ".iso", ".rmvb", ".avi", ".mov", ".mpeg", ".mpg",
    ".wmv", ".3gp", ".asf", ".m4v", ".flv", ".m2ts", ".strm", ".tp", ".f4v",
    ".webm", ".vob", ".divx", ".mts", ".m2t", ".mxf",
]

# 默认纳入的「下载」扩展名（字幕、元数据、图片等）→ 实际下载到本地。
# 这些文件体积小，且媒体服务器刮削/播放时需要真实文件在本地。
DEFAULT_DOWNLOAD_EXT = [
    # 字幕
    ".srt", ".ass", ".ssa", ".sub", ".idx", ".sup", ".vtt", ".smi", ".ttml",
    # 元数据
    ".nfo", ".xml", ".txt", ".json",
    # 图片
    ".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tbn",
]

# 向后兼容别名：旧配置/旧测试使用 DEFAULT_META_EXT 表示「附属文件」
DEFAULT_META_EXT = DEFAULT_DOWNLOAD_EXT


def normalize_remote_path(path: str) -> str:
    """规范化 OpenList 远端路径为以 / 开头的 POSIX 形式。

    对齐 OpenList `utils.FixAndCleanPath` 的语义：反斜杠转正斜杠、确保以 / 开头、
    折叠 . 与 ..（根目录之上仍是根目录）。
    """
    if path is None:
        path = ""
    text = str(path).strip().replace("\\", "/")
    if not text.startswith("/"):
        text = "/" + text
    cleaned = posixpath.normpath(text)
    # normpath 会把 "/" 保留为 "/"，但 "//a" 会变成 "//a"，这里再收敛一次
    while cleaned.startswith("//"):
        cleaned = cleaned[1:]
    return cleaned or "/"


def join_remote_path(parent: str, name: str) -> str:
    """把目录项名拼接到父路径上，返回规范化的绝对远端路径。"""
    return normalize_remote_path(posixpath.join(normalize_remote_path(parent), str(name)))


def encode_remote_path(path: str) -> str:
    """按路径段编码远端路径，用于拼装 OpenList 直链。

    逐段编码可保留 `/` 作为分隔符，同时正确转义空格、中文、`#`、`?`、`%` 等字符；
    相比整体 `quote(path, safe="")` 更不容易因服务端解码差异而出错。
    """
    normalized = normalize_remote_path(path)
    segments = [seg for seg in normalized.split("/") if seg]
    return "/".join(urllib.parse.quote(seg, safe="") for seg in segments)


def build_direct_url(base_url: str, remote_path: str, encode: bool = True) -> str:
    """构造 OpenList 直链（`/d/<path>`），供 strm 使用。"""
    base = str(base_url or "").rstrip("/")
    encoded = encode_remote_path(remote_path) if encode else normalize_remote_path(remote_path).lstrip("/")
    return f"{base}/d/{encoded}"


def build_download_url(base_url: str, remote_path: str) -> str:
    """构造带直链下载的完整 URL（用于「复制链接」展示）。"""
    return build_direct_url(base_url, remote_path, encode=True)


def is_video_file(name: str, video_ext: Iterable[str]) -> bool:
    """判断文件名是否为配置纳入的视频扩展名。"""
    ext = posixpath.splitext(str(name))[1].lower()
    return ext in {str(e).lower() for e in video_ext}


def is_meta_file(name: str, meta_ext: Iterable[str]) -> bool:
    """判断文件名是否为可选的附属元数据/字幕文件。"""
    ext = posixpath.splitext(str(name))[1].lower()
    return ext in {str(e).lower() for e in meta_ext}


def strm_target_path(root_dir: str, remote_path: str, remote_root: str = "/") -> str:
    """把远端文件路径映射为本地 strm 输出相对路径。

    - `remote_root`：远端扫描起点（如 `/EmbyCloud`），会从输出中剥离，避免本地多出
      一层与挂载点同名的目录。
    - 返回值使用 POSIX 分隔符，调用方再用 `Path` 拼接，保证跨平台一致。
    """
    remote = normalize_remote_path(remote_path)
    root = normalize_remote_path(remote_root)
    base = str(root_dir or "").strip().rstrip("/")

    if root != "/" and (remote == root or remote.startswith(root + "/")):
        relative = remote[len(root):].lstrip("/")
    else:
        relative = remote.lstrip("/")

    if not relative:
        relative = posixpath.basename(remote)
    return f"{base}/{relative}" if base else relative


def iter_entries(payload: Any) -> list[dict]:
    """从 `/api/fs/list` 响应中提取条目列表，兼容多种返回形状。

    OpenList 正常返回 `data.content`；此处对 data 直接是列表、content 缺失等情况
    做容错，避免因版本差异直接抛异常。
    """
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if isinstance(data, list):
        return [e for e in data if isinstance(e, dict)]
    if isinstance(data, dict):
        content = data.get("content")
        if isinstance(content, list):
            return [e for e in content if isinstance(e, dict)]
    return []


def response_ok(payload: Any) -> bool:
    """判断 OpenList 响应是否成功（约定 code == 200）。"""
    if not isinstance(payload, dict):
        return False
    code = payload.get("code")
    if isinstance(code, int):
        return code == 200
    if isinstance(code, str) and code.isdigit():
        return int(code) == 200
    return False


@dataclass
class OutputPlan:
    """一次扫描的产物清单，区分「生成 strm」与「下载文件」两类。"""

    # 生成 .strm： (本地绝对路径, strm 内容)
    strm_files: list[tuple[str, str]] = field(default_factory=list)
    # 下载实体文件： (本地绝对路径, 远端绝对路径)
    downloads: list[tuple[str, str]] = field(default_factory=list)
    scanned_files: int = 0
    scanned_dirs: int = 0
    skipped: int = 0

    @property
    def total(self) -> int:
        return len(self.strm_files) + len(self.downloads)


# 向后兼容别名（旧调用方可能引用 StrmPlan）
StrmPlan = OutputPlan


def classify_output(
    name: str,
    video_ext: Iterable[str],
    download_ext: Iterable[str],
) -> str:
    """判断文件应如何处理。

    :return: `"strm"` 生成 strm、`"download"` 下载实体文件、`"skip"` 忽略
    """
    ext = posixpath.splitext(str(name))[1].lower()
    if not ext:
        return "skip"
    if ext in {str(e).lower() for e in video_ext}:
        return "strm"
    if ext in {str(e).lower() for e in download_ext}:
        return "download"
    return "skip"


def relative_display_path(root_dir: str, target: str) -> str:
    """生成用于日志展示的相对路径。"""
    try:
        return posixpath.relpath(str(target).replace("\\", "/"), str(root_dir).replace("\\", "/").rstrip("/"))
    except Exception:
        return str(target)
