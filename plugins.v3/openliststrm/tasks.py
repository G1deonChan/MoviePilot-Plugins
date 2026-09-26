"""多任务配置解析（面向用户友好的行式格式）。

## 为什么不用 JSON

早期版本把任务存成 JSON 数组，用户需要手工写括号和引号，很容易写错。现改为
**一行一个任务、字段用 `|` 分隔**，与同类插件的习惯一致，直接在多行文本框里填写即可。

## 行格式

```
任务名 | OpenList地址 | Token或用户名:密码 | 扫描规则 | 执行周期 | 选项
```

- 只有**前 4 段必填**，后两段可省略
- `扫描规则` 内可能包含 `#`（规则自身的分隔符），因此用 `|` 作为字段分隔符
- 以 `#` 开头的整行视为注释

### 示例

```
电影 | https://openlist.example.com | openlist-xxxx | /EmbyCloud/电影#/volume1/video/strm/source | 0 30 4 * * *
动漫 | https://ani.example.com | admin:mypass | /Ani#/volume1/video/anistrm/source | 0 30 6 * * * | force
剧集 | https://openlist.example.com | openlist-yyyy | /EmbyCloud/电视剧#/volume1/video/strm/source
```

### 选项段

多个选项用逗号分隔，可用值：

| 选项 | 含义 |
| --- | --- |
| `force` | 强制覆盖已存在的 strm |
| `detect` | 本任务完成后检测失效 strm（只检测，不删除） |
| `off` / `disabled` | 停用该任务 |

## 多 OpenList 实例

每个任务自带「OpenList 地址 + 凭据」，因此可以同时对接多个不同的 OpenList 服务，
互不影响。凭据支持两种写法：

- `token`：后台「设置 → 其他」里复制的 Token（推荐）
- `用户名:密码`：插件自动登录换取 JWT

## 兼容旧配置

`parse_tasks` 同时接受旧的 JSON 数组格式与旧的单任务字段，会自动转换成新结构，
并给出迁移提示。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

# 任务名允许的字符（用于生成稳定 ID），去掉分隔符与空白
_SLUG_RE = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff]+")

# 选项关键字
_FLAG_FORCE = {"force", "overwrite", "覆盖"}
_FLAG_DETECT = {"detect", "detect_broken", "检测"}
_FLAG_OFF = {"off", "disabled", "停用", "禁用"}


@dataclass
class TaskConfig:
    """单个生成任务。

    注意：本任务**不包含任何自动删除开关**。远端文件消失后产生的死链，统一由
    「检测失效 strm → 列表展示 → 手动点清理」流程处理，避免保存配置或定时执行时
    静默删除本地文件。
    """

    id: str
    name: str = ""
    enabled: bool = True
    # 每个任务独立的 OpenList 连接信息，支持多实例
    openlist_url: str = ""
    openlist_token: str = ""
    openlist_username: str = ""
    openlist_password: str = ""
    rules: str = ""
    cron: str = ""
    force_overwrite: bool = False
    detect_broken: bool = False

    @property
    def display_name(self) -> str:
        return self.name or self.id

    @property
    def credential(self) -> str:
        """返回脱敏后的凭据描述，用于日志与页面展示。"""
        if self.openlist_token:
            token = self.openlist_token
            masked = f"{token[:12]}…{token[-4:]}" if len(token) > 20 else "已配置"
            return f"Token {masked}"
        if self.openlist_username:
            return f"账号 {self.openlist_username}"
        return "未配置凭据"

    def to_line(self) -> str:
        """序列化为一行配置文本。"""
        parts = [
            self.name or self.id,
            self.openlist_url,
            self._credential_text(),
            self._single_line_rules(),
            self.cron,
            self._flags_text(),
        ]
        # 去掉末尾空字段，让输出整洁
        while len(parts) > 4 and not parts[-1]:
            parts.pop()
        return " | ".join(parts)

    def _credential_text(self) -> str:
        """凭据字段的原始文本（Token 或 用户名:密码）。"""
        if self.openlist_token:
            return self.openlist_token
        if self.openlist_username:
            return f"{self.openlist_username}:{self.openlist_password}"
        return ""

    def _single_line_rules(self) -> str:
        """把多行规则压成一行，行间用 `;` 分隔（避免与字段分隔符冲突）。"""
        return ";".join(
            line.strip() for line in str(self.rules or "").splitlines() if line.strip()
        )

    def _flags_text(self) -> str:
        flags = []
        if not self.enabled:
            flags.append("off")
        if self.force_overwrite:
            flags.append("force")
        if self.detect_broken:
            flags.append("detect")
        return ",".join(flags)

    def to_json(self) -> dict:
        """兼容旧调用方的字典形式（不含明文密码以外的敏感转换）。"""
        return {
            "id": self.id,
            "name": self.name,
            "enabled": self.enabled,
            "openlist_url": self.openlist_url,
            "openlist_token": self.openlist_token,
            "openlist_username": self.openlist_username,
            "openlist_password": self.openlist_password,
            "rules": self.rules,
            "cron": self.cron,
            "force_overwrite": self.force_overwrite,
            "detect_broken": self.detect_broken,
        }


def _make_id(name: str, index: int, used: set[str]) -> str:
    """由任务名生成稳定且唯一的 ID。"""
    slug = _SLUG_RE.sub("_", str(name or "").strip()).strip("_").lower()
    if not slug:
        slug = f"task{index}"
    if slug[0].isdigit():
        slug = f"t{slug}"
    slug = slug[:48]
    candidate = slug
    suffix = 2
    while candidate in used:
        candidate = f"{slug}_{suffix}"
        suffix += 1
    return candidate


def parse_tasks(
    raw: Any,
    *,
    legacy_rules: str = "",
    legacy_cron: str = "",
    legacy_force: bool = False,
    legacy_delete_missing: bool = False,
    legacy_url: str = "",
    legacy_token: str = "",
    legacy_username: str = "",
    legacy_password: str = "",
) -> tuple[list[TaskConfig], list[str]]:
    """解析任务配置文本。

    支持两种输入：
    - **行式格式**（当前推荐）：一行一个任务，字段用 `|` 分隔
    - **JSON 数组**（旧版）：自动识别并转换，同时提示迁移

    :param legacy_*: 更旧的单任务全局字段，在没有任何任务时用于迁移。
    """
    warnings: list[str] = []
    text = str(raw or "").strip()

    if not text:
        return _migrate_legacy(warnings, legacy_rules, legacy_cron, legacy_force,
                               legacy_delete_missing, legacy_url, legacy_token,
                               legacy_username, legacy_password)

    # 旧版 JSON 数组：自动转换。
    # 仅当形如 `[{` 时才认定为 JSON——否则 `[4K]电影 | ...` 这类以方括号开头的
    # 任务名会被误判为 JSON，解析失败后整份配置被丢弃。
    if text.startswith("[{"):
        tasks = _parse_json_tasks(text, warnings, legacy_url, legacy_token,
                                  legacy_username, legacy_password)
        if tasks:
            warnings.append("检测到旧版 JSON 任务配置，已自动转换为行式格式；建议保存后改用行式填写")
        else:
            # JSON 解析失败时回退行式解析，避免整份配置丢失
            warnings.append("JSON 任务配置解析失败，已尝试按行式格式解析")
            fallback, line_warnings = _parse_lines(text)
            warnings.extend(line_warnings)
            return fallback, warnings
        return tasks, warnings

    tasks, line_warnings = _parse_lines(text)
    warnings.extend(line_warnings)

    if not tasks:
        return _migrate_legacy(warnings, legacy_rules, legacy_cron, legacy_force,
                               legacy_delete_missing, legacy_url, legacy_token,
                               legacy_username, legacy_password)
    return tasks, warnings


def _looks_like_flags(text: str) -> bool:
    """判断一段文本是否只由已知选项组成（用于从行尾识别选项字段）。"""
    raw = str(text or "").strip()
    if not raw:
        return False
    known = _FLAG_FORCE | _FLAG_DETECT | _FLAG_OFF
    tokens = [t.strip().lower() for t in raw.replace("，", ",").split(",") if t.strip()]
    return bool(tokens) and all(t in known for t in tokens)


# cron 字段允许的字符（数字、* / - , ? 以及 L W # 等扩展符）
_CRON_CHARS_RE = re.compile(r"^[\d\*\?\/\-,#LWlw]+$")


def _looks_like_cron(text: str) -> bool:
    """判断一段文本是否像一个 crontab 表达式（5 或 6 段）。"""
    fields = str(text or "").split()
    if len(fields) not in (5, 6):
        return False
    return all(_CRON_CHARS_RE.match(f) for f in fields)


def _parse_lines(text: str) -> tuple[list[TaskConfig], list[str]]:
    """解析行式任务配置。

    采用「**以 URL 字段为锚点、从行尾倒推**」的稳健切分，而不是简单 split：

    - 任务名里含 `|`（如 `电影|剧集`）→ 锚点前的部分整体作为名字，不会被截断；
    - 扫描规则里含 `|`（正则交替，如 `\\.(mkv|mp4)$`）→ 切分后**用 `|` 重新拼回**，
      正则得以保留；
    - 行尾的周期与选项按内容特征识别（而非固定位置），因此可省略、可乱序。

    这些情况在简单 split 下会静默错位（正则丢失、cron 被污染、字段整体错位）。
    """
    tasks: list[TaskConfig] = []
    warnings: list[str] = []
    used: set[str] = set()

    for index, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        parts = [p.strip() for p in line.split("|")]

        # 锚点：第一个以 http(s):// 开头的字段就是 OpenList 地址
        url_idx = next(
            (i for i, p in enumerate(parts) if p.lower().startswith(("http://", "https://"))),
            None,
        )
        if url_idx is None:
            warnings.append(
                f"第 {index} 行找不到合法的 OpenList 地址（需以 http:// 或 https:// 开头），已跳过：{line[:60]}"
            )
            continue
        if url_idx + 1 >= len(parts):
            warnings.append(f"第 {index} 行缺少凭据字段，已跳过：{line[:60]}")
            continue

        name = "|".join(parts[:url_idx]).strip()
        url = parts[url_idx]
        credential = parts[url_idx + 1]
        rest = parts[url_idx + 2:]

        if not rest:
            warnings.append(f"第 {index} 行缺少扫描规则，已跳过：{line[:60]}")
            continue

        # 从行尾倒推选项与周期（二者都可省略）。
        # 判定顺序：先看最后一段是不是合法选项；若不是，再看是不是 cron；
        # 都不是时才在多段情况下按位置兜底（用于对拼错的选项给出明确警告）。
        flags = ""
        cron = ""
        if len(rest) >= 2 and _looks_like_flags(rest[-1]):
            flags = rest.pop()
            if len(rest) >= 2 and _looks_like_cron(rest[-1]):
                cron = rest.pop()
        elif len(rest) >= 2 and _looks_like_cron(rest[-1]):
            cron = rest.pop()
            # 周期之前若还可能是选项
            if len(rest) >= 2 and _looks_like_flags(rest[-1]):
                flags = rest.pop()
        elif len(rest) >= 3:
            # 最后一段既不是合法选项也不是 cron，但字段数偏多：
            # 按位置判定为「规则|周期|选项」，以便对拼错的选项给出警告
            flags = rest.pop()
            if _looks_like_cron(rest[-1]):
                cron = rest.pop()

        # 剩余部分就是规则；用 `|` 拼回，保留正则交替
        rules = "|".join(rest).strip()
        if not rules:
            warnings.append(f"第 {index} 行缺少扫描规则，已跳过：{line[:60]}")
            continue

        token, username, password = _parse_credential(credential)
        enabled, force, detect = _parse_flags(flags, warnings, index)

        task_id = _make_id(name, index, used)
        used.add(task_id)

        # 规则内的 `;` 还原成多行
        rules_text = "\n".join(seg.strip() for seg in rules.split(";") if seg.strip())

        tasks.append(TaskConfig(
            id=task_id,
            name=name or f"任务{index}",
            enabled=enabled,
            openlist_url=url.rstrip("/"),
            openlist_token=token,
            openlist_username=username,
            openlist_password=password,
            rules=rules_text,
            cron=cron,
            force_overwrite=force,
            detect_broken=detect,
        ))

    return tasks, warnings


def _parse_credential(credential: str) -> tuple[str, str, str]:
    """解析凭据字段，返回 (token, username, password)。

    - 含 `:` 视为「用户名:密码」
    - 否则视为 Token
    """
    text = str(credential or "").strip()
    if not text:
        return "", "", ""
    if text.startswith("openlist-"):
        return text, "", ""          # OpenList 的后台 Token 前缀，明确按 Token 处理
    if ":" in text:
        username, _, password = text.partition(":")
        return "", username.strip(), password
    return text, "", ""


def _parse_flags(flags: str, warnings: list[str], index: int) -> tuple[bool, bool, bool]:
    """解析选项段，返回 (enabled, force, detect)。"""
    enabled, force, detect = True, False, False
    text = str(flags or "").strip()
    if not text:
        return enabled, force, detect

    for raw in text.replace("，", ",").split(","):
        flag = raw.strip().lower()
        if not flag:
            continue
        if flag in _FLAG_FORCE:
            force = True
        elif flag in _FLAG_DETECT:
            detect = True
        elif flag in _FLAG_OFF:
            enabled = False
        else:
            warnings.append(f"第 {index} 行存在无法识别的选项「{raw.strip()}」，已忽略")
    return enabled, force, detect


def _parse_json_tasks(
    text: str,
    warnings: list[str],
    legacy_url: str,
    legacy_token: str,
    legacy_username: str,
    legacy_password: str,
) -> list[TaskConfig]:
    """把旧版 JSON 数组转换成新的 TaskConfig 列表。"""
    try:
        loaded = json.loads(text)
    except ValueError as err:
        warnings.append(f"任务配置不是合法 JSON（{err}），已忽略")
        return []
    if not isinstance(loaded, list):
        warnings.append("任务配置不是数组，已忽略")
        return []

    tasks: list[TaskConfig] = []
    used: set[str] = set()
    for index, item in enumerate(loaded, start=1):
        if not isinstance(item, dict):
            continue
        rules = str(item.get("rules") or "").strip()
        if not rules:
            warnings.append(f"第 {index} 个任务没有扫描规则，已跳过")
            continue

        name = str(item.get("name") or "").strip() or f"任务{index}"
        task_id = str(item.get("id") or "").strip() or _make_id(name, index, used)
        if task_id in used:
            task_id = _make_id(name, index, used)
        used.add(task_id)

        tasks.append(TaskConfig(
            id=task_id,
            name=name,
            enabled=bool(item.get("enabled", True)),
            openlist_url=str(item.get("openlist_url") or legacy_url or "").rstrip("/"),
            openlist_token=str(item.get("openlist_token") or legacy_token or ""),
            openlist_username=str(item.get("openlist_username") or legacy_username or ""),
            openlist_password=str(item.get("openlist_password") or legacy_password or ""),
            rules=rules,
            cron=str(item.get("cron") or "").strip(),
            force_overwrite=bool(item.get("force_overwrite", False)),
            detect_broken=bool(item.get("detect_broken", False)),
        ))

        if item.get("delete_missing"):
            warnings.append(
                f"任务「{name}」的「自动删除失效 strm」已取消："
                "新版只检测并列失效项，需在插件详情页确认后清理"
            )
    return tasks


def _migrate_legacy(
    warnings: list[str],
    legacy_rules: str,
    legacy_cron: str,
    legacy_force: bool,
    legacy_delete_missing: bool,
    legacy_url: str,
    legacy_token: str,
    legacy_username: str,
    legacy_password: str,
) -> tuple[list[TaskConfig], list[str]]:
    """把更旧的单任务全局配置迁移成一个任务。"""
    if not str(legacy_rules or "").strip():
        return [], warnings

    tasks = [TaskConfig(
        id="default",
        name="默认任务",
        enabled=True,
        openlist_url=str(legacy_url or "").rstrip("/"),
        openlist_token=str(legacy_token or ""),
        openlist_username=str(legacy_username or ""),
        openlist_password=str(legacy_password or ""),
        rules=legacy_rules,
        cron=legacy_cron,
        force_overwrite=legacy_force,
    )]
    warnings.append("已把旧的单任务配置迁移为「默认任务」")
    if legacy_delete_missing:
        warnings.append(
            "旧配置中的「删除源已不存在的 strm」已改为安全模式："
            "任务只检测并列出失效项，需在插件详情页手动确认后清理"
        )
    return tasks, warnings


def tasks_to_text(tasks: list[TaskConfig]) -> str:
    """把任务列表序列化为行式配置文本。"""
    return "\n".join(t.to_line() for t in tasks)


def tasks_to_json(tasks: list[TaskConfig]) -> str:
    """兼容旧调用方：仍可序列化为 JSON（内部不再使用）。"""
    return json.dumps([t.to_json() for t in tasks], ensure_ascii=False, separators=(",", ":"))


def new_task_id(existing: list[TaskConfig]) -> str:
    """生成一个未被占用的任务 ID。"""
    used = {t.id for t in existing}
    return _make_id("task", len(existing) + 1, used)
