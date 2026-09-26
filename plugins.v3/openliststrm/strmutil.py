"""OpenList 目录树遍历与 strm 生成的纯逻辑工具。

本模块只依赖标准库，便于单元测试；网络请求由插件主类注入，不在导入期发生
任何副作用。设计目标是：不依赖 WebDAV / 本地挂载，直接通过 OpenList HTTP API
读取目录树并产出 strm 内容。
"""

from __future__ import annotations

import fnmatch
import posixpath
import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

# 默认纳入的视频扩展名（小写，含点）→ 生成 .strm。可在插件配置中覆盖。
#
# 注意：**不含 `.strm`**。远端若存在 .strm 文件，它本身就是一个「指向别处的链接」，
# 再为它生成 .strm 只会得到嵌套的链接文件，没有意义。
DEFAULT_VIDEO_EXT = [
    ".mp4", ".mkv", ".ts", ".iso", ".rmvb", ".avi", ".mov", ".mpeg", ".mpg",
    ".wmv", ".3gp", ".asf", ".m4v", ".flv", ".m2ts", ".tp", ".f4v",
    ".webm", ".vob", ".divx", ".mts", ".m2t", ".mxf",
]

# 默认纳入的「下载」扩展名 → 实际下载为本地真实文件。
#
# 只收**必须由本插件提供**的文件：
#   - 字幕：播放时需要与视频同目录的真实文件
#
# 刻意**不收** .nfo / .xml / 图片：MoviePilot 的刮削流程会自己生成这些元数据，
# 插件再下载一份会造成重复，并与刮削结果互相覆盖（实测曾产生 4949 个重复 .nfo）。
# 若确实需要远端自带的元数据，可在配置里把它们加进「下载扩展名」。
DEFAULT_DOWNLOAD_EXT = [
    # 字幕
    ".srt", ".ass", ".ssa", ".sub", ".idx", ".sup", ".vtt", ".smi", ".ttml",
]

# 旧版默认值（含元数据与图片），仅用于迁移提示，不再作为默认生效
LEGACY_DOWNLOAD_EXT = [
    ".srt", ".ass", ".ssa", ".sub", ".idx", ".sup", ".vtt", ".smi", ".ttml",
    ".nfo", ".xml",
    ".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tbn",
]

# 向后兼容别名：旧配置/旧测试使用 DEFAULT_META_EXT 表示「附属文件」
DEFAULT_META_EXT = DEFAULT_DOWNLOAD_EXT

# 生成的 strm 文件后缀。产物是文本文件，必须带该后缀才能被
# `rglob("*.strm")` 系列的检测/清理功能识别。
STRM_SUFFIX = ".strm"

# 默认跳过的垃圾目录名（各种网盘/系统/下载器产生的元数据目录）。
# 这些目录里的内容永远不需要生成 strm 或下载。
DEFAULT_SKIP_DIRS = [
    "@eadir", "@tmp", "@recycle", "#recycle", "#snapshot",
    "$recycle.bin", "system volume information", "lost+found",
    ".git", ".svn", ".stfolder", ".recycle", ".trash", ".trash-1000",
    ".thumbnails", ".cache", ".sync", "node_modules",
    "__macosx", ".ds_store",
]

# 默认跳过的垃圾文件名（大小写不敏感；支持 `*` 通配）。
# 覆盖系统残留、下载器临时文件、广告/推广文件等。
DEFAULT_SKIP_FILES = [
    "thumbs.db", "desktop.ini", ".ds_store", "ehthumbs.db",
    "*.tmp", "*.temp", "*.part", "*.partial", "*.crdownload",
    "*.!qb", "*.!ut", "*.downloading", "*.aria2", "*.bc!",
    "*.url", "*.lnk", "*.db", "*.ini", "*.log", "*.bak",
    "*.torrent", "*.nfo.bak",
    # 常见广告/推广/说明文件
    "*广告*", "*推广*", "*最新地址*", "*获取方式*", "*更多资源*",
    "*请勿*", "*必看*", "*公告*", "*.html", "*.htm",
]


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


def strm_target_path(
    root_dir: str,
    remote_path: str,
    remote_root: str = "/",
    *,
    add_strm_suffix: bool = True,
    replace_extension: bool = True,
) -> str:
    """把远端文件路径映射为本地输出路径。

    - `remote_root`：远端扫描起点（如 `/EmbyCloud`），会从输出中剥离，避免本地多出
      一层与挂载点同名的目录。

    - `replace_extension`：把**文件名的最后一个扩展名替换成 `.strm`**。

      `剧集名.mkv` → `剧集名.strm`。这是与 CloudStrm 等同类插件一致的命名，
      也是媒体库既有的组织方式，便于从其它工具平滑迁移。

    - `add_strm_suffix`：不替换扩展名，而是**追加** `.strm`（`剧集名.mkv.strm`）。
      两种风格二选一；`replace_extension` 优先。

    - 单纯传 `add_strm_suffix=False, replace_extension=False` 时保留原文件名，
      用于下载字幕等实体文件。

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

    if replace_extension:
        # 替换最后一个扩展名：a.mkv -> a.strm
        # 无扩展名的文件（如 "movie"）则追加，避免变成 "movie.strm" 之外的空名
        stem, ext = posixpath.splitext(relative)
        relative = f"{stem}{STRM_SUFFIX}" if ext else f"{relative}{STRM_SUFFIX}"
    elif add_strm_suffix:
        relative = f"{relative}{STRM_SUFFIX}"

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


def compile_globs(patterns: Iterable[str]) -> list[re.Pattern]:
    """把通配符模式编译成正则（大小写不敏感）。

    支持 `*`（任意字符）与 `?`（单字符）。非法模式会被跳过。
    """
    compiled: list[re.Pattern] = []
    for raw in patterns or []:
        pattern = str(raw or "").strip()
        if not pattern:
            continue
        try:
            compiled.append(re.compile(fnmatch.translate(pattern), re.IGNORECASE))
        except Exception:  # noqa: BLE001 - 单个模式异常不应影响其它
            continue
    return compiled

def should_skip_dir(
    name: str,
    skip_dirs: Iterable[str],
    extra_patterns: Optional[Iterable[re.Pattern]] = None,
) -> bool:
    """判断目录是否属于应跳过的垃圾目录。"""
    lowered = str(name or "").strip().lower()
    if not lowered:
        return True
    if lowered in {str(d).strip().lower() for d in (skip_dirs or [])}:
        return True
    # 隐藏目录（以 . 开头）一律跳过，避免把 .git / .stfolder 等纳入扫描
    if lowered.startswith("."):
        return True
    for pattern in (extra_patterns or []):
        if pattern.match(lowered):
            return True
    return False


def should_skip_file(
    name: str,
    skip_files: Iterable[str],
    extra_patterns: Optional[Iterable[re.Pattern]] = None,
) -> bool:
    """判断文件是否属于应跳过的无关文件（广告、临时文件、系统残留等）。"""
    lowered = str(name or "").strip().lower()
    if not lowered:
        return True
    if lowered in {str(f).strip().lower() for f in (skip_files or [])}:
        return True
    for pattern in (extra_patterns or []):
        if pattern.match(lowered):
            return True
    return False


def parse_multiline_list(text: str) -> list[str]:
    """解析多行/逗号分隔的列表文本，忽略空行与 `#` 注释行。

    让「跳过目录」「跳过文件」既能一行一个，也能逗号分隔。
    """
    items: list[str] = []
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        for part in line.replace("，", ",").split(","):
            item = part.strip()
            if item:
                items.append(item)
    return items


def relative_display_path(root_dir: str, target: str) -> str:
    """生成用于日志展示的相对路径。"""
    try:
        return posixpath.relpath(str(target).replace("\\", "/"), str(root_dir).replace("\\", "/").rstrip("/"))
    except Exception:
        return str(target)
