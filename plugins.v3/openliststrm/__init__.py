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
)
from .downloader import DownloadStats, download_all
from .openlist import OpenListClient, OpenListError
from .scanner import parse_rules, scan
from .strmutil import DEFAULT_DOWNLOAD_EXT, DEFAULT_VIDEO_EXT
from .tasks import TaskConfig, parse_tasks, tasks_to_json
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
    plugin_version = "1.2.0"
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
    _openlist_url: str = ""
    _openlist_token: str = ""
    _openlist_username: str = ""
    _openlist_password: str = ""
    _openlist_otp: str = ""
    _video_ext: str = ""
    _download_ext: str = ""
    _download_enabled: bool = True
    _download_max_files: int = 0
    _clear_strm: bool = False

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
    _clear_cache: bool = False

    # 失效清理
    _cleanup_delete_strm: bool = True
    _cleanup_delete_hardlinks: bool = False
    _cleanup_delete_records: bool = False

    # 运行期状态
    _scheduler: Optional[BackgroundScheduler] = None
    _cancelled: bool = False
    # 最近一次失效检测结果（详情页展示）
    _broken_items: List[dict] = []

    # ------------------------------------------------------------------ 生命周期
    def init_plugin(self, config: dict = None) -> None:
        """读取配置并建立本次运行所需状态；可被重复调用。"""
        self.stop_service()

        config = config or {}
        self._enabled = bool(config.get("enabled"))
        self._onlyonce = bool(config.get("onlyonce"))
        self._notify = bool(config.get("notify"))
        self._openlist_url = str(config.get("openlist_url") or "").strip()
        self._openlist_token = str(config.get("openlist_token") or "").strip()
        self._openlist_username = str(config.get("openlist_username") or "").strip()
        self._openlist_password = str(config.get("openlist_password") or "")
        self._openlist_otp = str(config.get("openlist_otp") or "").strip()
        self._video_ext = str(config.get("video_ext") or "")
        self._download_ext = str(config.get("download_ext") or "")
        self._download_enabled = bool(config.get("download_enabled", True))
        self._download_max_files = int(config.get("download_max_files") or 0)
        self._clear_strm = bool(config.get("clear_strm"))
        self._cache_enabled = bool(config.get("cache_enabled", True))
        self._cache_ttl_hours = int(config.get("cache_ttl_hours") or 0)
        self._clear_cache = bool(config.get("clear_cache"))
        self._cleanup_delete_strm = bool(config.get("cleanup_delete_strm", True))
        self._cleanup_delete_hardlinks = bool(config.get("cleanup_delete_hardlinks"))
        self._cleanup_delete_records = bool(config.get("cleanup_delete_records"))
        self._cancelled = False

        # 兼容旧版单任务字段
        self._cron = str(config.get("cron") or "").strip()
        self._scan_rules = str(config.get("scan_rules") or "")
        self._force_overwrite = bool(config.get("force_overwrite"))
        self._delete_missing = bool(config.get("delete_missing"))

        tasks, warnings = parse_tasks(
            config.get("tasks"),
            legacy_rules=self._scan_rules,
            legacy_cron=self._cron,
            legacy_force=self._force_overwrite,
            legacy_delete_missing=self._delete_missing,
        )
        self._tasks = tasks
        for message in warnings:
            logger.info(f"任务配置：{message}")

        # 一次性动作
        actions = []
        if self._clear_cache:
            actions.append(("clear_cache", self.clear_cache))
        if self._clear_strm:
            actions.append(("clear_strm", self.clear_strm))
        if self._onlyonce:
            actions.append(("run_all", self.run_all_tasks))

        self._onlyonce = False
        self._clear_cache = False
        self._clear_strm = False
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
                trigger = CronTrigger.from_crontab(cron)
            except Exception as err:  # noqa: BLE001
                logger.error(f"任务「{task.display_name}」周期格式错误，已跳过：{cron} -> {err}")
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
             "auth": "bear", "summary": "检测失效 strm"},
            {"path": "/broken/cleanup", "endpoint": self.api_broken_cleanup, "methods": ["POST"],
             "auth": "bear", "summary": "清理失效 strm"},
            {"path": "/broken/list", "endpoint": self.api_broken_list, "methods": ["GET"],
             "auth": "bear", "summary": "查看上次检测结果"},
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
                    {
                        "component": "VRow",
                        "content": [
                            self._col(6, [{
                                "component": "VTextField",
                                "props": {
                                    "model": "openlist_url",
                                    "label": "OpenList 地址",
                                    "placeholder": "https://your-openlist.example.com",
                                    "hint": "填到端口为止；实例挂在子路径时需带上，不要带 /api",
                                    "persistent-hint": True,
                                },
                            }]),
                            self._col(6, [{
                                "component": "VTextField",
                                "props": {
                                    "model": "openlist_token",
                                    "label": "OpenList Token（推荐）",
                                    "placeholder": "OpenList 后台「设置 → 其他」中获取",
                                    "hint": "填了 Token 就不需要下面的用户名密码",
                                    "persistent-hint": True,
                                },
                            }]),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            self._col(4, [{"component": "VTextField", "props": {"model": "openlist_username", "label": "用户名（可选）"}}]),
                            self._col(4, [{"component": "VTextField", "props": {"model": "openlist_password", "label": "密码（可选）", "type": "password"}}]),
                            self._col(4, [{"component": "VTextField", "props": {"model": "openlist_otp", "label": "两步验证码（可选）"}}]),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            self._col(12, [{
                                "component": "VAlert",
                                "props": {
                                    "type": "info", "variant": "tonal",
                                    "text": "文件处理规则：下面第一类扩展名会生成 .strm（指向云盘直链），"
                                            "第二类会下载为本地真实文件（字幕/NFO/封面等，播放与刮削需要）。",
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
                                    "hint": f"逗号分隔，留空使用内置默认（{len(DEFAULT_VIDEO_EXT)} 种视频格式）",
                                    "persistent-hint": True,
                                },
                            }]),
                            self._col(6, [{
                                "component": "VTextField",
                                "props": {
                                    "model": "download_ext",
                                    "label": "② 下载为本地文件的扩展名",
                                    "placeholder": ".srt,.ass,.nfo,.jpg",
                                    "hint": f"字幕/元数据/图片，留空使用内置默认（{len(DEFAULT_DOWNLOAD_EXT)} 种）",
                                    "persistent-hint": True,
                                },
                            }]),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            self._col(12, [{
                                "component": "VTextField",
                                "props": {
                                    "model": "download_max_files",
                                    "label": "单次最多下载文件数",
                                    "placeholder": "0",
                                    "hint": "0 表示不限。首次全量下载文件较多时可设为 500 分批完成，避免占用过久",
                                    "persistent-hint": True,
                                },
                            }]),
                        ],
                    },

                    # ---------------- 多任务列表 ----------------
                    {
                        "component": "VRow",
                        "content": [
                            self._col(12, [{
                                "component": "VAlert",
                                "props": {
                                    "type": "info",
                                    "variant": "tonal",
                                    "text": "任务列表（每个任务可单独设置周期，错峰执行）",
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
                                    "label": "任务配置（JSON 数组，建议用下方按钮生成）",
                                    "rows": 8,
                                    "placeholder": '[{"id":"task_1","name":"电影库","enabled":true,'
                                                   '"rules":"/EmbyCloud#/volume1/video/strm/source",'
                                                   '"cron":"0 30 4 * * *"}]',
                                    "hint": "rules 每行一条扫描规则：OpenList路径#本地输出目录[#包含正则[#排除正则]]",
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

                    # ---------------- 缓存 ----------------
                    {
                        "component": "VRow",
                        "content": [
                            self._col(12, [{
                                "component": "VAlert",
                                "props": {
                                    "type": "info", "variant": "tonal",
                                    "text": "目录树缓存：按目录 mtime 持久化遍历结果。远端无变化时直接复用，"
                                            "大幅减少请求；目录有更新则自动重新拉取并淘汰已删除目录。",
                                    "style": "white-space: pre-line;",
                                },
                            }]),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            self._col(6, [{"component": "VSwitch", "props": {"model": "cache_enabled", "label": "启用目录树缓存"}}]),
                            self._col(6, [{
                                "component": "VTextField",
                                "props": {
                                    "model": "cache_ttl_hours",
                                    "label": "缓存最长有效时长（小时）",
                                    "placeholder": "0",
                                    "hint": "0 表示不按时间过期，完全依赖目录 mtime；若存储不维护 mtime 可设为 24 强制每日刷新",
                                    "persistent-hint": True,
                                },
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
                                    "text": "失效 strm 清理：检测指向已消失文件的 strm。所有删除都必须先执行检测、"
                                            "在下方列表中确认后再点清理；路径越界会被拒绝，远端文件永不被删除。",
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
                                    "onclick": "function(e) { alert('已开始检测，稍后查看插件详情页');"
                                               " window.MoviePilotAPI.post('plugin/OpenListStrm/broken/scan', {})"
                                               ".then(function(r) { alert(r && r.message ? r.message : '检测完成，请查看详情页') })"
                                               ".catch(function(err) { console.error(err); alert('检测失败') }) }",
                                },
                                "text": "检测失效 strm",
                            }]),
                            self._col(6, [{
                                "component": "VBtn",
                                "props": {
                                    "color": "error", "variant": "tonal", "prepend-icon": "mdi-delete-sweep",
                                    "onclick": "function(e) { if (!confirm('确定清理上次检测到的失效项？此操作按当前开关执行，不可撤销。')) return;"
                                               " window.MoviePilotAPI.post('plugin/OpenListStrm/broken/cleanup', {})"
                                               ".then(function(r) { alert(r && r.message ? r.message : '清理完成') })"
                                               ".catch(function(err) { console.error(err); alert('清理失败') }) }",
                                },
                                "text": "清理失效项",
                            }]),
                        ],
                    },
                ],
            }
        ], {
            "enabled": False,
            "onlyonce": False,
            "notify": False,
            "openlist_url": "",
            "openlist_token": "",
            "openlist_username": "",
            "openlist_password": "",
            "openlist_otp": "",
            "video_ext": "",
            "download_ext": "",
            "download_enabled": True,
            "download_max_files": 0,
            "cache_enabled": True,
            "cache_ttl_hours": 0,
            "tasks": "",
            "cleanup_delete_strm": True,
            "cleanup_delete_hardlinks": False,
            "cleanup_delete_records": False,
        }

    def get_page(self) -> Optional[List[dict]]:
        """返回详情页：任务概览、缓存状态、失效 strm 列表。"""
        content: List[dict] = [
            self._card("任务概览", self._task_overview_rows()),
            self._card("目录树缓存", self._cache_rows()),
            self._card("失效 strm（最近一次检测）", self._broken_rows()),
        ]
        return content

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
        if not self._openlist_url:
            logger.error("未配置 OpenList 地址，任务终止")
            return

        rules, rule_errors = parse_rules(task.rules)
        for message in rule_errors:
            logger.warning(f"[{task.display_name}] {message}")
        if not rules:
            logger.error(f"任务「{task.display_name}」没有可用的扫描规则")
            self._notify_result(False, f"任务「{task.display_name}」没有可用的扫描规则")
            return

        started = datetime.datetime.now()
        logger.info(f"任务「{task.display_name}」开始，共 {len(rules)} 条规则")

        client = self._make_client()
        try:
            client.login()
        except OpenListError as err:
            logger.error(f"OpenList 登录失败：{err}")
            self._notify_result(False, f"OpenList 登录失败：{err}")
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
            force=task.force_overwrite,
            should_cancel=lambda: self._cancelled,
            base_path=base_path,
            cache=cache,
        )
        for message in result.errors:
            logger.warning(f"[{task.display_name}] {message}")

        created, skipped = self._write_strm(result.planned, force=task.force_overwrite)
        removed = self._purge_missing(result, rules) if task.delete_missing else 0

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
            f"视频 {result.videos_found} 个，新增 strm {created}、跳过 {skipped}、清理 {removed}"
            f"{download_summary}，耗时 {elapsed}s"
            + (f"；缓存命中 {cache_stats.get('hits')} / 未命中 {cache_stats.get('misses')} "
               f"/ 过期 {cache_stats.get('stale')}" if cache_stats else "")
        )

        if result.cancelled:
            logger.warning(f"任务「{task.display_name}」被取消（插件可能已停用或重载）")
            return

        # 需要时顺带检测失效项（复用本次遍历结果，无需二次请求）
        if task.detect_broken:
            self._detect_with_client(client, rules, remote_existing=result.remote_files)

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
        """独立执行一次失效检测（不生成 strm）。"""
        client = self._make_client()
        try:
            client.login()
        except OpenListError as err:
            return {"success": False, "message": f"OpenList 登录失败：{err}"}
        rules = [r for t in self._tasks if t.enabled for r in parse_rules(t.rules)[0]]
        plan = self._detect_with_client(client, rules, remote_existing=None)
        return {
            "success": True,
            "message": f"检测完成：扫描 {plan.total_scanned} 个 strm，发现 {plan.count} 个失效",
        }

    def _detect_with_client(self, client: OpenListClient, rules, *,
                            remote_existing: Optional[set] = None) -> CleanupPlan:
        """调用检测并补齐硬链接/转移记录信息，结果存入 `_broken_items`。"""
        plan = collect_broken(
            rules,
            client=client,
            remote_existing=remote_existing,
            verify_remote=True,
            should_cancel=lambda: self._cancelled,
        )
        # 补齐联动信息（只读）
        try:
            attach_hardlinks(plan, rules, self._lookup_transfer_records)
        except Exception as err:  # noqa: BLE001
            logger.debug(f"补齐硬链接/转移记录失败（不影响检测）：{err}")

        self._broken_items = [
            {
                "strm": str(item.strm_path),
                "remote": item.remote_path,
                "reason": item.reason,
                "hardlinks": [str(p) for p in item.hardlinks],
                "records": list(item.transfer_ids),
            }
            for item in plan.broken
        ]
        logger.info(
            f"失效检测完成：扫描 {plan.total_scanned} 个 strm，"
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

    def cleanup_broken(self) -> dict:
        """清理上次检测到的失效项。"""
        if not self._broken_items:
            return {"success": False, "message": "没有可清理的记录，请先执行检测"}

        rules = [r for t in self._tasks if t.enabled for r in parse_rules(t.rules)[0]]
        client = self._make_client()
        plan = collect_broken(rules, client=client, remote_existing=None, verify_remote=True)
        attach_hardlinks(plan, rules, self._lookup_transfer_records)

        if plan.count == 0:
            self._broken_items = []
            return {"success": True, "message": "未发现失效项，无需清理"}

        stats = execute_cleanup(
            plan,
            rules,
            delete_strm=self._cleanup_delete_strm,
            delete_hardlinks=self._cleanup_delete_hardlinks,
            delete_records=self._cleanup_delete_records,
            record_deleter=self._delete_transfer_record if self._cleanup_delete_records else None,
        )
        self._broken_items = []
        message = (
            f"清理完成：strm {stats['strm_deleted']} 个（失败 {stats['strm_failed']}）、"
            f"硬链接 {stats['link_deleted']} 个（失败 {stats['link_failed']}）、"
            f"转移记录 {stats['record_deleted']} 条（失败 {stats['record_failed']}）"
        )
        logger.info(message)
        for detail in stats["messages"][:10]:
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

    def _purge_missing(self, result, rules) -> int:
        """删除本地存在但远端已不存在的 strm。"""
        wanted = {str(Path(p)) for p, _ in result.planned}
        removed = 0
        for rule in rules:
            root = Path(rule.local_dir)
            if not root.is_dir():
                continue
            for existing in root.rglob("*.strm"):
                if str(existing) not in wanted:
                    try:
                        existing.unlink()
                        removed += 1
                    except OSError as err:
                        logger.debug(f"删除失效 strm 失败：{existing} -> {err}")
        return removed

    def clear_strm(self) -> None:
        """清空所有任务输出目录下的 strm 文件。"""
        rules = [r for t in self._tasks for r in parse_rules(t.rules)[0]]
        count = 0
        for rule in rules:
            root = Path(rule.local_dir)
            if not root.is_dir():
                continue
            for existing in root.rglob("*.strm"):
                try:
                    existing.unlink()
                    count += 1
                except OSError as err:
                    logger.error(f"删除失败：{existing} -> {err}")
        logger.info(f"已清空 {count} 个 strm 文件")
        self._notify_result(True, f"已清空 {count} 个 strm 文件")

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

    def api_test(self) -> Dict[str, Any]:
        """API：测试 OpenList 连通性。"""
        try:
            client = self._make_client()
            client.login()
            tasks = [t for t in self._tasks if t.enabled]
            if not tasks:
                return {"success": False, "message": "没有启用中的任务"}
            rules, _ = parse_rules(tasks[0].rules)
            root = rules[0].remote_path if rules else "/"
            entries = client.list_dir(root)
            return {
                "success": True,
                "message": f"连接成功，{root} 下有 {len(entries)} 个条目",
                "data": {"path": root, "count": len(entries)},
            }
        except OpenListError as err:
            return {"success": False, "message": f"连接失败：{err}"}
        except Exception as err:  # noqa: BLE001
            return {"success": False, "message": f"未预期错误：{err}"}

    def api_browse(self, path: str = "/") -> Dict[str, Any]:
        """API：浏览指定目录。"""
        try:
            client = self._make_client()
            client.login()
            entries = client.list_dir(path or "/")
            return {
                "success": True,
                "data": {
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
    def _card(title: str, rows: List[dict]) -> dict:
        return {
            "component": "VCard",
            "props": {"variant": "tonal", "class": "mb-4"},
            "content": [
                {"component": "VCardTitle", "props": {"text": title}},
                {"component": "VCardText", "content": rows or [
                    {"component": "VLabel", "props": {"text": "暂无数据"}}
                ]},
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
            return [{"component": "VLabel", "props": {"text": "暂无失效记录，可点「检测失效 strm」"}}]
        rows = []
        for item in self._broken_items[:50]:
            extra = []
            if item.get("hardlinks"):
                extra.append(f"硬链接 {len(item['hardlinks'])}")
            if item.get("records"):
                extra.append(f"记录 {len(item['records'])}")
            rows.append({
                "component": "VRow",
                "content": [
                    self._col(5, [{"component": "VLabel", "props": {"text": item.get("remote", "")}}]),
                    self._col(4, [{"component": "VLabel", "props": {"text": item.get("reason", "")}}]),
                    self._col(3, [{"component": "VLabel", "props": {"text": "、".join(extra)} }]),
                ],
            })
        return rows

    # ------------------------------------------------------------------ 工具
    def _make_client(self) -> OpenListClient:
        """构造 OpenList 客户端。"""
        return OpenListClient(
            base_url=self._openlist_url,
            token=self._openlist_token,
            username=self._openlist_username,
            password=self._openlist_password,
            otp_code=self._openlist_otp,
            transport=self._transport,
        )

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
        """回写当前配置。"""
        self.update_config({
            "enabled": self._enabled,
            "notify": self._notify,
            "openlist_url": self._openlist_url,
            "openlist_token": self._openlist_token,
            "openlist_username": self._openlist_username,
            "openlist_password": self._openlist_password,
            "openlist_otp": self._openlist_otp,
            "video_ext": self._video_ext,
            "download_ext": self._download_ext,
            "download_enabled": self._download_enabled,
            "download_max_files": self._download_max_files,
            "cache_enabled": self._cache_enabled,
            "cache_ttl_hours": self._cache_ttl_hours,
            "tasks": tasks_to_json(self._tasks),
            "cleanup_delete_strm": self._cleanup_delete_strm,
            "cleanup_delete_hardlinks": self._cleanup_delete_hardlinks,
            "cleanup_delete_records": self._cleanup_delete_records,
        })
