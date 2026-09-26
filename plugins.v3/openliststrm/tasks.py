"""多任务配置解析。

## 为什么需要多任务

单个大库（例如 795 个目录 / 1.4 万个文件）一次全量扫描耗时较长。把它拆成多个
**独立任务**，各自拥有：

- 自己的扫描规则（OpenList 路径 + 本地输出目录）
- 自己的执行周期（Cron）
- 自己的开关状态

就能让不同目录在不同时间点错峰执行，单次任务更短、更可控，也便于按库类型
（电影 / 动漫 / 网盘）分别设置策略。

## 配置存储

插件主配置中保存一个 JSON 数组字符串 `tasks`，每个元素形如：

```json
{
  "id": "task_1",
  "name": "电影库",
  "enabled": true,
  "rules": "/EmbyCloud#/volume1/video/strm/source",
  "cron": "0 30 4 * * *",
  "force_overwrite": false,
  "delete_missing": false,
  "detect_broken": false
}
```

为兼容旧版本（只有单个 `scan_rules` / `cron`），解析时会自动把旧配置包装成
一个名为「默认任务」的条目。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Optional

# 任务 ID 只允许字母数字与下划线，避免注入到页面脚本时出问题
_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")


@dataclass
class TaskConfig:
    """单个生成任务。"""

    id: str
    name: str = ""
    enabled: bool = True
    rules: str = ""
    cron: str = ""
    force_overwrite: bool = False
    delete_missing: bool = False
    detect_broken: bool = False

    @property
    def display_name(self) -> str:
        return self.name or self.id

    def to_json(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "enabled": self.enabled,
            "rules": self.rules,
            "cron": self.cron,
            "force_overwrite": self.force_overwrite,
            "delete_missing": self.delete_missing,
            "detect_broken": self.detect_broken,
        }


def parse_tasks(
    raw: Any,
    *,
    legacy_rules: str = "",
    legacy_cron: str = "",
    legacy_force: bool = False,
    legacy_delete_missing: bool = False,
) -> tuple[list[TaskConfig], list[str]]:
    """解析任务列表，返回 (任务列表, 警告信息列表)。

    :param legacy_*: 旧版单任务配置，当 `raw` 为空时用于自动迁移。
    """
    warnings: list[str] = []
    items: list[Any] = []

    text = str(raw or "").strip()
    if text:
        if text.startswith("["):
            try:
                loaded = json.loads(text)
                if isinstance(loaded, list):
                    items = loaded
                else:
                    warnings.append("tasks 配置不是数组，已忽略")
            except ValueError as err:
                warnings.append(f"tasks 配置不是合法 JSON（{err}），已忽略")
        else:
            warnings.append("tasks 配置格式无法识别，已忽略")

    tasks: list[TaskConfig] = []
    seen: set[str] = set()
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            continue
        task = _from_dict(item, index, warnings)
        if task is None:
            continue
        if task.id in seen:
            warnings.append(f"任务 ID 重复，已跳过：{task.id}")
            continue
        seen.add(task.id)
        tasks.append(task)

    # 旧配置迁移：没有任何有效任务但存在旧字段时，包装成默认任务
    if not tasks and str(legacy_rules or "").strip():
        tasks.append(TaskConfig(
            id="default",
            name="默认任务",
            enabled=True,
            rules=legacy_rules,
            cron=legacy_cron,
            force_overwrite=legacy_force,
            delete_missing=legacy_delete_missing,
        ))
        warnings.append("已把旧的单任务配置迁移为「默认任务」")

    return tasks, warnings


def _from_dict(item: dict, index: int, warnings: list[str]) -> Optional[TaskConfig]:
    """把单个字典转成 TaskConfig，非法项返回 None。"""
    task_id = str(item.get("id") or "").strip()
    if not task_id:
        task_id = f"task_{index}"
    if not _ID_RE.match(task_id):
        warnings.append(f"任务 ID 含非法字符，已跳过：{task_id}")
        return None

    rules = str(item.get("rules") or "").strip()
    if not rules:
        warnings.append(f"任务「{task_id}」没有扫描规则，已跳过")
        return None

    return TaskConfig(
        id=task_id,
        name=str(item.get("name") or "").strip(),
        enabled=bool(item.get("enabled", True)),
        rules=rules,
        cron=str(item.get("cron") or "").strip(),
        force_overwrite=bool(item.get("force_overwrite", False)),
        delete_missing=bool(item.get("delete_missing", False)),
        detect_broken=bool(item.get("detect_broken", False)),
    )


def tasks_to_json(tasks: list[TaskConfig]) -> str:
    """序列化任务列表为紧凑 JSON 字符串。"""
    return json.dumps([t.to_json() for t in tasks], ensure_ascii=False, separators=(",", ":"))


def new_task_id(existing: list[TaskConfig]) -> str:
    """生成一个未被占用的任务 ID。"""
    used = {t.id for t in existing}
    index = len(existing) + 1
    while f"task_{index}" in used:
        index += 1
    return f"task_{index}"
