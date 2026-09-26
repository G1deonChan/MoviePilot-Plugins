"""失效 strm 检测与联动清理。

## 检测

判断本地 strm 指向的 OpenList 文件是否仍然存在，采用「先粗筛再精查」：

1. **粗筛**：复用扫描阶段得到的「远端现存文件集合」，与本地 strm 反解出的路径求差集；
2. **精查**：对差集中的路径调用 `/api/fs/get` 逐个确认，避免因遍历遗漏而误判。

## 联动清理（三项，均可独立开关）

| 对象 | 说明 |
| --- | --- |
| `.strm` 文件 | 删除失效的 strm 本体 |
| 硬链接 | MoviePilot 以 `mode=link` 整理 strm 时，会在媒体库目录生成**同一个 strm 的硬链接**。删掉它只减少引用计数，不影响下载目录里的源 strm |
| 转移记录 | 删除 `transferhistory` 中对应的整理记录，避免媒体库里残留「已整理但文件已失效」的脏记录 |

## 安全约束

- **只读远端**：本模块不删除 OpenList 上的任何文件。
- **绝不误删原片**：`mode=link` 的整理对象是 `.strm` 文本文件本身（不是视频），
  且删除前用 `samefile()` 校验 inode 一致，只有确为同一文件的硬链接才会被删。
- **目录边界**：所有删除路径必须位于规则声明的输出目录、或其转移记录中的
  `dest` 路径内，越界一律拒绝。
- **默认预演**：检测结果先列表展示，必须显式确认才真正删除。
"""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional

from .openlist import OpenListClient, OpenListError
from .scanner import ScanRule
from .strmutil import normalize_remote_path

# strm 内容形如 https://host/d/<encoded path>（可能带 ?sign=... 查询串）
_ABSOLUTE_URL_RE = re.compile(
    r"^https?://[^/\s]+(?P<prefix>(?:/[^/\s?]*)*)?/d/(?P<path>[^?\s]+)", re.IGNORECASE
)
_RELATIVE_URL_RE = re.compile(
    r"^/?(?P<prefix>(?:[^/\s?]+/)*?)d/(?P<path>[^?\s]+)$", re.IGNORECASE
)


@dataclass
class BrokenStrm:
    """一个失效的 strm 记录。"""

    strm_path: Path                        # 本地 strm 绝对路径（下载/监控目录侧）
    raw_url: str                           # strm 中的原始内容
    remote_path: str                       # 反解出的 OpenList 路径
    reason: str                            # 判定为失效的原因
    hardlinks: list[Path] = field(default_factory=list)     # 找到的硬链接（媒体库侧）
    transfer_ids: list[int] = field(default_factory=list)   # 关联的整理记录 ID

    @property
    def detail(self) -> str:
        """供页面展示的一行摘要。"""
        extra = []
        if self.hardlinks:
            extra.append(f"硬链接 {len(self.hardlinks)}")
        if self.transfer_ids:
            extra.append(f"记录 {len(self.transfer_ids)}")
        suffix = f"（可联动清理：{'、'.join(extra)}）" if extra else ""
        return f"{self.remote_path} — {self.reason}{suffix}"


@dataclass
class CleanupPlan:
    """一次检测产出的清理计划。"""

    broken: list[BrokenStrm] = field(default_factory=list)
    total_scanned: int = 0
    skipped_unparsable: int = 0
    errors: list[str] = field(default_factory=list)
    cancelled: bool = False

    @property
    def count(self) -> int:
        return len(self.broken)

    @property
    def hardlink_count(self) -> int:
        return sum(len(item.hardlinks) for item in self.broken)

    @property
    def transfer_count(self) -> int:
        return sum(len(item.transfer_ids) for item in self.broken)


# --------------------------------------------------------------------- 解析
def parse_strm_url(content: str) -> Optional[tuple[str, str]]:
    """从 strm 内容反解出 (站点路径前缀, OpenList 路径)。

    兼容：
    - 绝对 URL：`https://host/d/<path>`、`https://host/openlist/d/<path>`
    - 相对路径：`/d/<path>`、`d/<path>`

    返回的第一项是 `/d/` 之前的路径前缀（实例挂子路径时非空，通常为空），
    第二项是解码后的 OpenList 路径（始终以 `/` 开头）。
    """
    text = str(content or "").strip()
    if not text:
        return None
    # 只取第一行，并去掉 BOM
    text = text.splitlines()[0].strip().lstrip("\ufeff")
    if not text:
        return None

    match = _ABSOLUTE_URL_RE.match(text)
    if match:
        prefix = match.group("prefix") or ""
        raw_path = match.group("path")
    else:
        rel = _RELATIVE_URL_RE.match(text)
        if not rel:
            return None
        raw = rel.group("prefix") or ""
        prefix = "/" + raw.rstrip("/") if raw.strip("/") else ""
        raw_path = rel.group("path")

    segments = [urllib.parse.unquote(seg) for seg in raw_path.split("/") if seg]
    if not segments:
        return None
    return (prefix.rstrip("/"), normalize_remote_path("/" + "/".join(segments)))


def read_strm(path: Path) -> Optional[str]:
    """读取 strm 内容；失败返回 None。"""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def iter_strm_files(root: Path) -> Iterable[Path]:
    """遍历输出目录下的全部 .strm 文件（结果有序，便于稳定展示）。"""
    if not root.is_dir():
        return
    yield from sorted(root.rglob("*.strm"))


# --------------------------------------------------------------------- 边界校验
def is_within(path: Path, roots: Iterable[Path]) -> bool:
    """检查路径是否位于允许的根目录内，防止越界删除。"""
    try:
        resolved = path.resolve(strict=False)
    except OSError:
        return False
    for root in roots:
        try:
            base = Path(root).resolve(strict=False)
        except OSError:
            continue
        if resolved == base or base in resolved.parents:
            return True
    return False


def safe_delete(path: Path, allowed_roots: list[Path]) -> tuple[bool, str]:
    """安全删除单个文件：必须位于允许目录内，且是普通文件。"""
    if not is_within(path, allowed_roots):
        return False, f"路径不在允许范围内，已拒绝：{path}"
    try:
        if not path.exists():
            return False, f"文件不存在：{path}"
        if path.is_file() is False:
            return False, f"不是普通文件，已拒绝：{path}"
        path.unlink()
        return True, "已删除"
    except OSError as err:
        return False, f"删除失败：{err}"


def same_inode(a: Path, b: Path) -> bool:
    """判断两个路径是否指向同一份数据（硬链接或同一文件）。

    等价于宿主 `SystemUtils.is_hardlink` 的单文件判定：基于 inode 比较，
    因此只有确为同一文件时才会返回 True。
    """
    try:
        if not a.exists() or not b.exists():
            return False
        return a.samefile(b)
    except OSError:
        return False


# --------------------------------------------------------------------- 检测
def collect_broken(
    rules: list[ScanRule],
    *,
    client: Optional[OpenListClient] = None,
    remote_existing: Optional[set[str]] = None,
    verify_remote: bool = True,
    max_verify: int = 3000,
    should_cancel: Optional[Callable[[], bool]] = None,
) -> CleanupPlan:
    """收集失效的 strm。

    :param remote_existing: 远端现存文件集合（来自扫描结果）。为 None 时跳过粗筛，
                            直接对每个本地 strm 做 API 校验。
    :param verify_remote:   是否对粗筛结果调用 API 精查（防止集合不完整导致误判）
    :param max_verify:      精查数量上限，避免超大库拖慢任务
    """
    plan = CleanupPlan()
    existing = {normalize_remote_path(p) for p in (remote_existing or set())}
    verified = 0

    for rule in rules:
        for strm_path in iter_strm_files(Path(rule.local_dir)):
            if should_cancel and should_cancel():
                plan.cancelled = True
                return plan

            plan.total_scanned += 1
            content = read_strm(strm_path)
            if content is None:
                plan.errors.append(f"无法读取：{strm_path}")
                continue

            parsed = parse_strm_url(content)
            if not parsed:
                # 非本插件生成的内容一律跳过，绝不纳入删除候选
                plan.skipped_unparsable += 1
                continue

            _, remote_path = parsed

            if remote_existing is not None and remote_path in existing:
                continue        # 远端仍在，无需处理

            if remote_existing is None:
                reason = "远端文件不可访问"
            else:
                reason = "远端目录树中已不存在"

            # 精查：粗筛可能因遍历上限/异常而不完整，务必确认后再删
            if verify_remote and client is not None:
                if verified >= max_verify:
                    plan.errors.append(
                        f"精查项已达上限 {max_verify}，其余未确认（可调大上限后重试）"
                    )
                    break
                verified += 1
                try:
                    if client.get_file(remote_path) is not None:
                        continue    # 实际存在，属粗筛误判
                except OpenListError as err:
                    plan.errors.append(f"校验失败 {remote_path}：{err}")
                    continue
                reason = "远端文件已不存在（已 API 校验）"

            plan.broken.append(BrokenStrm(
                strm_path=strm_path,
                raw_url=content.strip(),
                remote_path=remote_path,
                reason=reason,
            ))

    return plan


def attach_hardlinks(
    plan: CleanupPlan,
    rules: list[ScanRule],
    transfer_lookup: Callable[[Path], list[dict]],
) -> None:
    """为每个失效项补齐硬链接与转移记录信息（只读，不做删除）。

    :param transfer_lookup: 传入本地 strm 路径，返回关联的整理记录列表，
                            每项至少含 `id` 与 `dest` 字段。
    """
    # 硬链接可能位于媒体库目录（规则输出目录之外），因此允许范围要放宽到
    # 「记录里声明的 dest」，而不是仅限规则目录
    for item in plan.broken:
        records = transfer_lookup(item.strm_path) or []
        for record in records:
            record_id = record.get("id")
            if isinstance(record_id, int):
                item.transfer_ids.append(record_id)

            dest = record.get("dest")
            if not dest:
                continue
            candidate = Path(str(dest))
            # 只有确认与源 strm 是同一份数据（硬链接）才纳入
            if candidate.exists() and same_inode(candidate, item.strm_path):
                if candidate not in item.hardlinks:
                    item.hardlinks.append(candidate)


def execute_cleanup(
    plan: CleanupPlan,
    rules: list[ScanRule],
    *,
    delete_strm: bool = True,
    delete_hardlinks: bool = False,
    delete_records: bool = False,
    record_deleter: Optional[Callable[[int], bool]] = None,
) -> dict:
    """执行清理，返回逐项结果统计。

    默认只删 strm 本体；硬链接与转移记录需显式开启。
    所有被删路径都会再次做边界校验。
    """
    allowed: list[Path] = [Path(r.local_dir) for r in rules if r.local_dir]
    # 硬链接位于媒体库目录，加入其所在父目录作为允许范围
    for item in plan.broken:
        for link in item.hardlinks:
            allowed.append(link.parent)

    stats = {
        "strm_deleted": 0, "strm_failed": 0,
        "link_deleted": 0, "link_failed": 0,
        "record_deleted": 0, "record_failed": 0,
        "messages": [],
    }

    for item in plan.broken:
        if delete_hardlinks:
            for link in item.hardlinks:
                ok, message = safe_delete(link, allowed)
                if ok:
                    stats["link_deleted"] += 1
                else:
                    stats["link_failed"] += 1
                    stats["messages"].append(f"硬链接 {message}")

        if delete_strm:
            ok, message = safe_delete(item.strm_path, allowed)
            if ok:
                stats["strm_deleted"] += 1
            else:
                stats["strm_failed"] += 1
                stats["messages"].append(f"strm {message}")

        if delete_records and record_deleter is not None:
            for record_id in item.transfer_ids:
                try:
                    if record_deleter(record_id):
                        stats["record_deleted"] += 1
                    else:
                        stats["record_failed"] += 1
                except Exception as err:  # noqa: BLE001 - 单条失败不应中断
                    stats["record_failed"] += 1
                    stats["messages"].append(f"转移记录 {record_id} 删除异常：{err}")

    return stats
