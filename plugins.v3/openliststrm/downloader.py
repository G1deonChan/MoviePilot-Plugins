"""字幕 / 元数据 / 图片等小文件的下载器。

## 为什么需要下载而非生成 strm

视频文件用 strm（指向云盘直链）即可播放，但**字幕、NFO、封面等附属文件必须
是本地真实文件**：

- Emby / Jellyfin / Plex 刮削时直接读取同目录的 `.nfo`、`.jpg`；
- 播放器加载外挂字幕时会去同目录找 `.srt` / `.ass`；
- 这些文件体积很小（KB 级），下载成本可忽略。

因此本插件的策略是：
**视频 → 生成 strm；字幕/元数据/图片 → 实际下载到同目录。**

## 实现要点

- 通过 OpenList 的 `/d/<path>` 直链下载（该入口无需登录，服务端 302 到真实存储）。
- 流式写入 + 临时文件 + 原子替换，避免中断留下半个文件被刮削读到。
- 已存在且大小一致时跳过，避免重复下载。
- 单文件失败不影响其它文件，错误逐条记录。
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

# 下载传输层签名：(url, dest_path, timeout) -> (ok, message)
DownloadTransport = Callable[[str, Path, int], "tuple[bool, str]"]


@dataclass
class DownloadStats:
    """下载结果统计。"""

    downloaded: int = 0        # 新下载
    skipped: int = 0           # 本地已存在且大小一致
    updated: int = 0           # 本地存在但大小不同，已覆盖
    failed: int = 0
    bytes_total: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.downloaded + self.skipped + self.updated + self.failed

    def summary(self) -> str:
        """生成可读的统计摘要。"""
        parts = [f"下载 {self.downloaded}"]
        if self.updated:
            parts.append(f"更新 {self.updated}")
        if self.skipped:
            parts.append(f"跳过 {self.skipped}")
        if self.failed:
            parts.append(f"失败 {self.failed}")
        if self.bytes_total:
            parts.append(f"共 {human_size(self.bytes_total)}")
        return "、".join(parts)


def human_size(num: int) -> str:
    """把字节数格式化为可读字符串。"""
    size = float(num or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def needs_download(local_path: Path, remote_size: int) -> tuple[bool, bool]:
    """判断是否需要下载。

    :return: (是否需要下载, 是否属于覆盖更新)
    """
    try:
        if not local_path.exists():
            return True, False
        if not local_path.is_file():
            return True, False
        if remote_size and remote_size > 0:
            if local_path.stat().st_size == remote_size:
                return False, False        # 大小一致，认为已是最新
            return True, True              # 大小不同，覆盖更新
        # 远端未提供大小：本地已有则跳过，避免重复下载
        return False, False
    except OSError:
        return True, False


def atomic_write_bytes(dest: Path, data: bytes) -> None:
    """把字节原子写入目标文件。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(dest.parent), suffix=".part")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp_name, dest)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def download_all(
    items: list[tuple[str, str]],
    *,
    remote_sizes: Optional[dict[str, int]] = None,
    transport: Optional[DownloadTransport] = None,
    timeout: int = 60,
    force: bool = False,
    should_cancel: Optional[Callable[[], bool]] = None,
    max_files: int = 0,
) -> DownloadStats:
    """下载全部待下载文件。

    :param items:        (本地路径, 远端路径) 列表
    :param remote_sizes: 远端路径 -> 字节数；用于判断是否需要更新
    :param transport:    下载实现；None 时使用内置的 urllib 实现
    :param force:        True 时无视本地已存在的文件，一律重新下载
    :param max_files:    单次下载数量上限（0 表示不限），防止首次运行拉取过多
    """
    stats = DownloadStats()
    sizes = remote_sizes or {}
    if transport is None:
        transport = urllib_transport

    for local, remote in items:
        if should_cancel and should_cancel():
            break
        if max_files and stats.downloaded + stats.updated >= max_files:
            stats.errors.append(f"已达单次下载上限 {max_files}，其余文件将在下次任务中继续")
            break

        dest = Path(local)
        remote_size = int(sizes.get(remote, 0) or 0)

        if not force:
            need, is_update = needs_download(dest, remote_size)
            if not need:
                stats.skipped += 1
                continue
        else:
            is_update = dest.exists()

        ok, message = transport(remote, dest, timeout)
        if not ok:
            stats.failed += 1
            if len(stats.errors) < 20:
                stats.errors.append(f"{remote} -> {message}")
            continue

        try:
            size = dest.stat().st_size
            stats.bytes_total += size
        except OSError:
            size = 0

        if is_update:
            stats.updated += 1
        else:
            stats.downloaded += 1

    return stats


def urllib_transport(url: str, dest: Path, timeout: int) -> tuple[bool, str]:
    """内置下载实现：用标准库 urllib 流式下载到临时文件后原子替换。

    单独实现而不依赖宿主 RequestUtils，是为了让下载逻辑可被独立测试。
    """
    import urllib.error
    import urllib.request

    dest = Path(dest)
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
    except OSError as err:
        return False, f"创建目录失败：{err}"

    fd, tmp_name = tempfile.mkstemp(dir=str(dest.parent), suffix=".part")
    request = urllib.request.Request(url, headers={"User-Agent": "MoviePilot-OpenListStrm"})
    try:
        with os.fdopen(fd, "wb") as handle:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                while True:
                    chunk = response.read(64 * 1024)
                    if not chunk:
                        break
                    handle.write(chunk)
        os.replace(tmp_name, dest)
        return True, "OK"
    except urllib.error.HTTPError as err:
        _cleanup(tmp_name)
        return False, f"HTTP {err.code}"
    except urllib.error.URLError as err:
        _cleanup(tmp_name)
        return False, f"网络错误：{err.reason}"
    except Exception as err:  # noqa: BLE001
        _cleanup(tmp_name)
        return False, f"{type(err).__name__}: {err}"


def _cleanup(path: str) -> None:
    """删除临时文件，忽略失败。"""
    try:
        os.unlink(path)
    except OSError:
        pass
