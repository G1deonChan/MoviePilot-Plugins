"""远端目录树持久化缓存。

## 为什么需要

OpenList 只有一个「列单层目录」的接口（`/api/fs/list`），没有一次拉取整棵树的
API。因此规模大的库（如 795 个目录 / 1.4 万文件）每次全量扫描都要逐目录请求，
在网络往返上耗时明显。

本模块把「目录 → 条目列表」的结果按目录持久化到磁盘，并记录远端目录的
`mtime`。下次扫描时：

- **目录 mtime 未变** → 直接复用缓存条目，**不再发起请求**；
- **mtime 变化或目录是新增的** → 重新拉取并更新缓存；
- **远端已删除的目录** → 扫描结束后从缓存中淘汰。

这样在「远端无变化」的常态下，绝大部分目录可以零请求完成遍历。

## 一致性

缓存的失效判据是**目录自身的 mtime**（来自 `/api/fs/list` 返回的父目录条目），
它是 OpenList 层面可见的最新修改时间。若某些存储驱动不维护目录 mtime，
缓存可能偏旧，此时可关闭缓存或调小 TTL 强制刷新。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

# 缓存格式版本；结构变更时递增，旧缓存会被自动忽略并重建
CACHE_VERSION = 2
CACHE_FILENAME = "tree_cache.json"

# 单次扫描允许写入的最大条目数，避免异常情况把缓存撑爆
MAX_ENTRIES_PER_DIR = 100000


@dataclass
class DirCacheEntry:
    """单个目录的缓存条目。"""

    mtime: str = ""                    # 远端目录的修改时间（字符串，便于 JSON 序列化）
    fetched_at: float = 0.0            # 本地抓取时间戳
    entries: list[dict] = field(default_factory=list)   # /api/fs/list 的 content 原样保存

    def to_json(self) -> dict:
        return {"mtime": self.mtime, "fetched_at": self.fetched_at, "entries": self.entries}

    @classmethod
    def from_json(cls, raw: Any) -> Optional["DirCacheEntry"]:
        if not isinstance(raw, dict):
            return None
        entries = raw.get("entries")
        if not isinstance(entries, list):
            return None
        return cls(
            mtime=str(raw.get("mtime") or ""),
            fetched_at=float(raw.get("fetched_at") or 0.0),
            entries=[e for e in entries if isinstance(e, dict)][:MAX_ENTRIES_PER_DIR],
        )


class TreeCache:
    """目录树缓存：加载、查询、更新、落盘。"""

    def __init__(self, path: Path, ttl_hours: float = 0) -> None:
        """
        :param path:      缓存文件路径
        :param ttl_hours: 缓存最长有效时长（小时）。0 或负数表示不按时间过期，
                          完全依赖目录 mtime 判断。
        """
        self.path = Path(path)
        self.ttl_seconds = max(0.0, float(ttl_hours)) * 3600
        self.dirs: dict[str, DirCacheEntry] = {}
        self.dirty = False
        # 统计
        self.hits = 0
        self.misses = 0
        self.stale = 0
        self.evicted = 0

    # ------------------------------------------------------------------ 持久化
    def load(self) -> bool:
        """从磁盘加载缓存；失败时静默返回 False（按空缓存处理）。"""
        try:
            if not self.path.is_file():
                return False
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        if not isinstance(raw, dict) or raw.get("version") != CACHE_VERSION:
            return False
        dirs = raw.get("dirs")
        if not isinstance(dirs, dict):
            return False
        for key, value in dirs.items():
            entry = DirCacheEntry.from_json(value)
            if entry is not None:
                self.dirs[str(key)] = entry
        return True

    def save(self) -> bool:
        """把缓存写回磁盘（原子写入，避免中断产生半个文件）。"""
        if not self.dirty:
            return True
        payload = {
            "version": CACHE_VERSION,
            "saved_at": time.time(),
            "dirs": {k: v.to_json() for k, v in self.dirs.items()},
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            tmp.replace(self.path)
            self.dirty = False
            return True
        except OSError:
            return False

    # ------------------------------------------------------------------ 查询/更新
    def is_fresh(self, entry: Optional[DirCacheEntry]) -> bool:
        """判断缓存条目是否仍在有效期内。"""
        if entry is None:
            return False
        if self.ttl_seconds <= 0:
            return True
        return (time.time() - entry.fetched_at) <= self.ttl_seconds

    def get(self, remote_path: str, mtime: Optional[str] = None) -> Optional[list[dict]]:
        """读取缓存条目。

        :param mtime: 已知的远端目录 mtime。传入时会与缓存比较，不一致则视为过期。
                      传 None 表示不做 mtime 比对，仅按 TTL 判断。
        """
        entry = self.dirs.get(remote_path)
        if entry is None:
            self.misses += 1
            return None
        if not self.is_fresh(entry):
            self.stale += 1
            return None
        if mtime is not None and entry.mtime and str(mtime) != entry.mtime:
            self.stale += 1
            return None
        self.hits += 1
        return entry.entries

    def put(self, remote_path: str, entries: list[dict], mtime: Optional[str] = None) -> None:
        """写入/更新缓存条目。"""
        self.dirs[remote_path] = DirCacheEntry(
            mtime=str(mtime or ""),
            fetched_at=time.time(),
            entries=entries[:MAX_ENTRIES_PER_DIR],
        )
        self.dirty = True

    def prune(self, seen_paths: set[str]) -> int:
        """淘汰本次扫描中未出现、且位于扫描根之下的缓存条目。

        :param seen_paths: 本次扫描实际访问过的目录集合
        """
        # 只淘汰「看起来属于本次扫描范围」的条目：以某个已见目录为前缀
        prefixes = {p.rstrip("/") + "/" for p in seen_paths}
        removed = []
        for key in list(self.dirs):
            if key in seen_paths:
                continue
            if any(key.startswith(prefix) or key == prefix.rstrip("/") for prefix in prefixes):
                removed.append(key)
        for key in removed:
            self.dirs.pop(key, None)
        if removed:
            self.dirty = True
            self.evicted += len(removed)
        return len(removed)

    def age_hours(self) -> Optional[float]:
        """返回最旧条目的年龄（小时），用于展示。"""
        if not self.dirs:
            return None
        oldest = min(e.fetched_at for e in self.dirs.values() if e.fetched_at > 0)
        if oldest <= 0:
            return None
        return (time.time() - oldest) / 3600

    def stats(self) -> dict:
        """返回缓存统计，供页面与日志展示。"""
        total_entries = sum(len(e.entries) for e in self.dirs.values())
        return {
            "dirs": len(self.dirs),
            "entries": total_entries,
            "hits": self.hits,
            "misses": self.misses,
            "stale": self.stale,
            "evicted": self.evicted,
            "age_hours": round(self.age_hours() or 0.0, 1),
            "size_kb": round(self.file_size() / 1024, 1),
        }

    def file_size(self) -> int:
        """缓存文件大小（字节）。"""
        try:
            return self.path.stat().st_size
        except OSError:
            return 0

    def clear(self) -> None:
        """清空内存与磁盘缓存。"""
        self.dirs.clear()
        self.dirty = False
        try:
            if self.path.is_file():
                self.path.unlink()
        except OSError:
            pass
