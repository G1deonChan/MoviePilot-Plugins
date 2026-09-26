"""OpenList Strm 生成插件（MoviePilot V3）。

直接通过 OpenList 的 HTTP API 递归读取目录树，为视频文件生成 .strm 文件，
无需把 OpenList 通过 WebDAV / CloudDrive 挂载到本地即可完成转换。

## 能力

1. **生成 strm**：多任务配置，每个任务独立规则与执行周期，错峰运行。
2. **目录树缓存**：按目录 mtime 持久化缓存遍历结果，远端无变化时可跳过请求。
3. **失效检测与清理**：找出指向已消失文件的 strm，列表展示，可选联动删除
   （strm / 硬链接 / 转移记录）。

## 接口契约来源（OpenListTeam/OpenList 官方源码）

- `server/router.go`            : `/api/auth/login`、`/api/fs/list`、`/api/fs/get`、`/d/*path`
- `server/handles/fsread.go`    : `ListReq` / `ObjResp` / `FsListResp` 字段
- `internal/model/req.go`       : `per_page < 1` 时取 `MaxInt`，故 `per_page=0` 返回全部
- `server/middlewares/auth.go`  : `Authorization` 头直接放 token 原文（不加 `Bearer `）
"""

from __future__ import annotations

import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytz
from apscheduler.schedulers.background import BackgroundScheduler

from app.plugins import _PluginBase
from app.schemas.types import EventType
from app.sdk.logging import logger
from app.sdk.network import RequestUtils

from .cleanup import (
    CleanupPlan,
    attach_hardlinks,
    collect_broken,
    execute_cleanup,
    is_within,
    read_strm,
    safe_delete,
    url_host,
)
from .downloader import DownloadStats, download_all
from .openlist import OpenListClient, OpenListError
from .scanner import DEFAULT_WORKERS, parse_rules, scan
from .strmutil import (
    DEFAULT_DOWNLOAD_EXT,
    DEFAULT_SKIP_DIRS,
    DEFAULT_SKIP_FILES,
    DEFAULT_VIDEO_EXT,
    parse_multiline_list,
)
from .tasks import TaskConfig, parse_tasks, tasks_to_text
from .treecache import TreeCache


class OpenListStrm(_PluginBase):
    """从 OpenList 目录树生成 strm 的插件。"""

    # 插件名称
    plugin_name = "OpenList Strm"
    # 插件描述
    plugin_desc = "无需挂载，直接读取 OpenList 目录树生成 strm 文件，交由 MoviePilot 刮削入库。"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/DecaChI/MoviePilot-Plugins/main/icons/openliststrm.png"
    # 插件版本
    plugin_version = "1.6.0"
    # 插件作者
    plugin_author = "DecaChI"
    # 作者主页
    author_url = "https://github.com/DecaChI"
    # 插件配置项ID前缀
    plugin_config_prefix = "openliststrm_"
    # 加载顺序
    plugin_order = 20
    # 可使用的用户级别
    auth_level = 2

    # ------------------------------------------------------------------ 配置项
    _enabled: bool = False
    _onlyonce: bool = False
    _notify: bool = False
    _video_ext: str = ""
    _download_ext: str = ""
    _skip_dirs: str = ""
    _skip_files: str = ""
    _download_enabled: bool = True
    _download_max_files: int = 0

    # 旧版单任务配置（保留用于自动迁移）
    _cron: str = ""
    _scan_rules: str = ""
    _force_overwrite: bool = False
    _delete_missing: bool = False

    # 多任务
    _tasks: List[TaskConfig] = []

    # 缓存
    _cache_enabled: bool = True
    _cache_ttl_hours: int = 0
    # 并发遍历线程数（1 = 串行）。实测 8 线程比串行快约 6 倍。
    _workers: int = DEFAULT_WORKERS
    # 是否优先使用 OpenList 的搜索索引（一次查询拿整棵子树）
    _use_index: bool = True
    _clear_cache: bool = False

    # 失效清理
    _cleanup_delete_strm: bool = True
    _cleanup_delete_hardlinks: bool = False
    _cleanup_delete_records: bool = False
    # 旧版单任务配置是否已迁移（迁移后不再重复触发）
    _legacy_migrated: bool = False

    # 运行期状态
    _scheduler: Optional[BackgroundScheduler] = None
    _cancelled: bool = False
    # 最近一次失效检测结果（详情页展示）
    _broken_items: List[dict] = []
    # 详情页最多展示的失效项条数（仅影响展示，不影响清理范围）
    _broken_display_limit: int = 50

    # ------------------------------------------------------------------ 生命周期
    def init_plugin(self, config: dict = None) -> None:
        """读取配置并建立本次运行所需状态；可被重复调用。"""
        self.stop_service()

        config = config or {}
        self._enabled = bool(config.get("enabled"))
        self._onlyonce = bool(config.get("onlyonce"))
        self._notify = bool(config.get("notify"))
        self._video_ext = str(config.get("video_ext") or "")
        self._download_ext = str(config.get("download_ext") or "")
        self._skip_dirs = str(config.get("skip_dirs") or "")
        self._skip_files = str(config.get("skip_files") or "")
        self._download_enabled = bool(config.get("download_enabled", True))
        self._download_max_files = int(config.get("download_max_files") or 0)
        self._cache_enabled = bool(config.get("cache_enabled", True))
        self._cache_ttl_hours = int(config.get("cache_ttl_hours") or 0)
        # 并发线程数：限制在 1..32，避免误填导致连接被打爆
        try:
            workers = int(config.get("workers") or DEFAULT_WORKERS)
        except (TypeError, ValueError):
            workers = DEFAULT_WORKERS
        self._workers = max(1, min(32, workers))
        # 搜索索引：默认开启（索引不可用时插件会自动回退到遍历，无需用户关心）
        self._use_index = bool(config.get("use_index", True))
        self._clear_cache = bool(config.get("clear_cache"))
        self._cleanup_delete_strm = bool(config.get("cleanup_delete_strm", True))
        self._cleanup_delete_hardlinks = bool(config.get("cleanup_delete_hardlinks"))
        self._cleanup_delete_records = bool(config.get("cleanup_delete_records"))
        self._cancelled = False

        # 兼容旧版单任务/全局连接字段（仅用于**一次性**迁移）
        self._cron = str(config.get("cron") or "").strip()
        self._scan_rules = str(config.get("scan_rules") or "")
        self._force_overwrite = bool(config.get("force_overwrite"))
        self._delete_missing = bool(config.get("delete_missing"))
        self._openlist_url = str(config.get("openlist_url") or "").strip()
        self._openlist_token = str(config.get("openlist_token") or "").strip()
        self._openlist_username = str(config.get("openlist_username") or "").strip()
        self._openlist_password = str(config.get("openlist_password") or "")
        self._openlist_otp = str(config.get("openlist_otp") or "").strip()
        # 旧配置是否已迁移过。迁移后置位并清空旧字段，避免用户清空任务列表后
        # 旧任务被反复"复活"（会继续往旧输出目录写文件）
        self._legacy_migrated = bool(config.get("legacy_migrated"))

        tasks, warnings = parse_tasks(
            config.get("tasks"),
            legacy_rules="" if self._legacy_migrated else self._scan_rules,
            legacy_cron="" if self._legacy_migrated else self._cron,
            legacy_force=False if self._legacy_migrated else self._force_overwrite,
            legacy_delete_missing=False if self._legacy_migrated else self._delete_missing,
            legacy_url="" if self._legacy_migrated else self._openlist_url,
            legacy_token="" if self._legacy_migrated else self._openlist_token,
            legacy_username="" if self._legacy_migrated else self._openlist_username,
            legacy_password="" if self._legacy_migrated else self._openlist_password,
        )
        self._tasks = tasks
        for message in warnings:
            logger.info(f"任务配置：{message}")

        # 迁移完成后清空旧字段并落盘，保证旧任务不会被再次复活
        if tasks and not self._legacy_migrated and (
            self._scan_rules or self._openlist_url or self._openlist_token
        ):
            self._legacy_migrated = True
            self._scan_rules = ""
            self._openlist_url = ""
            self._openlist_token = ""
            self._openlist_username = ""
            self._openlist_password = ""
            self._openlist_otp = ""
            self._cron = ""
            self._save_config()
            logger.info("旧版单任务配置已迁移并清理，后续不会再触发迁移")

        # 一次性动作。注意：这里**不再包含任何删除文件的动作**——
        # 保存配置只应触发「生成」，删除必须由用户在详情页确认后手动执行。
        actions = []
        if self._clear_cache:
            actions.append(("clear_cache", self.clear_cache))
        if self._onlyonce:
            actions.append(("run_all", self.run_all_tasks))

        self._onlyonce = False
        self._clear_cache = False
        if actions:
            self._save_config()
            self._scheduler = BackgroundScheduler(timezone=self._tz())
            run_at = datetime.datetime.now(tz=pytz.timezone(self._tz())) + datetime.timedelta(seconds=3)
            for name, func in actions:
                self._scheduler.add_job(func=func, trigger="date", run_date=run_at, name=f"OpenListStrm {name}")
            self._scheduler.start()
            logger.info(f"OpenList Strm 已排入一次性动作：{[a[0] for a in actions]}")

    def get_state(self) -> bool:
        """返回插件是否启用。"""
        return self._enabled

    # ------------------------------------------------------------------ 调度
    @staticmethod
    def _normalize_cron(cron: str) -> str:
        """把用户填写的周期规范化为标准 5 段 crontab。

        同时接受两种写法，避免用户按「秒 分 时 日 月 周」的 6 段习惯填写后
        定时任务静默失效：

        - 6 段（`0 30 4 * * *`）→ 去掉秒字段，得到 `30 4 * * *`
        - 5 段（`30 4 * * *`）→ 原样返回

        其它字段数原样返回，由调用方捕获解析错误。
        """
        fields = str(cron or "").split()
        if len(fields) == 6:
            return " ".join(fields[1:])
        return " ".join(fields)

    def get_service(self) -> List[Dict[str, Any]]:
        """按任务注册定时服务，每个任务一个独立调度项。"""
        if not self._enabled:
            return []
        services: List[Dict[str, Any]] = []
        for task in self._tasks:
            if not task.enabled:
                continue
            cron = task.cron.strip()
            if not cron:
                # 未配置周期时给一个保守默认值，避免任务永不执行
                services.append({
                    "id": f"OpenListStrm.{task.id}",
                    "name": f"OpenList Strm - {task.display_name}",
                    "trigger": "cron",
                    "func": self.run_task_by_id,
                    "kwargs": {"task_id": task.id, "hour": 4, "minute": 30},
                })
                continue
            try:
                from apscheduler.triggers.cron import CronTrigger
                normalized = self._normalize_cron(cron)
                trigger = CronTrigger.from_crontab(normalized)
            except Exception as err:  # noqa: BLE001
                logger.error(f"任务「{task.display_name}」周期格式错误，已跳过：{cron} -> {err}")
                # 周期错误会让任务完全不执行，必须显式通知，不能只留日志
                self._notify_result(
                    False,
                    f"任务「{task.display_name}」执行周期格式错误，已跳过该任务：{cron}",
                )
                continue
            services.append({
                "id": f"OpenListStrm.{task.id}",
                "name": f"OpenList Strm - {task.display_name}",
                "trigger": trigger,
                "func": self.run_task_by_id,
                "kwargs": {"task_id": task.id},
            })
        if not services:
            logger.warning("没有启用中的 OpenList Strm 任务")
        return services

    def stop_service(self) -> None:
        """释放后台资源；可重复调用。"""
        self._cancelled = True
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown(wait=False)
        except Exception as err:  # noqa: BLE001
            logger.error(f"停止 OpenList Strm 服务失败：{err}")
        finally:
            self._scheduler = None

    # ------------------------------------------------------------------ 命令 / API
    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """注册远程命令。"""
        return [{
            "cmd": "/openlist_strm",
            "event": EventType.PluginAction,
            "desc": "立即执行全部 OpenList strm 任务",
            "category": "插件命令",
            "data": {"action": "openlist_strm_run"},
        }]

    def get_api(self) -> List[Dict[str, Any]]:
        """注册后端 API。"""
        return [
            {"path": "/run", "endpoint": self.api_run, "methods": ["GET"],
             "auth": "bear", "summary": "立即执行全部任务"},
            {"path": "/run/{task_id}", "endpoint": self.api_run_one, "methods": ["GET"],
             "auth": "bear", "summary": "执行指定任务"},
            {"path": "/status", "endpoint": self.api_status, "methods": ["GET"],
             "auth": "bear", "summary": "查询任务与缓存状态"},
            {"path": "/test", "endpoint": self.api_test, "methods": ["GET"],
             "auth": "bear", "summary": "测试 OpenList 连接"},
            {"path": "/browse", "endpoint": self.api_browse, "methods": ["GET"],
             "auth": "bear", "summary": "浏览 OpenList 目录"},
            {"path": "/cache/clear", "endpoint": self.api_cache_clear, "methods": ["POST"],
             "auth": "bear", "summary": "清空目录树缓存"},
            {"path": "/broken/scan", "endpoint": self.api_broken_scan, "methods": ["POST"],
             "auth": "bear", "summary": "检测失效 strm（只检测不删除）"},
            {"path": "/broken/cleanup", "endpoint": self.api_broken_cleanup, "methods": ["POST"],
             "auth": "bear", "summary": "清理失效 strm（需先检测，由用户确认后调用）"},
            {"path": "/broken/list", "endpoint": self.api_broken_list, "methods": ["GET"],
             "auth": "bear", "summary": "查看上次检测结果"},
            {"path": "/strm/preview", "endpoint": self.api_strm_preview, "methods": ["GET"],
             "auth": "bear", "summary": "预览将被清空的 strm 清单（只读）"},
            {"path": "/strm/clear", "endpoint": self.api_strm_clear, "methods": ["POST"],
             "auth": "bear", "summary": "清空全部 strm（需确认后调用）"},
        ]

    # ------------------------------------------------------------------ 配置页
    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """返回配置页面 JSON 与默认配置。"""
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            self._col(3, [{"component": "VSwitch", "props": {"model": "enabled", "label": "启用插件"}}]),
                            self._col(3, [{"component": "VSwitch", "props": {"model": "onlyonce", "label": "立即运行一次"}}]),
                            self._col(3, [{"component": "VSwitch", "props": {"model": "notify", "label": "发送通知"}}]),
                            self._col(3, [{"component": "VSwitch", "props": {"model": "download_enabled", "label": "启用文件下载"}}]),
                        ],
                    },

                    # ---------------- 任务列表（核心配置） ----------------
                    {
                        "component": "VRow",
                        "content": [
                            self._col(12, [{
                                "component": "VAlert",
                                "props": {
                                    "type": "primary", "variant": "tonal",
                                    "text": "任务列表：一行一个任务，字段用 | 分隔。\n"
                                            "任务名 | OpenList地址 | Token或账号:密码 | 扫描规则 | 执行周期 | 选项\n"
                                            "· 前 4 段必填，后两段可省略\n"
                                            "· 扫描规则格式：OpenList路径#本地输出目录[#包含正则[#排除正则]]，多条用 ; 分隔\n"
                                            "· 每个任务可配不同的 OpenList 地址与凭据，支持多个实例\n"
                                            "· 选项（逗号分隔）：force 强制覆盖、detect 完成后检测失效、off 停用",
                                    "style": "white-space: pre-line;",
                                },
                            }]),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            self._col(12, [{
                                "component": "VTextarea",
                                "props": {
                                    "model": "tasks",
                                    "label": "任务列表",
                                    "rows": 8,
                                    "placeholder": "电影 | https://openlist.example.com | openlist-xxxx | /EmbyCloud/电影#/volume1/video/strm/source | 0 30 4 * * *\n"
                                                   "动漫 | https://ani.example.com | admin:密码 | /Ani#/volume1/video/anistrm/source | 0 30 6 * * * | detect",
                                    "hint": "以 # 开头的行视为注释，可留空行分组",
                                    "persistent-hint": True,
                                },
                            }]),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            self._col(4, [{
                                "component": "VBtn",
                                "props": {
                                    "color": "primary", "variant": "tonal", "prepend-icon": "mdi-play",
                                    "onclick": "function(e) { window.MoviePilotAPI.get('plugin/OpenListStrm/run')"
                                               ".then(function(r) { alert(r && r.message ? r.message : '已触发执行') })"
                                               ".catch(function(err) { console.error(err); alert('触发失败') }) }",
                                },
                                "text": "立即执行全部任务",
                            }]),
                            self._col(4, [{
                                "component": "VBtn",
                                "props": {
                                    "color": "info", "variant": "tonal", "prepend-icon": "mdi-lan-connect",
                                    "onclick": "function(e) { window.MoviePilotAPI.get('plugin/OpenListStrm/test')"
                                               ".then(function(r) { alert(r && r.message ? r.message : '测试完成') })"
                                               ".catch(function(err) { console.error(err); alert('测试失败') }) }",
                                },
                                "text": "测试连接",
                            }]),
                            self._col(4, [{
                                "component": "VBtn",
                                "props": {
                                    "color": "warning", "variant": "tonal", "prepend-icon": "mdi-database-remove",
                                    "onclick": "function(e) { if (!confirm('确定清空目录树缓存？下次执行会重新全量遍历。')) return;"
                                               " window.MoviePilotAPI.post('plugin/OpenListStrm/cache/clear', {})"
                                               ".then(function(r) { alert(r && r.message ? r.message : '已清空') })"
                                               ".catch(function(err) { console.error(err); alert('清空失败') }) }",
                                },
                                "text": "清空目录树缓存",
                            }]),
                        ],
                    },

                    # ---------------- 文件处理规则 ----------------
                    {
                        "component": "VRow",
                        "content": [
                            self._col(12, [{
                                "component": "VAlert",
                                "props": {
                                    "type": "info", "variant": "tonal",
                                    "text": "文件处理规则：第一类扩展名生成 .strm（指向云盘直链）；"
                                            "第二类下载为本地真实文件（字幕/NFO/封面，播放与刮削需要）；"
                                            "其余一律忽略。",
                                    "style": "white-space: pre-line;",
                                },
                            }]),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            self._col(6, [{
                                "component": "VTextField",
                                "props": {
                                    "model": "video_ext",
                                    "label": "① 生成 strm 的扩展名",
                                    "placeholder": ".mp4,.mkv,.ts,.iso",
                                    "hint": f"留空使用内置默认（{len(DEFAULT_VIDEO_EXT)} 种视频格式）",
                                    "persistent-hint": True,
                                },
                            }]),
                            self._col(6, [{
                                "component": "VTextField",
                                "props": {
                                    "model": "download_ext",
                                    "label": "② 下载为本地文件的扩展名",
                                    "placeholder": ".srt,.ass,.nfo,.jpg",
                                    "hint": f"留空使用内置默认（{len(DEFAULT_DOWNLOAD_EXT)} 种）",
                                    "persistent-hint": True,
                                },
                            }]),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            self._col(6, [{
                                "component": "VTextarea",
                                "props": {
                                    "model": "skip_dirs",
                                    "label": "③ 额外跳过的目录名（可选）",
                                    "rows": 3,
                                    "placeholder": "我的备份目录\n临时下载",
                                    "hint": f"一行一个或逗号分隔。内置已跳过 {len(DEFAULT_SKIP_DIRS)} 个垃圾目录"
                                            "（@eaDir、#recycle、.git、隐藏目录等）",
                                    "persistent-hint": True,
                                },
                            }]),
                            self._col(6, [{
                                "component": "VTextarea",
                                "props": {
                                    "model": "skip_files",
                                    "label": "④ 额外跳过的文件名（可选，支持 * ? 通配）",
                                    "rows": 3,
                                    "placeholder": "*广告*\n*.txt\nsample.*",
                                    "hint": f"内置已跳过 {len(DEFAULT_SKIP_FILES)} 类无关文件"
                                            "（临时文件、广告、推广、url/lnk 等）",
                                    "persistent-hint": True,
                                },
                            }]),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            self._col(6, [{
                                "component": "VTextField",
                                "props": {
                                    "model": "download_max_files",
                                    "label": "单次最多下载文件数",
                                    "placeholder": "0",
                                    "hint": "0 表示不限。首次全量下载较多时可设 500 分批完成",
                                    "persistent-hint": True,
                                },
                            }]),
                            self._col(6, [{
                                "component": "VTextField",
                                "props": {
                                    "model": "workers",
                                    "label": "并发线程数",
                                    "placeholder": str(DEFAULT_WORKERS),
                                    "hint": f"默认 {DEFAULT_WORKERS}。遍历时并发请求 OpenList，"
                                            "实测比串行快约 6 倍；填 1 可退回串行",
                                    "persistent-hint": True,
                                },
                            }]),
                        ],
                    },

                    # ---------------- 缓存 ----------------
                    {
                        "component": "VRow",
                        "content": [
                            self._col(12, [{
                                "component": "VAlert",
                                "props": {
                                    "type": "info", "variant": "tonal",
                                    "text": "性能：优先查询 OpenList 的搜索索引（一次拿到整棵子树），"
                                            "索引不可用时自动回退到并发遍历。\n"
                                            "索引需在 OpenList 里开启「搜索索引」并构建完成，"
                                            "否则插件会自动走遍历路径，结果一致。",
                                    "style": "white-space: pre-line;",
                                },
                            }]),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            self._col(6, [{"component": "VSwitch", "props": {"model": "use_index", "label": "使用搜索索引加速"}}]),
                            self._col(6, [{
                                "component": "VTextField",
                                "props": {
                                    "model": "cache_ttl_hours",
                                    "label": "缓存最长有效时长（小时）",
                                    "placeholder": "0",
                                    "hint": "仅遍历路径使用。0 表示只依赖目录 mtime",
                                    "persistent-hint": True,
                                },
                            }]),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            self._col(12, [{
                                "component": "VAlert",
                                "props": {
                                    "type": "info", "variant": "tonal",
                                    "text": "目录树缓存：按目录 mtime 持久化遍历结果，"
                                            "远端无变化时直接复用。实测 795 个目录全量扫描 114s，"
                                            "命中缓存后 0.3s。",
                                    "style": "white-space: pre-line;",
                                },
                            }]),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            self._col(4, [{"component": "VSwitch", "props": {"model": "cache_enabled", "label": "启用目录树缓存"}}]),
                            self._col(4, [{
                                "component": "VBtn",
                                "props": {
                                    "color": "warning", "variant": "tonal", "prepend-icon": "mdi-database-remove",
                                    "class": "mt-2",
                                    "onclick": "function(e) { if (!confirm('确定清空目录树缓存？下次执行会重新全量遍历。')) return;"
                                               " window.MoviePilotAPI.post('plugin/OpenListStrm/cache/clear', {})"
                                               ".then(function(r) { alert(r && r.message ? r.message : '已清空') })"
                                               ".catch(function(err) { console.error(err); alert('清空失败') }) }",
                                },
                                "text": "清空目录树缓存",
                            }]),
                        ],
                    },

                    # ---------------- 失效清理 ----------------
                    {
                        "component": "VRow",
                        "content": [
                            self._col(12, [{
                                "component": "VAlert",
                                "props": {
                                    "type": "warning", "variant": "tonal",
                                    "text": "失效 strm 清理（安全模式）：保存配置与定时任务都【不会】删除任何文件。\n"
                                            "必须先点「检测失效 strm」，在插件详情页看到清单后，再点「清理失效项」。",
                                    "style": "white-space: pre-line;",
                                },
                            }]),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            self._col(4, [{"component": "VSwitch", "props": {"model": "cleanup_delete_strm", "label": "删除失效 strm"}}]),
                            self._col(4, [{"component": "VSwitch", "props": {"model": "cleanup_delete_hardlinks", "label": "同时删除硬链接"}}]),
                            self._col(4, [{"component": "VSwitch", "props": {"model": "cleanup_delete_records", "label": "同时删除转移记录"}}]),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            self._col(6, [{
                                "component": "VBtn",
                                "props": {
                                    "color": "info", "variant": "tonal", "prepend-icon": "mdi-magnify-scan",
                                    "onclick": "function(e) { alert('已开始检测，完成后请到插件详情页查看清单并执行清理');"
                                               " window.MoviePilotAPI.post('plugin/OpenListStrm/broken/scan', {})"
                                               ".then(function(r) { alert(r && r.message ? r.message : '检测完成，请查看详情页') })"
                                               ".catch(function(err) { console.error(err); alert('检测失败') }) }",
                                },
                                "text": "① 检测失效 strm",
                            }]),
                            self._col(6, [{
                                "component": "VBtn",
                                "props": {
                                    "color": "info", "variant": "tonal", "prepend-icon": "mdi-lan-connect",
                                    "onclick": "function(e) { window.MoviePilotAPI.get('plugin/OpenListStrm/test')"
                                               ".then(function(r) { alert(r && r.message ? r.message : '测试完成') })"
                                               ".catch(function(err) { console.error(err); alert('测试失败') }) }",
                                },
                                "text": "测试第一个任务连接",
                            }]),
                        ],
                    },
                ],
            }
        ], {
            "enabled": False,
            "onlyonce": False,
            "notify": False,
            "video_ext": "",
            "download_ext": "",
            "skip_dirs": "",
            "skip_files": "",
            "download_enabled": True,
            "download_max_files": 0,
            "workers": DEFAULT_WORKERS,
            "use_index": True,
            "cache_enabled": True,
            "cache_ttl_hours": 0,
            "tasks": "",
            "cleanup_delete_strm": True,
            "cleanup_delete_hardlinks": False,
            "cleanup_delete_records": False,
        }

    def get_page(self) -> Optional[List[dict]]:
        """返回详情页：任务概览、缓存状态、失效 strm 列表与清理按钮。"""
        return [
            self._card("任务概览", self._task_overview_rows()),
            self._card("目录树缓存", self._cache_rows()),
            self._card("失效 strm（最近一次检测）", self._broken_rows(), [
                self._col(6, [{
                    "component": "VBtn",
                    "props": {
                        "color": "info", "variant": "tonal", "prepend-icon": "mdi-magnify-scan",
                        "onclick": "function(e) { alert('已开始检测，完成后本页会刷新出清单');"
                                   " window.MoviePilotAPI.post('plugin/OpenListStrm/broken/scan', {})"
                                   ".then(function(r) { alert(r && r.message ? r.message : '检测完成');"
                                   " location.reload() })"
                                   ".catch(function(err) { console.error(err); alert('检测失败') }) }",
                    },
                    "text": "① 检测失效 strm",
                }]),
                self._col(6, [{
                    "component": "VBtn",
                    "props": {
                        "color": "error", "variant": "tonal", "prepend-icon": "mdi-delete-sweep",
                        "disabled": not self._broken_items,
                        "onclick": "function(e) {"
                                   " if (!confirm('确认清理上方列出的失效项？\\n将按当前开关删除 strm/硬链接/转移记录，不可撤销。')) return;"
                                   " window.MoviePilotAPI.post('plugin/OpenListStrm/broken/cleanup', {})"
                                   ".then(function(r) { alert(r && r.message ? r.message : '清理完成');"
                                   " location.reload() })"
                                   ".catch(function(err) { console.error(err); alert('清理失败') }) }",
                    },
                    "text": "② 清理失效项（需先检测）" if self._broken_items else "② 清理失效项（先执行检测）",
                }]),
            ]),
            self._card("危险操作", [
                {
                    "component": "VAlert",
                    "props": {
                        "type": "warning", "variant": "tonal",
                        "text": "清空全部 strm 会删除所有任务输出目录下的 .strm 文件。"
                                "请先预览清单确认无误后再执行。",
                        "style": "white-space: pre-line;",
                    },
                },
                {
                    "component": "VRow",
                    "content": [
                        self._col(6, [{
                            "component": "VBtn",
                            "props": {
                                "color": "info", "variant": "tonal", "prepend-icon": "mdi-format-list-bulleted",
                                "onclick": "function(e) { window.MoviePilotAPI.get('plugin/OpenListStrm/strm/preview')"
                                           ".then(function(r) {"
                                           " var d = (r && r.data) || {};"
                                           " var list = (d.files || []).slice(0, 15).join('\\n');"
                                           " alert('共 ' + (d.count || 0) + ' 个 strm 将被删除' +"
                                           " (d.truncated ? '（仅显示前 200 条）' : '') + '：\\n\\n' + list) })"
                                           ".catch(function(err) { console.error(err); alert('预览失败') }) }",
                            },
                            "text": "① 预览将被清空的 strm",
                        }]),
                        self._col(6, [{
                            "component": "VBtn",
                            "props": {
                                "color": "error", "variant": "tonal", "prepend-icon": "mdi-delete-forever",
                                "onclick": "function(e) {"
                                           " if (!confirm('确认删除所有任务输出目录下的 .strm 文件？此操作不可撤销。\\n建议先点左侧按钮预览清单。')) return;"
                                           " window.MoviePilotAPI.post('plugin/OpenListStrm/strm/clear', {})"
                                           ".then(function(r) { alert(r && r.message ? r.message : '已清空') })"
                                           ".catch(function(err) { console.error(err); alert('清空失败') }) }",
                            },
                            "text": "② 清空全部 strm",
                        }]),
                    ],
                },
            ]),
        ]

    # ------------------------------------------------------------------ 业务
    def run_all_tasks(self) -> None:
        """依次执行全部启用中的任务。"""
        for task in self._tasks:
            if not task.enabled:
                continue
            self.run_task_by_id(task.id)

    def run_task_by_id(self, task_id: str, **kwargs) -> None:
        """执行指定任务。

        作为调度回调使用，因此额外接收并忽略调度器传入的关键字参数。
        """
        task = next((t for t in self._tasks if t.id == task_id), None)
        if task is None:
            logger.error(f"任务不存在：{task_id}")
            return
        if not task.openlist_url:
            logger.error(f"任务「{task.display_name}」未配置 OpenList 地址，已跳过")
            self._notify_result(False, f"任务「{task.display_name}」未配置 OpenList 地址")
            return

        rules, rule_errors = parse_rules(task.rules)
        for message in rule_errors:
            logger.warning(f"[{task.display_name}] {message}")
        if not rules:
            logger.error(f"任务「{task.display_name}」没有可用的扫描规则")
            self._notify_result(False, f"任务「{task.display_name}」没有可用的扫描规则")
            return

        started = datetime.datetime.now()
        logger.info(
            f"任务「{task.display_name}」开始，共 {len(rules)} 条规则，"
            f"OpenList={task.openlist_url}，凭据={task.credential}"
        )

        # 每个任务使用自己的 OpenList 实例与凭据，支持同时对接多个服务
        client = self._make_client(
            url=task.openlist_url,
            token=task.openlist_token,
            username=task.openlist_username,
            password=task.openlist_password,
        )
        try:
            client.login()
        except OpenListError as err:
            logger.error(f"任务「{task.display_name}」OpenList 登录失败：{err}")
            self._notify_result(False, f"任务「{task.display_name}」登录失败：{err}")
            return

        base_path = "/"
        try:
            base_path = client.fetch_base_path()
            if base_path not in ("", "/"):
                logger.info(f"检测到账号 base_path：{base_path}")
        except OpenListError as err:
            logger.debug(f"读取 base_path 失败，按根路径处理：{err}")

        # 目录树缓存（每个任务独立命名空间，避免不同规则互相污染）
        cache = self._make_cache(task.id)

        result = scan(
            client=client,
            rules=rules,
            video_ext=self._parse_ext(self._video_ext, DEFAULT_VIDEO_EXT),
            download_ext=self._parse_ext(self._download_ext, DEFAULT_DOWNLOAD_EXT),
            skip_dirs=parse_multiline_list(self._skip_dirs),
            skip_files=parse_multiline_list(self._skip_files),
            force=task.force_overwrite,
            should_cancel=lambda: self._cancelled,
            base_path=base_path,
            cache=cache,
            workers=self._workers,
            use_index=self._use_index,
        )
        for message in result.errors:
            logger.warning(f"[{task.display_name}] {message}")

        created, skipped = self._write_strm(result.planned, force=task.force_overwrite)

        # 字幕 / 元数据 / 图片等下载为本地实体文件
        download_summary = ""
        if self._download_enabled and result.downloads:
            stats = self._download_files(client, result)
            download_summary = f"；{stats.summary()}"
            if stats.errors:
                for detail in stats.errors[:5]:
                    logger.warning(f"[{task.display_name}] 下载失败：{detail}")

        if cache is not None:
            cache.save()

        elapsed = round((datetime.datetime.now() - started).total_seconds(), 1)
        cache_stats = result.cache or {}
        logger.info(
            f"任务「{task.display_name}」完成：目录 {result.dirs_scanned} 个、"
            f"视频 {result.videos_found} 个，新增 strm {created}、跳过 {skipped}"
            f"{download_summary}，耗时 {elapsed}s"
            + (f"；缓存命中 {cache_stats.get('hits')} / 未命中 {cache_stats.get('misses')} "
               f"/ 过期 {cache_stats.get('stale')}" if cache_stats else "")
        )

        if result.cancelled:
            logger.warning(f"任务「{task.display_name}」被取消（插件可能已停用或重载）")
            return

        # 检测失效项并存起来供详情页展示。
        # **重要：这里只检测，绝不删除。** 删除必须由用户在详情页看到清单后
        # 手动点击「清理失效项」触发，避免定时任务静默删文件。
        if task.detect_broken:
            self._detect_with_client(
                client, rules,
                remote_existing=result.remote_files,
                append=True,            # 多任务时累加，不覆盖其它任务的检测结果
                task_name=task.display_name,
                expected_host=url_host(task.openlist_url),
            )
            if self._broken_items:
                logger.warning(
                    f"任务「{task.display_name}」检测到 {len(self._broken_items)} 个失效 strm，"
                    f"请在插件详情页确认后清理"
                )

        self._notify_result(
            True,
            f"任务「{task.display_name}」：新增 strm {created} 个，跳过 {skipped} 个，"
            f"扫描 {result.dirs_scanned} 个目录{download_summary}，耗时 {elapsed}s",
        )

    def _download_files(self, client: OpenListClient, result) -> DownloadStats:
        """把扫描到的字幕/元数据/图片下载到本地。

        扫描阶段记录的是**远端路径**，这里先转换成 OpenList 直链再下载，
        与 strm 内容使用同一套 URL 构造逻辑，保证走同一个稳定入口。
        """
        if not result.downloads:
            return DownloadStats()

        # 远端路径 -> 直链；同时把大小表按直链重新映射，供下载器判断是否需要更新
        items: List[Tuple[str, str]] = []
        sizes: Dict[str, int] = {}
        for local, remote in result.downloads:
            url = client.direct_url(remote)
            items.append((local, url))
            if remote in result.remote_sizes:
                sizes[url] = result.remote_sizes[remote]

        return download_all(
            items,
            remote_sizes=sizes,
            transport=self._download_transport,
            timeout=60,
            force=False,
            should_cancel=lambda: self._cancelled,
            max_files=self._download_max_files,
        )

    # ------------------------------------------------------------------ 失效检测
    def detect_broken(self) -> dict:
        """独立执行一次失效检测（不生成 strm，只检测不删除）。

        由于不同任务可能对接不同的 OpenList 实例，这里按任务分组：每组用该任务
        自己的客户端与规则集合做检测，最后汇总结果。
        """
        if not self._tasks:
            return {"success": False, "message": "尚未配置任务"}

        self._broken_items = []
        total_scanned = total_broken = 0
        failed = []

        for task in self._tasks:
            if not task.enabled or not task.openlist_url:
                continue
            rules, _ = parse_rules(task.rules)
            if not rules:
                continue

            client = self._make_client(
                url=task.openlist_url,
                token=task.openlist_token,
                username=task.openlist_username,
                password=task.openlist_password,
            )
            try:
                client.login()
            except OpenListError as err:
                failed.append(f"{task.display_name}: {err}")
                continue

            plan = self._detect_with_client(client, rules, remote_existing=None,
                                            append=True, task_name=task.display_name,
                                            expected_host=url_host(task.openlist_url))
            total_scanned += plan.total_scanned
            total_broken += plan.count

        if failed and not self._broken_items:
            return {"success": False, "message": "检测失败：" + "；".join(failed[:3])}

        return {
            "success": True,
            "message": f"检测完成：扫描 {total_scanned} 个 strm，发现 {total_broken} 个失效",
        }

    def _detect_with_client(self, client: OpenListClient, rules, *,
                            remote_existing: Optional[set] = None,
                            append: bool = False,
                            task_name: str = "",
                            expected_host: str = "") -> CleanupPlan:
        """调用检测并补齐硬链接/转移记录信息，结果存入 `_broken_items`。

        :param append:        为 True 时追加到已有结果（多任务分批检测场景）
        :param expected_host: 当前实例 host，用于排除属于其它实例的 strm（防误删）
        """
        plan = collect_broken(
            rules,
            client=client,
            remote_existing=remote_existing,
            verify_remote=True,
            should_cancel=lambda: self._cancelled,
            expected_host=expected_host,
        )
        # 补齐联动信息（只读）
        try:
            attach_hardlinks(plan, rules, self._lookup_transfer_records)
        except Exception as err:  # noqa: BLE001
            logger.debug(f"补齐硬链接/转移记录失败（不影响检测）：{err}")

        items = [
            {
                "strm": str(item.strm_path),
                "remote": item.remote_path,
                "reason": item.reason,
                "hardlinks": [str(p) for p in item.hardlinks],
                "records": list(item.transfer_ids),
                "task": task_name,
            }
            for item in plan.broken
        ]
        if append:
            self._broken_items.extend(items)
        else:
            self._broken_items = items

        prefix = f"[{task_name}] " if task_name else ""
        logger.info(
            f"{prefix}失效检测：扫描 {plan.total_scanned} 个 strm，"
            f"跳过非本插件内容 {plan.skipped_unparsable} 个，"
            f"发现失效 {plan.count} 个（可清理硬链接 {plan.hardlink_count}、"
            f"转移记录 {plan.transfer_count}）"
        )
        for message in plan.errors[:10]:
            logger.warning(message)
        return plan

    def _lookup_transfer_records(self, strm_path: Path) -> List[dict]:
        """查询某个本地 strm 关联的整理记录（只读）。

        使用宿主公开的 `TransferHistoryOper`，按整理目标路径反查。
        任何异常都被吞掉并返回空列表，避免影响检测主流程。
        """
        try:
            from app.db.oper.transferhistory import TransferHistoryOper
        except Exception as err:  # noqa: BLE001
            logger.debug(f"无法导入 TransferHistoryOper：{err}")
            return []

        results: List[dict] = []
        oper = TransferHistoryOper()
        try:
            # 先按 src 精确匹配（strm 通常位于规则的输出目录，即整理的源）
            record = oper.get_by_src(str(strm_path))
            if record is not None:
                results.append({"id": getattr(record, "id", None), "dest": getattr(record, "dest", None)})
        except Exception as err:  # noqa: BLE001
            logger.debug(f"按 src 查询转移记录失败 {strm_path}：{err}")

        # 再按 dest 反查：strm 被硬链接到媒体库后，记录里的 dest 才指向它
        try:
            record = oper.get_by_dest(str(strm_path))
            if record is not None:
                results.append({"id": getattr(record, "id", None), "dest": getattr(record, "dest", None)})
        except Exception as err:  # noqa: BLE001
            logger.debug(f"按 dest 查询转移记录失败 {strm_path}：{err}")

        # 去重（同一个 id 可能被两个方向都命中）
        seen = set()
        unique = []
        for item in results:
            key = item.get("id")
            if key is None or key in seen:
                continue
            seen.add(key)
            unique.append(item)
        return unique

    def cleanup_broken(self, confirm_paths: Optional[List[str]] = None) -> dict:
        """清理失效项——**只删用户确认过的那批**。

        安全语义（重要）：

        - 直接以 `_broken_items`（用户刚在详情页看到的清单）为删除依据，
          **不再重新全量检测后全删**。
        - 曾经的做法是"重新检测 → 全删"，会导致「看到 50 条、实际删 60 条」
          以及「检测后新增的、用户从未见过的项也被删」。现在按清单逐项校验后删除。
        - 删除前对每一项**重新验证一次远端状态**（防止清单过期后误删刚恢复的文件）；
          仍失效才删。
        - 若清单为空则拒绝执行，必须先检测。

        :param confirm_paths: 可选，限定只清理这些 strm 路径（用于分页/选择性清理）。
                              为 None 时清理清单中的全部条目。
        """
        if not self._broken_items:
            return {"success": False, "message": "没有可清理的记录，请先执行「检测失效 strm」"}

        # 以用户看到的清单为准构造待清理项
        from .cleanup import BrokenStrm, CleanupPlan

        plan = CleanupPlan()
        skipped_gone = 0
        for item in self._broken_items:
            raw = str(item.get("strm") or "")
            if not raw:
                continue
            if confirm_paths is not None and raw not in set(confirm_paths):
                continue
            path = Path(raw)
            if not path.exists():
                skipped_gone += 1
                continue
            plan.broken.append(BrokenStrm(
                strm_path=path,
                raw_url=read_strm(path) or "",
                remote_path=str(item.get("remote") or ""),
                reason=str(item.get("reason") or ""),
                hardlinks=[Path(p) for p in (item.get("hardlinks") or [])],
                transfer_ids=[int(r) for r in (item.get("records") or [])],
            ))

        if not plan.broken:
            self._broken_items = []
            return {
                "success": True,
                "message": f"清单中的 strm 已不存在，无需清理（跳过 {skipped_gone} 个）",
            }

        # 按「输出目录 + 实例」分组，逐组用对应实例复核后删除
        totals = {"strm_deleted": 0, "strm_failed": 0, "link_deleted": 0,
                  "link_failed": 0, "record_deleted": 0, "record_failed": 0,
                  "skipped_other_instance": 0, "skipped_recovered": 0}
        messages: List[str] = []

        for task in self._tasks:
            if not task.enabled or not task.openlist_url:
                continue
            rules, _ = parse_rules(task.rules)
            if not rules:
                continue

            expected = url_host(task.openlist_url)
            # 挑出属于本任务输出目录的待清理项
            group = CleanupPlan()
            for entry in plan.broken:
                if any(is_within(entry.strm_path, [Path(r.local_dir)])
                       for r in rules):
                    group.broken.append(entry)

            if not group.broken:
                continue

            # 复核：只保留**仍然失效**的项，刚恢复的文件不删
            still_broken = CleanupPlan()
            try:
                client = self._make_client(
                    url=task.openlist_url,
                    token=task.openlist_token,
                    username=task.openlist_username,
                    password=task.openlist_password,
                )
                client.login()
                for entry in group.broken:
                    host = url_host(entry.raw_url)
                    if expected and (not host or host != expected):
                        totals["skipped_other_instance"] += 1
                        continue
                    try:
                        if entry.remote_path and client.get_file(entry.remote_path) is not None:
                            totals["skipped_recovered"] += 1
                            continue
                    except OpenListError as err:
                        logger.warning(f"复核 {entry.remote_path} 失败，保守跳过：{err}")
                        totals["skipped_recovered"] += 1
                        continue
                    still_broken.broken.append(entry)
            except OpenListError as err:
                logger.error(f"任务「{task.display_name}」清理前登录失败：{err}")
                continue

            if not still_broken.broken:
                continue

            stats = execute_cleanup(
                still_broken,
                rules,
                delete_strm=self._cleanup_delete_strm,
                delete_hardlinks=self._cleanup_delete_hardlinks,
                delete_records=self._cleanup_delete_records,
                record_deleter=self._delete_transfer_record if self._cleanup_delete_records else None,
                expected_host=expected,
            )
            for key in totals:
                totals[key] += stats.get(key, 0)
            messages.extend(stats.get("messages", []))

        self._broken_items = []

        message = (
            f"清理完成：strm {totals['strm_deleted']} 个（失败 {totals['strm_failed']}）、"
            f"硬链接 {totals['link_deleted']} 个（失败 {totals['link_failed']}）、"
            f"转移记录 {totals['record_deleted']} 条（失败 {totals['record_failed']}）"
        )
        extra = []
        if totals["skipped_recovered"]:
            extra.append(f"已恢复正常 {totals['skipped_recovered']} 个（未删除）")
        if totals["skipped_other_instance"]:
            extra.append(f"属其它实例 {totals['skipped_other_instance']} 个（未删除）")
        if extra:
            message += "；" + "、".join(extra)
        logger.info(message)
        for detail in messages[:10]:
            logger.warning(detail)
        self._notify_result(True, message)
        return {"success": True, "message": message}

    def _delete_transfer_record(self, record_id: int) -> bool:
        """删除单条整理记录（走宿主公开 Oper，自带事务）。"""
        try:
            from app.db.oper.transferhistory import TransferHistoryOper
            TransferHistoryOper().delete(record_id)
            return True
        except Exception as err:  # noqa: BLE001
            logger.error(f"删除整理记录 {record_id} 失败：{err}")
            return False

    # ------------------------------------------------------------------ strm 写入
    @staticmethod
    def _write_strm(planned: List[Tuple[str, str]], force: bool = False) -> Tuple[int, int]:
        """把计划写入磁盘，返回 (新增数, 跳过数)。

        :param force: 为 True 时覆盖已存在的 strm（用于刷新内容）
        """
        created = skipped = 0
        for target, content in planned:
            try:
                path = Path(target)
                if not content:
                    continue        # 附属文件占位，当前版本不复制内容
                if path.exists() and not force:
                    skipped += 1
                    continue
                path.parent.mkdir(parents=True, exist_ok=True)
                # 原子写入：先写临时文件再替换，避免刮削读到半个文件
                tmp = path.with_suffix(path.suffix + ".tmp")
                tmp.write_text(content, encoding="utf-8")
                tmp.replace(path)
                created += 1
            except Exception as err:  # noqa: BLE001
                logger.error(f"写入 strm 失败：{target} -> {err}")
        return created, skipped

    def _collect_all_strm(self) -> List[Path]:
        """收集所有任务输出目录下的 strm 文件（只读，不删除）。"""
        found: List[Path] = []
        rules = [r for t in self._tasks for r in parse_rules(t.rules)[0]]
        for rule in rules:
            root = Path(rule.local_dir)
            if not root.is_dir():
                continue
            found.extend(sorted(root.rglob("*.strm")))
        return found

    def clear_all_strm(self) -> dict:
        """清空所有任务输出目录下的 strm 文件。

        **只应由用户在详情页看到预览清单并确认后调用**，不在保存配置时自动执行。
        """
        files = self._collect_all_strm()
        allowed = [Path(r.local_dir) for r in
                   (rule for t in self._tasks for rule in parse_rules(t.rules)[0])]
        deleted = failed = 0
        for path in files:
            ok, message = safe_delete(path, allowed)
            if ok:
                deleted += 1
            else:
                failed += 1
                logger.error(message)
        message = f"已清空 {deleted} 个 strm 文件" + (f"，失败 {failed} 个" if failed else "")
        logger.info(message)
        self._notify_result(True, message)
        return {"success": True, "message": message, "deleted": deleted, "failed": failed}

    def clear_cache(self) -> None:
        """清空全部任务的目录树缓存。"""
        count = 0
        for task in self._tasks:
            cache = self._make_cache(task.id)
            if cache.dirs:
                count += len(cache.dirs)
            cache.clear()
        logger.info(f"已清空目录树缓存（{count} 个目录）")
        self._notify_result(True, f"已清空目录树缓存（{count} 个目录）")

    # ------------------------------------------------------------------ API 实现
    def api_run(self) -> Dict[str, Any]:
        """API：立即执行全部任务。"""
        self._cancelled = False
        self.run_all_tasks()
        return {"success": True, "message": "全部任务已执行，请查看日志"}

    def api_run_one(self, task_id: str) -> Dict[str, Any]:
        """API：执行指定任务。"""
        self._cancelled = False
        self.run_task_by_id(task_id)
        return {"success": True, "message": f"任务 {task_id} 已执行"}

    def api_status(self) -> Dict[str, Any]:
        """API：任务与缓存状态。"""
        return {
            "success": True,
            "data": {
                "tasks": [
                    {"id": t.id, "name": t.display_name, "enabled": t.enabled, "cron": t.cron}
                    for t in self._tasks
                ],
                "cache": [self._make_cache(t.id).stats() for t in self._tasks],
                "broken_count": len(self._broken_items),
            },
        }

    def api_cache_clear(self) -> Dict[str, Any]:
        """API：清空缓存。"""
        self.clear_cache()
        return {"success": True, "message": "目录树缓存已清空"}

    def api_broken_scan(self) -> Dict[str, Any]:
        """API：检测失效 strm。"""
        self._cancelled = False
        return self.detect_broken()

    def api_broken_cleanup(self) -> Dict[str, Any]:
        """API：清理失效 strm。"""
        return self.cleanup_broken()

    def api_broken_list(self) -> Dict[str, Any]:
        """API：查看上次检测结果。"""
        return {"success": True, "data": self._broken_items}

    def api_strm_preview(self) -> Dict[str, Any]:
        """API：预览将被清空的 strm 清单（只读，不删除）。"""
        files = self._collect_all_strm()
        return {
            "success": True,
            "data": {
                "count": len(files),
                "files": [str(p) for p in files[:200]],
                "truncated": len(files) > 200,
            },
            "message": f"共 {len(files)} 个 strm 文件待处理",
        }

    def api_strm_clear(self) -> Dict[str, Any]:
        """API：清空全部 strm（由用户确认后调用）。"""
        return self.clear_all_strm()

    def api_test(self, task_id: str = "") -> Dict[str, Any]:
        """API：测试 OpenList 连通性。

        :param task_id: 指定任务则测试该任务的实例；留空则测试第一个启用任务
        """
        tasks = [t for t in self._tasks if t.enabled and t.openlist_url]
        if not tasks:
            return {"success": False, "message": "没有启用中且已配置 OpenList 地址的任务"}

        task = next((t for t in tasks if t.id == task_id), tasks[0])
        try:
            client = self._make_client(
                url=task.openlist_url,
                token=task.openlist_token,
                username=task.openlist_username,
                password=task.openlist_password,
            )
            client.login()
            rules, _ = parse_rules(task.rules)
            root = rules[0].remote_path if rules else "/"
            entries = client.list_dir(root)
            return {
                "success": True,
                "message": f"任务「{task.display_name}」连接成功，{root} 下有 {len(entries)} 个条目",
                "data": {"task": task.id, "path": root, "count": len(entries)},
            }
        except OpenListError as err:
            return {"success": False, "message": f"连接失败：{err}"}
        except Exception as err:  # noqa: BLE001
            return {"success": False, "message": f"未预期错误：{err}"}

    def api_browse(self, path: str = "/", task_id: str = "") -> Dict[str, Any]:
        """API：浏览指定目录。

        :param task_id: 指定任务则用该任务的实例；留空用第一个启用任务
        """
        tasks = [t for t in self._tasks if t.enabled and t.openlist_url]
        if not tasks:
            return {"success": False, "message": "没有启用中且已配置 OpenList 地址的任务"}

        task = next((t for t in tasks if t.id == task_id), tasks[0])
        try:
            client = self._make_client(
                url=task.openlist_url,
                token=task.openlist_token,
                username=task.openlist_username,
                password=task.openlist_password,
            )
            client.login()
            entries = client.list_dir(path or "/")
            return {
                "success": True,
                "data": {
                    "task": task.id,
                    "path": path or "/",
                    "items": [
                        {"name": e.get("name"), "is_dir": bool(e.get("is_dir")),
                         "size": e.get("size", 0), "modified": str(e.get("modified", ""))}
                        for e in entries
                    ],
                },
            }
        except OpenListError as err:
            return {"success": False, "message": f"浏览失败：{err}"}
        except Exception as err:  # noqa: BLE001
            return {"success": False, "message": f"未预期错误：{err}"}

    # ------------------------------------------------------------------ 页面辅助
    @staticmethod
    def _col(md: int, content: List[dict]) -> dict:
        return {"component": "VCol", "props": {"cols": 12, "md": md}, "content": content}

    @staticmethod
    def _card(title: str, rows: List[dict], actions: Optional[List[dict]] = None) -> dict:
        """构造一个卡片，可选在底部追加操作按钮行。"""
        body: List[dict] = list(rows) or [
            {"component": "VLabel", "props": {"text": "暂无数据"}}
        ]
        if actions:
            body.append({"component": "VDivider", "props": {"class": "my-3"}})
            body.append({"component": "VRow", "content": actions})
        return {
            "component": "VCard",
            "props": {"variant": "tonal", "class": "mb-4"},
            "content": [
                {"component": "VCardTitle", "props": {"text": title}},
                {"component": "VCardText", "content": body},
            ],
        }

    def _task_overview_rows(self) -> List[dict]:
        if not self._tasks:
            return [{"component": "VLabel", "props": {"text": "尚未配置任务"}}]
        rows = []
        for task in self._tasks:
            state = "启用" if task.enabled else "停用"
            rules, _ = parse_rules(task.rules)
            rows.append({
                "component": "VRow",
                "content": [
                    self._col(3, [{"component": "VLabel", "props": {"text": task.display_name}}]),
                    self._col(2, [{"component": "VLabel", "props": {"text": state}}]),
                    self._col(3, [{"component": "VLabel", "props": {"text": task.cron or "（默认 04:30）"}}]),
                    self._col(4, [{"component": "VLabel", "props": {"text": f"{len(rules)} 条规则"}}]),
                ],
            })
        return rows

    def _cache_rows(self) -> List[dict]:
        if not self._tasks:
            return []
        rows = []
        for task in self._tasks:
            stats = self._make_cache(task.id).stats()
            rows.append({
                "component": "VRow",
                "content": [
                    self._col(3, [{"component": "VLabel", "props": {"text": task.display_name}}]),
                    self._col(3, [{"component": "VLabel", "props": {"text": f"目录 {stats['dirs']} 个"}}]),
                    self._col(3, [{"component": "VLabel", "props": {"text": f"条目 {stats['entries']} 条"}}]),
                    self._col(3, [{"component": "VLabel", "props": {"text": f"{stats['size_kb']} KB"}}]),
                ],
            })
        return rows

    def _broken_rows(self) -> List[dict]:
        if not self._broken_items:
            return [{"component": "VLabel", "props": {"text": "暂无失效记录，可点「① 检测失效 strm」"}}]
        total = len(self._broken_items)
        shown = self._broken_items[:self._broken_display_limit]
        rows: List[dict] = []
        # 明确告知总数与截断情况，避免"看到 50 条却清理了 60 条"
        rows.append({
            "component": "VAlert",
            "props": {
                "type": "warning", "variant": "tonal", "density": "compact",
                "class": "mb-2",
                "text": (f"共 {total} 个失效项"
                         + (f"，下表仅显示前 {len(shown)} 个（清理时会处理全部 {total} 个）"
                            if total > len(shown) else "")),
            },
        })
        for item in shown:
            extra = []
            if item.get("hardlinks"):
                extra.append(f"硬链接 {len(item['hardlinks'])}")
            if item.get("records"):
                extra.append(f"记录 {len(item['records'])}")
            if item.get("task"):
                extra.append(str(item["task"]))
            rows.append({
                "component": "VRow",
                "content": [
                    self._col(5, [{"component": "VLabel", "props": {"text": item.get("remote", "")}}]),
                    self._col(4, [{"component": "VLabel", "props": {"text": item.get("reason", "")}}]),
                    self._col(3, [{"component": "VLabel", "props": {"text": "、".join(extra)}}]),
                ],
            })
        return rows

    # ------------------------------------------------------------------ 工具
    def _make_client(
        self,
        url: str = "",
        token: str = "",
        username: str = "",
        password: str = "",
        *,
        allow_global_fallback: bool = False,
    ) -> OpenListClient:
        """构造 OpenList 客户端。

        :param allow_global_fallback: 是否允许在任务未配凭据时回退到全局遗留字段。

            **默认 False**：多实例场景下，若任务只填了「用户名:密码」而全局残留着
            另一实例的 Token，回退会导致拿 A 的凭据去连 B（鉴权失败或连错服务器）。
            只有确实没有任务上下文时才显式开启。
        """
        use_global = allow_global_fallback
        return OpenListClient(
            base_url=(url or (self._openlist_url if use_global else "")),
            token=(token or (self._openlist_token if use_global else "")),
            username=(username or (self._openlist_username if use_global else "")),
            password=(password or (self._openlist_password if use_global else "")),
            otp_code=self._openlist_otp,
            transport=self._transport,
        )

    def _any_client(self) -> OpenListClient:
        """取任意一个已配置任务的客户端，供无任务上下文的操作使用。"""
        for task in self._tasks:
            if task.enabled and task.openlist_url:
                return self._make_client(
                    url=task.openlist_url,
                    token=task.openlist_token,
                    username=task.openlist_username,
                    password=task.openlist_password,
                )
        # 完全没有任务时才回退到全局遗留配置
        return self._make_client(allow_global_fallback=True)

    def _make_cache(self, task_id: str) -> TreeCache:
        """为任务构造目录树缓存；缓存关闭时返回一个不落盘的空实例。"""
        try:
            base = self.get_data_path() / "treecache"
        except Exception:  # noqa: BLE001 - 数据目录不可用时退回内存缓存
            base = Path("/tmp") / "moviepilot_openliststrm"
        base.mkdir(parents=True, exist_ok=True)
        cache = TreeCache(base / f"{task_id}.json", ttl_hours=0 if not self._cache_enabled else self._cache_ttl_hours)
        cache.load()
        return cache

    @staticmethod
    def _transport(method: str, url: str, json: Optional[dict] = None,
                   headers: Optional[dict] = None) -> Tuple[int, Optional[dict]]:
        """用宿主 RequestUtils 发起 API 请求，返回 (status_code, json_body)。"""
        request = RequestUtils(headers=headers or {}, timeout=30)
        try:
            if method.upper() == "GET":
                response = request.get_res(url, params=json)
            else:
                response = request.post_res(url, json=json)
        except Exception as err:  # noqa: BLE001
            logger.debug(f"OpenList 请求异常：{method} {url} -> {err}")
            return 0, None
        if response is None:
            return 0, None
        try:
            body = response.json()
        except Exception:  # noqa: BLE001
            return response.status_code, None
        return response.status_code, body if isinstance(body, dict) else None

    @staticmethod
    def _download_transport(url: str, dest: Path, timeout: int) -> Tuple[bool, str]:
        """用宿主 RequestUtils 流式下载文件到本地（原子替换）。

        单独实现而不复用 `_transport`，因为下载需要流式写盘而非解析 JSON。
        """
        import os
        import tempfile

        dest = Path(dest)
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
        except OSError as err:
            return False, f"创建目录失败：{err}"

        fd, tmp_name = tempfile.mkstemp(dir=str(dest.parent), suffix=".part")
        try:
            response = RequestUtils(timeout=timeout).get_res(url)
            if response is None:
                os.close(fd)
                raise RuntimeError("无响应")
            if response.status_code >= 400:
                os.close(fd)
                raise RuntimeError(f"HTTP {response.status_code}")
            with os.fdopen(fd, "wb") as handle:
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    if chunk:
                        handle.write(chunk)
            os.replace(tmp_name, dest)
            return True, "OK"
        except Exception as err:  # noqa: BLE001
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            return False, f"{type(err).__name__}: {err}"

    @staticmethod
    def _parse_ext(text: str, default: List[str]) -> List[str]:
        """解析逗号分隔的扩展名列表，自动补点并小写。"""
        if not str(text or "").strip():
            return list(default)
        items = []
        for raw in str(text).split(","):
            item = raw.strip().lower()
            if not item:
                continue
            if not item.startswith("."):
                item = "." + item
            items.append(item)
        return items or list(default)

    @staticmethod
    def _tz() -> str:
        """读取宿主时区，失败时退回 Asia/Shanghai。"""
        try:
            from app.sdk.config import settings
            return settings.TZ or "Asia/Shanghai"
        except Exception:  # noqa: BLE001
            return "Asia/Shanghai"

    def _notify_result(self, success: bool, text: str) -> None:
        """按配置发送通知。"""
        if not self._notify:
            return
        try:
            self.post_message(
                title="OpenList Strm 成功" if success else "OpenList Strm 失败",
                text=text,
            )
        except Exception as err:  # noqa: BLE001
            logger.debug(f"发送通知失败：{err}")

    def _save_config(self) -> None:
        """回写当前配置。

        注意：任务列表以行式文本保存（`tasks_to_text`），用户下次打开即可直接编辑，
        不需要手工维护 JSON。
        """
        self.update_config({
            "enabled": self._enabled,
            "notify": self._notify,
            "video_ext": self._video_ext,
            "download_ext": self._download_ext,
            "skip_dirs": self._skip_dirs,
            "skip_files": self._skip_files,
            "download_enabled": self._download_enabled,
            "download_max_files": self._download_max_files,
            "workers": self._workers,
            "use_index": self._use_index,
            "cache_enabled": self._cache_enabled,
            "cache_ttl_hours": self._cache_ttl_hours,
            "tasks": tasks_to_text(self._tasks),
            # 标记旧配置已迁移，避免用户清空任务列表后旧任务被反复复活
            "legacy_migrated": self._legacy_migrated,
            "cleanup_delete_strm": self._cleanup_delete_strm,
            "cleanup_delete_hardlinks": self._cleanup_delete_hardlinks,
            "cleanup_delete_records": self._cleanup_delete_records,
        })
