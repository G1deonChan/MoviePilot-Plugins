"""并发扫描 OpenList 目录树并生成可写入的 strm 计划。

设计要点：
- 不依赖 WebDAV / 本地挂载，纯 HTTP API 遍历。
- **按层并发**（BFS + 线程池）：实测单请求约 111ms，8 线程可提速约 6 倍。
- 支持多行「扫描规则」，每行形如：
      OpenList路径#本地输出目录[#包含正则[#排除正则]]
  也兼容用 `|` 分隔，便于在单行输入框中填写。
- 目录级 exclude 提前剪枝，避免无效递归。
- 可选接入 `TreeCache`：目录 mtime 未变时直接复用缓存条目，大幅减少请求数。
- 单个目录失败只跳过该目录，绝不中止整轮扫描。
- 通过 `should_cancel` 回调支持中途取消（插件停用/重载）。
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Callable, Optional

from .openlist import OpenListClient, OpenListError
from .strmutil import (
    DEFAULT_DOWNLOAD_EXT,
    DEFAULT_SKIP_DIRS,
    DEFAULT_SKIP_FILES,
    DEFAULT_VIDEO_EXT,
    build_direct_url,
    classify_output,
    compile_globs,
    join_remote_path,
    normalize_remote_path,
    should_skip_dir,
    should_skip_file,
    strm_target_path,
)
from .treecache import TreeCache

# 支持的分隔符：优先 `#`（与同类插件习惯一致），并兼容 `|`
_SEPARATORS = ("#", "|")

# 并发遍历的默认线程数。
# 实测（OpenList 单请求约 111ms）：8 线程提速约 6 倍，16 线程无进一步收益，
# 故取 8 作为兼顾速度与对服务端压力的默认值。
DEFAULT_WORKERS = 8


@dataclass
class ScanRule:
    """一条扫描规则。"""

    remote_path: str          # OpenList 内的扫描起点，如 /EmbyCloud
    local_dir: str            # 本地 strm 输出目录，如 /volume1/video/strm/source
    include: str = ""         # 只处理匹配该正则的路径（留空=全部）
    exclude: str = ""         # 跳过匹配该正则的路径
    raw: str = ""             # 原始配置行，用于日志

    _include_re: Optional[re.Pattern] = field(default=None, repr=False, compare=False)
    _exclude_re: Optional[re.Pattern] = field(default=None, repr=False, compare=False)

    def compiled(self) -> tuple[Optional[re.Pattern], Optional[re.Pattern]]:
        """编译并缓存正则，非法正则会被忽略（不阻断整个任务）。"""
        if self._include_re is None and self.include:
            try:
                self._include_re = re.compile(self.include)
            except re.error:
                self._include_re = None
        if self._exclude_re is None and self.exclude:
            try:
                self._exclude_re = re.compile(self.exclude)
            except re.error:
                self._exclude_re = None
        return self._include_re, self._exclude_re

    def matches(self, remote_path: str) -> bool:
        """判断**文件**路径是否应被处理（同时应用 include / exclude）。"""
        include_re, exclude_re = self.compiled()
        if exclude_re and exclude_re.search(remote_path):
            return False
        if include_re and not include_re.search(remote_path):
            return False
        return True

    def allows_dir(self, remote_path: str) -> bool:
        """判断**目录**是否应继续递归。

        只应用 exclude：include 通常用于筛选文件名（如 `\\.mkv$`、`movie`），
        若也用于目录，父目录因不匹配而被剪枝，其下符合条件的文件将永远扫不到。
        因此 include 只在文件层生效，exclude 在目录层提前剪枝以节省请求。
        """
        _, exclude_re = self.compiled()
        if exclude_re and exclude_re.search(remote_path):
            return False
        return True


def parse_rules(text: str) -> tuple[list[ScanRule], list[str]]:
    """解析多行规则文本，返回 (规则列表, 错误说明列表)。

    每行格式：`远端路径#本地目录[#包含正则[#排除正则]]`
    以 `#` 开头的整行视为注释；空行忽略。
    """
    rules: list[ScanRule] = []
    errors: list[str] = []
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        parts = _split_line(line)
        if len(parts) < 2:
            errors.append(f"规则缺少本地输出目录，已跳过：{line}")
            continue

        remote_path, local_dir = parts[0].strip(), parts[1].strip()
        if not remote_path or not local_dir:
            errors.append(f"规则存在空字段，已跳过：{line}")
            continue

        include = parts[2].strip() if len(parts) > 2 else ""
        exclude = parts[3].strip() if len(parts) > 3 else ""

        for label, pattern in (("包含", include), ("排除", exclude)):
            if pattern:
                try:
                    re.compile(pattern)
                except re.error as err:
                    errors.append(f"{label}正则非法（{err}），已忽略该项：{line}")

        rules.append(ScanRule(
            remote_path=normalize_remote_path(remote_path),
            local_dir=local_dir.replace("\\", "/").rstrip("/"),
            include=include,
            exclude=exclude,
            raw=line,
        ))
    return rules, errors


def _split_line(line: str) -> list[str]:
    """按 `#` 或 `|` 切分规则行，取出现次数更多的分隔符，避免与正则内容冲突。"""
    hash_parts = line.split("#")
    pipe_parts = line.split("|")
    if len(hash_parts) >= len(pipe_parts):
        return hash_parts
    return pipe_parts


@dataclass
class ScanResult:
    """一次扫描的汇总结果。"""

    # 生成 .strm： (本地路径, strm 内容)
    planned: list[tuple[str, str]] = field(default_factory=list)
    # 下载实体文件： (本地路径, 远端路径)
    downloads: list[tuple[str, str]] = field(default_factory=list)
    dirs_scanned: int = 0
    files_seen: int = 0
    videos_found: int = 0
    metas_found: int = 0
    skipped_by_rule: int = 0
    # 被内置/自定义过滤器跳过的数量
    skipped_dirs: int = 0
    skipped_files: int = 0
    # 因上游报错被跳过的目录数（不等于 0 说明扫描不完整）
    dirs_failed: int = 0
    errors: list[str] = field(default_factory=list)
    cancelled: bool = False
    # 远端现存文件的绝对路径集合，供失效检测复用（避免二次遍历）
    remote_files: set[str] = field(default_factory=set)
    # 远端文件大小：远端路径 -> 字节数（供下载时判断是否需要更新）
    remote_sizes: dict[str, int] = field(default_factory=dict)
    # 本次实际访问过的远端目录，供缓存淘汰使用
    visited_dirs: set[str] = field(default_factory=set)
    # 缓存统计（未启用缓存时为空）
    cache: dict = field(default_factory=dict)

    @property
    def planned_count(self) -> int:
        return len(self.planned)

    @property
    def download_count(self) -> int:
        return len(self.downloads)


def scan(
    client: OpenListClient,
    rules: list[ScanRule],
    *,
    video_ext: Optional[list[str]] = None,
    download_ext: Optional[list[str]] = None,
    skip_dirs: Optional[list[str]] = None,
    skip_files: Optional[list[str]] = None,
    force: bool = False,
    should_cancel: Optional[Callable[[], bool]] = None,
    max_depth: int = 64,
    dir_password: str = "",
    base_path: str = "/",
    cache: Optional[TreeCache] = None,
    workers: int = DEFAULT_WORKERS,
) -> ScanResult:
    """遍历所有规则并产出 strm 与下载计划。

    **并发遍历**：按层并发请求同层目录（BFS），而不是逐目录串行递归。

    实测（795 目录的 /Ani）：单请求约 111ms、串行需 95s+；
    8 线程并发可提速约 6 倍（16 线程无进一步收益，故默认 8）。
    并发只影响**请求顺序**，不影响结果：每个目录的处理彼此独立，
    产出顺序在最后统一排序，保证结果稳定可复现。

    :param video_ext:    纳入「生成 strm」的扩展名
    :param download_ext: 纳入「下载实体文件」的扩展名（字幕等）
    :param skip_dirs:    需要跳过的目录名（精确匹配，大小写不敏感）
    :param skip_files:   需要跳过的文件名（支持 `*` `?` 通配）
    :param force:        True 时即使本地文件已存在也重新生成/下载
    :param max_depth:    递归深度保护，防止异常软链或指标环导致无限递归
    :param base_path:    账号的 base_path。OpenList 的 API 会自动拼接该前缀，
                         但 `/d/` 直链不会，因此生成 URL 时必须显式补上。
    :param cache:        目录树缓存。启用后 mtime 未变的目录可跳过请求。
    :param workers:      并发线程数；1 表示退回串行。
    """
    videos = [e.lower() for e in (video_ext or DEFAULT_VIDEO_EXT)]
    downloads = [e.lower() for e in (download_ext or DEFAULT_DOWNLOAD_EXT)]
    # 跳过规则：内置默认 + 用户追加，两者合并
    dir_blacklist = list(DEFAULT_SKIP_DIRS) + list(skip_dirs or [])
    file_patterns = compile_globs(list(DEFAULT_SKIP_FILES) + list(skip_files or []))
    result = ScanResult()
    base = normalize_remote_path(base_path or "/")

    ctx = _ScanContext(
        client=client,
        videos=videos,
        downloads=downloads,
        dir_blacklist=dir_blacklist,
        file_patterns=file_patterns,
        dir_password=dir_password,
        cache=cache,
        result=result,
    )

    for rule in rules:
        if should_cancel and should_cancel():
            result.cancelled = True
            return result

        # API 请求路径：账号 base_path 由服务端自动拼接，这里传用户填写的相对路径
        api_root = rule.remote_path
        # 直链路径：必须补上 base_path，否则 /d/ 会指向错误位置
        url_root = _with_base(base, rule.remote_path)

        # 扫描起点只受 exclude 约束，避免 include 命中不到根目录时整条规则失效
        if not rule.allows_dir(api_root):
            result.skipped_by_rule += 1
            continue

        try:
            _walk_concurrent(
                ctx=ctx,
                rule=rule,
                root_api=api_root,
                root_url=url_root,
                max_depth=max_depth,
                workers=max(1, int(workers)),
                should_cancel=should_cancel,
            )
        except OpenListError as err:
            result.errors.append(f"[{rule.remote_path}] {err}")
        except Exception as err:  # noqa: BLE001 - 单条规则失败不应中断其它规则
            result.errors.append(f"[{rule.remote_path}] 未预期错误：{type(err).__name__}: {err}")

        if result.cancelled:
            return result

    # 并发完成顺序不确定，统一排序让结果稳定
    result.planned.sort(key=lambda item: item[0])
    result.downloads.sort(key=lambda item: item[0])

    # 扫描结束后淘汰已消失目录的缓存条目
    if cache is not None:
        cache.prune(result.visited_dirs)
        result.cache = cache.stats()

    return result


@dataclass
class _ScanContext:
    """一次扫描的共享上下文（并发任务只读，各自写入独立缓冲）。"""

    client: OpenListClient
    videos: list[str]
    downloads: list[str]
    dir_blacklist: list[str]
    file_patterns: list["re.Pattern"]
    dir_password: str
    cache: Optional[TreeCache]
    result: ScanResult


@dataclass
class _DirOutcome:
    """单个目录的处理结果（在 worker 线程内构造，避免共享可变状态）。"""

    entries: list[dict] = field(default_factory=list)
    sub_dirs: list[tuple[str, str, str]] = field(default_factory=list)
    planned: list[tuple[str, str]] = field(default_factory=list)
    downloads: list[tuple[str, str]] = field(default_factory=list)
    remote_files: list[str] = field(default_factory=list)
    remote_sizes: dict[str, int] = field(default_factory=dict)
    videos: int = 0
    metas: int = 0
    files: int = 0
    skipped_dirs: int = 0
    skipped_files: int = 0
    skipped_by_rule: int = 0
    # 该目录本身是否成功获取（False 表示已计入 dirs_failed）
    ok: bool = True
    # 是否命中缓存（用于统计）
    cached: bool = False


def _process_dir(ctx: _ScanContext, rule: ScanRule, api_current: str,
                 url_current: str, depth: int, parent_mtime: Optional[str]) -> _DirOutcome:
    """获取并处理一个目录（在 worker 线程中执行，不修改共享状态）。"""
    entries = None
    if ctx.cache is not None and depth > 0:
        entries = ctx.cache.get(api_current, parent_mtime)
    if entries is None:
        entries = ctx.client.list_dir(api_current, password=ctx.dir_password)
        if ctx.cache is not None:
            ctx.cache.put(api_current, entries, parent_mtime)

    return _process_entries(ctx, rule, api_current, url_current, entries)


def _merge(ctx: _ScanContext, out: _DirOutcome) -> None:
    """把单个目录的结果并入总结果（仅在主线程调用，无需加锁）。"""
    res = ctx.result
    res.dirs_scanned += 1
    res.files_seen += out.files
    res.videos_found += out.videos
    res.metas_found += out.metas
    res.skipped_dirs += out.skipped_dirs
    res.skipped_files += out.skipped_files
    res.skipped_by_rule += out.skipped_by_rule
    res.planned.extend(out.planned)
    res.downloads.extend(out.downloads)
    res.remote_files.update(out.remote_files)
    res.remote_sizes.update(out.remote_sizes)


def _walk_concurrent(
    *,
    ctx: _ScanContext,
    rule: ScanRule,
    root_api: str,
    root_url: str,
    max_depth: int,
    workers: int,
    should_cancel: Optional[Callable[[], bool]],
) -> None:
    """按层并发遍历（BFS）。

    只有**同层目录之间**才并发：每层内部用线程池拉取，收集下一层目录后再进入下一层。
    这样既拿到了网络延迟的并行收益，又天然避免了同一目录被重复访问。

    根目录始终单独串行拉取（它没有父条目，不能走缓存，失败必须上报）。
    """
    res = ctx.result

    # 根目录：不做缓存、失败必须上报（规则不可用）
    root_entries = ctx.client.list_dir(root_api, password=ctx.dir_password)
    if ctx.cache is not None:
        ctx.cache.put(root_api, root_entries, None)
    res.visited_dirs.add(root_api)

    # 处理根目录内容（复用已取得的条目，避免重复请求）
    root_out = _process_entries(ctx, rule, root_api, root_url, root_entries)
    _merge(ctx, root_out)

    # 下一层待处理目录： (api, url, mtime, depth)
    frontier = [(a, u, m, 1) for a, u, m in root_out.sub_dirs]

    while frontier:
        if should_cancel and should_cancel():
            res.cancelled = True
            return

        # 按深度分组（同层并发），超出深度的直接报错跳过
        batch: list[tuple[str, str, str, int]] = []
        for item in frontier:
            if item[3] > max_depth:
                res.errors.append(f"超过最大递归深度 {max_depth}，已跳过：{item[0]}")
                continue
            batch.append(item)

        if not batch:
            return

        next_frontier: list[tuple[str, str, str, int]] = []

        if workers <= 1 or len(batch) == 1:
            # 串行回退路径（便于测试与限流场景）
            for api_p, url_p, mtime_p, depth_p in batch:
                if should_cancel and should_cancel():
                    res.cancelled = True
                    return
                _run_one(ctx, rule, api_p, url_p, depth_p, mtime_p,
                         next_frontier, workers)
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {
                    pool.submit(_process_dir, ctx, rule, api_p, url_p, depth_p, mtime_p):
                        (api_p, url_p, depth_p)
                    for api_p, url_p, mtime_p, depth_p in batch
                }
                for fut in as_completed(futures):
                    api_p, _url_p, depth_p = futures[fut]
                    try:
                        out = fut.result()
                    except OpenListError as err:
                        # 单个目录失败：记录并跳过，绝不中止整轮扫描
                        res.dirs_failed += 1
                        res.errors.append(f"跳过目录 {api_p}：{err}")
                        continue
                    except Exception as err:  # noqa: BLE001
                        res.dirs_failed += 1
                        res.errors.append(
                            f"跳过目录 {api_p}：未预期错误 {type(err).__name__}: {err}")
                        continue

                    res.visited_dirs.add(api_p)
                    _merge(ctx, out)
                    for a, u, m in out.sub_dirs:
                        next_frontier.append((a, u, m, depth_p + 1))

        frontier = next_frontier


def _run_one(ctx: _ScanContext, rule: ScanRule, api_p: str, url_p: str,
             depth_p: int, mtime_p: Optional[str],
             next_frontier: list[tuple[str, str, str, int]],
             workers: int) -> None:
    """串行处理一个目录并把子目录加入下一层。"""
    res = ctx.result
    try:
        out = _process_dir(ctx, rule, api_p, url_p, depth_p, mtime_p)
    except OpenListError as err:
        res.dirs_failed += 1
        res.errors.append(f"跳过目录 {api_p}：{err}")
        return
    except Exception as err:  # noqa: BLE001
        res.dirs_failed += 1
        res.errors.append(f"跳过目录 {api_p}：未预期错误 {type(err).__name__}: {err}")
        return
    res.visited_dirs.add(api_p)
    _merge(ctx, out)
    for a, u, m in out.sub_dirs:
        next_frontier.append((a, u, m, depth_p + 1))


def _process_entries(ctx: _ScanContext, rule: ScanRule, api_current: str,
                     url_current: str, entries: list[dict]) -> _DirOutcome:
    """处理已取得的条目列表（与 _process_dir 的解析部分共用）。"""
    out = _DirOutcome(entries=entries)
    for entry in entries:
        name = str(entry.get("name") or "").strip()
        if not name or name in (".", ".."):
            continue

        api_child = join_remote_path(api_current, name)
        url_child = join_remote_path(url_current, name)

        if entry.get("is_dir"):
            if not rule.allows_dir(api_child):
                out.skipped_by_rule += 1
                continue
            if should_skip_dir(name, ctx.dir_blacklist):
                out.skipped_dirs += 1
                continue
            out.sub_dirs.append((api_child, url_child, str(entry.get("modified") or "")))
            continue

        if not rule.matches(api_child):
            out.skipped_by_rule += 1
            continue
        if should_skip_file(name, (), ctx.file_patterns):
            out.skipped_files += 1
            continue

        out.files += 1
        out.remote_files.append(api_child)
        action = classify_output(name, ctx.videos, ctx.downloads)
        if action == "strm":
            out.videos += 1
            local_path = strm_target_path(rule.local_dir, api_child, rule.remote_path,
                                          replace_extension=True)
            out.planned.append((local_path, build_direct_url(ctx.client.base_url, url_child)))
        elif action == "download":
            out.metas += 1
            local_path = strm_target_path(rule.local_dir, api_child, rule.remote_path,
                                          replace_extension=False, add_strm_suffix=False)
            out.downloads.append((local_path, api_child))
            try:
                out.remote_sizes[api_child] = int(entry.get("size") or 0)
            except (TypeError, ValueError):
                out.remote_sizes[api_child] = 0
    return out


def _with_base(base_path: str, remote_path: str) -> str:
    """把账号 base_path 前缀与规则中的相对路径合成直链用的绝对路径。"""
    base = normalize_remote_path(base_path or "/")
    target = normalize_remote_path(remote_path)
    if base == "/":
        return target
    if target == "/":
        return base
    if target == base or target.startswith(base + "/"):
        return target          # 用户已自带前缀，避免重复拼接
    return normalize_remote_path(base + "/" + target.lstrip("/"))
