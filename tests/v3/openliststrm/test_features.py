"""OpenList Strm 插件新增能力测试：目录树缓存、多任务配置、失效清理。

沿用 test_openliststrm.py 的合成包加载方式，只加载纯逻辑模块。

运行：
    python -m pytest tests/v3/openliststrm -v
"""

import importlib.util
import json
import sys
import time
import types
from pathlib import Path

import pytest

PLUGIN_ID = "openliststrm"
PLUGIN_DIR = Path(__file__).resolve().parents[3] / "plugins.v3" / PLUGIN_ID
PKG_NAME = f"mp_plugin_{PLUGIN_ID}"


def _load():
    if PKG_NAME in sys.modules:
        return
    pkg = types.ModuleType(PKG_NAME)
    pkg.__path__ = [str(PLUGIN_DIR)]
    sys.modules[PKG_NAME] = pkg
    for sub in ("strmutil", "openlist", "treecache", "scanner", "tasks", "cleanup"):
        name = f"{PKG_NAME}.{sub}"
        spec = importlib.util.spec_from_file_location(name, PLUGIN_DIR / f"{sub}.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)


_load()

from mp_plugin_openliststrm.cleanup import (  # noqa: E402
    attach_hardlinks,
    collect_broken,
    execute_cleanup,
    is_within,
    parse_strm_url,
    safe_delete,
    same_inode,
    url_host,
)
from mp_plugin_openliststrm.openlist import OpenListClient, OpenListError  # noqa: E402
from mp_plugin_openliststrm.scanner import parse_rules, scan  # noqa: E402
from mp_plugin_openliststrm.tasks import (  # noqa: E402
    parse_tasks,
    tasks_to_text,
)
from mp_plugin_openliststrm.treecache import TreeCache  # noqa: E402

BASE = "https://openlist.example.com"
VIDEO = [".mkv", ".mp4"]


# ===================================================================== 目录树缓存
class TestTreeCache:
    def test_put_and_get(self, tmp_path):
        cache = TreeCache(tmp_path / "c.json")
        cache.put("/A", [{"name": "a.mkv"}], mtime="2026-01-01")
        assert cache.get("/A", "2026-01-01") == [{"name": "a.mkv"}]

    def test_miss_on_unknown_dir(self, tmp_path):
        cache = TreeCache(tmp_path / "c.json")
        assert cache.get("/nope") is None

    def test_stale_when_mtime_changed(self, tmp_path):
        cache = TreeCache(tmp_path / "c.json")
        cache.put("/A", [{"name": "a"}], mtime="2026-01-01")
        # 远端目录变了 -> 缓存必须视为过期
        assert cache.get("/A", "2026-02-02") is None

    def test_mtime_none_skips_comparison(self, tmp_path):
        cache = TreeCache(tmp_path / "c.json")
        cache.put("/A", [{"name": "a"}], mtime="2026-01-01")
        assert cache.get("/A") == [{"name": "a"}]

    def test_ttl_expiry(self, tmp_path):
        cache = TreeCache(tmp_path / "c.json", ttl_hours=0.0001)   # 0.36 秒
        cache.put("/A", [{"name": "a"}])
        assert cache.get("/A") is not None
        time.sleep(0.5)
        assert cache.get("/A") is None

    def test_ttl_zero_never_expires_by_time(self, tmp_path):
        cache = TreeCache(tmp_path / "c.json", ttl_hours=0)
        cache.dirs["/A"] = type(cache.dirs.get("/A"))  # 占位，下面重建
        cache.put("/A", [{"name": "a"}])
        # 人为把抓取时间设为很久以前，仍应命中（因为 TTL=0 不按时间过期）
        cache.dirs["/A"].fetched_at = time.time() - 999999
        assert cache.get("/A") is not None

    def test_persist_roundtrip(self, tmp_path):
        path = tmp_path / "c.json"
        cache = TreeCache(path)
        cache.put("/A", [{"name": "a.mkv", "is_dir": False}], mtime="2026-01-01")
        assert cache.save()
        assert path.is_file()

        reloaded = TreeCache(path)
        assert reloaded.load()
        assert reloaded.get("/A", "2026-01-01") == [{"name": "a.mkv", "is_dir": False}]

    def test_load_rejects_wrong_version(self, tmp_path):
        path = tmp_path / "c.json"
        path.write_text(json.dumps({"version": 999, "dirs": {"/A": {"entries": []}}}), encoding="utf-8")
        cache = TreeCache(path)
        assert cache.load() is False

    def test_load_survives_corrupt_file(self, tmp_path):
        path = tmp_path / "c.json"
        path.write_text("{not json", encoding="utf-8")
        cache = TreeCache(path)
        assert cache.load() is False
        assert cache.get("/A") is None

    def test_save_skipped_when_clean(self, tmp_path):
        path = tmp_path / "c.json"
        cache = TreeCache(path)
        assert cache.save() is True          # 无脏数据时不落盘
        assert not path.exists()

    def test_prune_removes_gone_dirs(self, tmp_path):
        cache = TreeCache(tmp_path / "c.json")
        cache.put("/A", [])
        cache.put("/A/keep", [])
        cache.put("/A/gone", [])
        cache.put("/Other", [])
        # 本次只访问到 /A 与 /A/keep
        removed = cache.prune({"/A", "/A/keep"})
        assert removed == 1
        assert "/A/gone" not in cache.dirs
        assert "/A/keep" in cache.dirs
        # /Other 不在扫描范围内，不应被淘汰
        assert "/Other" in cache.dirs

    def test_stats(self, tmp_path):
        cache = TreeCache(tmp_path / "c.json")
        cache.put("/A", [{"n": 1}, {"n": 2}])
        cache.get("/A")
        cache.get("/missing")
        stats = cache.stats()
        assert stats["dirs"] == 1
        assert stats["entries"] == 2
        assert stats["hits"] == 1
        assert stats["misses"] == 1

    def test_clear_removes_memory_and_file(self, tmp_path):
        path = tmp_path / "c.json"
        cache = TreeCache(path)
        cache.put("/A", [{"n": 1}])
        cache.save()
        cache.clear()
        assert cache.dirs == {}
        assert not path.exists()


class TestScanWithCache:
    def _transport(self, tree, counter):
        def _t(method, url, json=None, headers=None):
            if url.endswith("/api/fs/list"):
                counter["n"] += 1
                path = (json or {}).get("path", "/")
                if path in tree:
                    return 200, {"code": 200, "data": {"content": tree[path]}}
            return 200, {"code": 500, "message": "object not found"}
        return _t

    def test_cache_avoids_requests_on_second_scan(self, tmp_path):
        """第二次扫描时：根目录重取（感知顶层变化），子目录命中缓存。"""
        tree = {
            "/A": [{"name": "sub", "is_dir": True, "modified": "2026-01-01"},
                   {"name": "a.mkv", "is_dir": False}],
            "/A/sub": [{"name": "b.mkv", "is_dir": False}],
        }
        counter = {"n": 0}
        rules, _ = parse_rules("/A#/out")

        cache = TreeCache(tmp_path / "c.json")
        client = OpenListClient(BASE, token="t", transport=self._transport(tree, counter))
        first = scan(client, rules, video_ext=VIDEO, cache=cache)
        assert counter["n"] == 2                    # 根 + 子目录都请求了
        assert first.videos_found == 2
        cache.save()

        counter["n"] = 0
        cache2 = TreeCache(tmp_path / "c.json")
        cache2.load()
        client2 = OpenListClient(BASE, token="t", transport=self._transport(tree, counter))
        second = scan(client2, rules, video_ext=VIDEO, cache=cache2)
        assert second.videos_found == 2
        # 只请求根目录；子目录 /A/sub 的 mtime 未变，命中缓存
        assert counter["n"] == 1
        assert cache2.stats()["hits"] >= 1

    def test_root_always_refetched_to_see_top_level_changes(self, tmp_path):
        """根目录必须每次重取：否则顶层新增的文件会被缓存的旧列表挡住。"""
        tree = {"/A": [{"name": "a.mkv", "is_dir": False}]}
        rules, _ = parse_rules("/A#/out")
        cache = TreeCache(tmp_path / "c.json")
        c1 = {"n": 0}
        client = OpenListClient(BASE, token="t", transport=self._transport(tree, c1))
        scan(client, rules, video_ext=VIDEO, cache=cache)
        cache.save()
        assert c1["n"] == 1

        # 远端新增了一个文件
        tree["/A"] = [{"name": "a.mkv", "is_dir": False},
                      {"name": "new.mkv", "is_dir": False}]
        c2 = {"n": 0}
        cache = TreeCache(tmp_path / "c.json")
        cache.load()
        client = OpenListClient(BASE, token="t", transport=self._transport(tree, c2))
        result = scan(client, rules, video_ext=VIDEO, cache=cache)
        assert c2["n"] == 1                         # 重新请求了根目录
        assert result.videos_found == 2             # 新文件被发现

    def test_mtime_change_forces_refetch(self, tmp_path):
        """子目录 mtime 变化时必须重新请求该目录。"""
        tree = {
            "/A": [{"name": "sub", "is_dir": True, "modified": "2026-01-01"}],
            "/A/sub": [{"name": "b.mkv", "is_dir": False}],
        }
        rules, _ = parse_rules("/A#/out")
        c1 = {"n": 0}
        cache = TreeCache(tmp_path / "c.json")
        client = OpenListClient(BASE, token="t", transport=self._transport(tree, c1))
        scan(client, rules, video_ext=VIDEO, cache=cache)
        cache.save()
        assert c1["n"] == 2

        # 子目录 mtime 变了 -> 必须重新请求它
        tree2 = {
            "/A": [{"name": "sub", "is_dir": True, "modified": "2026-09-09"}],
            "/A/sub": [{"name": "b.mkv", "is_dir": False}, {"name": "c.mkv", "is_dir": False}],
        }
        c2 = {"n": 0}
        cache = TreeCache(tmp_path / "c.json")
        cache.load()
        client = OpenListClient(BASE, token="t", transport=self._transport(tree2, c2))
        result = scan(client, rules, video_ext=VIDEO, cache=cache)
        assert c2["n"] == 2                         # 根 + 变化的子目录
        assert result.videos_found == 2             # 新增文件被发现


# ===================================================================== 多任务配置
class TestParseTasks:
    """行式任务配置：任务名 | 地址 | 凭据 | 规则 | 周期 | 选项"""

    def test_empty_yields_no_tasks(self):
        tasks, warnings = parse_tasks("")
        assert tasks == [] and warnings == []

    def test_basic_line(self):
        line = "电影 | https://ol.example.com | openlist-tok | /A#/out | 0 0 4 * * *"
        tasks, warnings = parse_tasks(line)
        assert len(tasks) == 1
        t = tasks[0]
        assert t.name == "电影"
        assert t.openlist_url == "https://ol.example.com"
        assert t.openlist_token == "openlist-tok"
        assert t.rules == "/A#/out"
        assert t.cron == "0 0 4 * * *"
        assert t.enabled is True
        assert not warnings

    def test_minimal_four_fields(self):
        """只有前 4 段也合法，周期与选项可省略。"""
        tasks, warnings = parse_tasks("动漫 | https://a.example.com | tok | /Ani#/out")
        assert len(tasks) == 1
        assert tasks[0].cron == ""
        assert not warnings

    def test_multiple_tasks_and_comments(self):
        text = (
            "# 这是注释\n"
            "\n"
            "电影 | https://ol1.example.com | tok1 | /A#/out\n"
            "动漫 | https://ol2.example.com | tok2 | /B#/out | 0 0 6 * * *\n"
        )
        tasks, _ = parse_tasks(text)
        assert [t.name for t in tasks] == ["电影", "动漫"]
        # 每个任务有独立的服务器地址
        assert tasks[0].openlist_url == "https://ol1.example.com"
        assert tasks[1].openlist_url == "https://ol2.example.com"

    def test_username_password_credential(self):
        tasks, _ = parse_tasks("x | https://ol.example.com | admin:secret | /A#/out")
        t = tasks[0]
        assert t.openlist_token == ""
        assert t.openlist_username == "admin"
        assert t.openlist_password == "secret"

    def test_colon_password_kept_intact(self):
        """密码里含冒号时应完整保留。"""
        tasks, _ = parse_tasks("x | https://ol.example.com | admin:a:b:c | /A#/out")
        t = tasks[0]
        assert t.openlist_username == "admin"
        assert t.openlist_password == "a:b:c"

    def test_token_prefix_forces_token(self):
        """openlist- 前缀即使含冒号也按 Token 处理。"""
        tasks, _ = parse_tasks("x | https://ol.example.com | openlist-abc-1 | /A#/out")
        assert tasks[0].openlist_token == "openlist-abc-1"
        assert tasks[0].openlist_username == ""

    def test_flags(self):
        tasks, warnings = parse_tasks("x | https://ol.example.com | t | /A#/out | 0 0 4 * * * | force,detect")
        t = tasks[0]
        assert t.force_overwrite is True
        assert t.detect_broken is True
        assert t.enabled is True
        assert not warnings

    def test_off_flag_disables(self):
        tasks, _ = parse_tasks("x | https://ol.example.com | t | /A#/out | 0 0 4 * * * | off")
        assert tasks[0].enabled is False

    def test_unknown_flag_warns_but_keeps(self):
        tasks, warnings = parse_tasks("x | https://ol.example.com | t | /A#/out | 0 0 4 * * * | bogus")
        assert len(tasks) == 1
        assert any("bogus" in w for w in warnings)

    def test_insufficient_fields_skipped(self):
        tasks, warnings = parse_tasks("只有名字 | https://x.com")
        assert tasks == [] and warnings

    def test_missing_url_skipped(self):
        tasks, warnings = parse_tasks("x |  | tok | /A#/out")
        assert tasks == [] and warnings

    def test_missing_rules_skipped(self):
        tasks, warnings = parse_tasks("x | https://ol.example.com | tok | ")
        assert tasks == [] and warnings

    def test_multi_line_rules_joined_by_semicolon(self):
        """规则多条时用 ; 分隔，解析后还原为多行。"""
        tasks, _ = parse_tasks("x | https://ol.example.com | t | /A#/out1;/B#/out2")
        assert tasks[0].rules == "/A#/out1\n/B#/out2"

    def test_auto_id_from_name(self):
        tasks, _ = parse_tasks("电影库 | https://ol.example.com | t | /A#/out")
        assert tasks[0].id == "电影库"

    def test_id_collision_avoided(self):
        text = ("同名 | https://a.com | t | /A#/out\n"
                "同名 | https://b.com | t | /B#/out")
        tasks, _ = parse_tasks(text)
        assert len(tasks) == 2
        assert tasks[0].id != tasks[1].id

    def test_legacy_single_task_migration(self):
        tasks, warnings = parse_tasks(
            "", legacy_rules="/A#/out", legacy_cron="0 0 4 * * *",
            legacy_url="https://old.example.com", legacy_token="oldt",
        )
        assert len(tasks) == 1
        t = tasks[0]
        assert t.id == "default"
        assert t.rules == "/A#/out"
        assert t.cron == "0 0 4 * * *"
        assert t.openlist_url == "https://old.example.com"
        assert t.openlist_token == "oldt"
        assert any("迁移" in w for w in warnings)

    def test_legacy_delete_missing_warns_safe_mode(self):
        """旧版自动删除开关必须转成安全模式并提示。"""
        tasks, warnings = parse_tasks("", legacy_rules="/A#/out", legacy_delete_missing=True)
        assert len(tasks) == 1
        assert not hasattr(tasks[0], "delete_missing")
        assert any("安全模式" in w for w in warnings)

    def test_json_array_auto_migrated(self):
        """旧版 JSON 数组仍可解析，并提示迁移。"""
        raw = json.dumps([{"id": "t1", "name": "电影", "rules": "/A#/out", "cron": "0 0 4 * * *"}])
        tasks, warnings = parse_tasks(raw)
        assert len(tasks) == 1
        assert tasks[0].name == "电影"
        assert any("JSON" in w for w in warnings)

    def test_json_task_uses_legacy_global_credential(self):
        """旧 JSON 任务没写地址时，回退到全局配置。"""
        raw = json.dumps([{"name": "x", "rules": "/A#/out"}])
        tasks, _ = parse_tasks(raw, legacy_url="https://g.example.com", legacy_token="gt")
        assert tasks[0].openlist_url == "https://g.example.com"
        assert tasks[0].openlist_token == "gt"

    def test_invalid_json_warns(self):
        tasks, warnings = parse_tasks("{oops")
        assert tasks == [] and warnings

    def test_roundtrip_line_format(self):
        """行式格式必须可往返：解析 → 序列化 → 再解析结果一致。"""
        original = ("电影 | https://ol1.example.com | openlist-tok1 | /A#/out | 0 0 4 * * *\n"
                    "动漫 | https://ol2.example.com | admin:pw | /B#/out | 0 0 6 * * * | force,detect")
        tasks, _ = parse_tasks(original)
        again, _ = parse_tasks(tasks_to_text(tasks))
        assert len(again) == len(tasks)
        for a, b in zip(tasks, again):
            assert a.to_json() == b.to_json()

    def test_credential_masked(self):
        """凭据展示必须脱敏。"""
        tasks, _ = parse_tasks("x | https://ol.example.com | openlist-abcdefghijklmnop-qrst | /A#/out")
        shown = tasks[0].credential
        assert "abcdefghijklmnop-qrst" not in shown
        assert "Token" in shown



# ===================================================================== strm 解析
class TestParseStrmUrl:
    @pytest.mark.parametrize("content,expected", [
        ("https://h/d/Ani/01.mkv", "/Ani/01.mkv"),
        ("https://h/openlist/d/Ani/01.mkv", "/Ani/01.mkv"),
        ("https://h/d/Ani/01.mkv?sign=abc:0", "/Ani/01.mkv"),
        ("https://h:8443/d/Ani/01.mkv", "/Ani/01.mkv"),
        ("/d/Ani/01.mkv", "/Ani/01.mkv"),
        ("d/Ani/01.mkv", "/Ani/01.mkv"),
        ("\ufeffhttps://h/d/Ani/01.mkv", "/Ani/01.mkv"),
        ("https://h/d/Ani/01.mkv\n", "/Ani/01.mkv"),
    ])
    def test_variants(self, content, expected):
        parsed = parse_strm_url(content)
        assert parsed is not None
        assert parsed[2] == expected          # (host, prefix, path)

    def test_host_is_preserved(self):
        """host 必须保留：多实例下用它判断 strm 属于哪个实例，防止误删。"""
        assert parse_strm_url("https://server-a.example.com/d/Ani/01.mkv")[0] == "server-a.example.com"
        assert parse_strm_url("https://server-b.example.com/d/Ani/01.mkv")[0] == "server-b.example.com"

    def test_host_lowercased(self):
        assert parse_strm_url("https://Server-A.Example.COM/d/a.mkv")[0] == "server-a.example.com"

    def test_host_includes_port(self):
        assert parse_strm_url("https://h:8443/d/a.mkv")[0] == "h:8443"

    def test_relative_url_has_empty_host(self):
        assert parse_strm_url("/d/Ani/01.mkv")[0] == ""

    def test_percent_decoding(self):
        parsed = parse_strm_url("https://h/d/Ani/%E4%B8%AD%E6%96%87/01.mkv")
        assert parsed[2] == "/Ani/中文/01.mkv"

    def test_fullwidth_tilde_roundtrip(self):
        # 真实样本：全角波浪号必须正确解码
        url = "https://alist.decanas.top/d/Ani/2019-1/%E8%BC%9D%E5%A4%9C%E5%A7%AC%EF%BD%9E%E5%A4%A9/01.mp4"
        parsed = parse_strm_url(url)
        assert "～" in parsed[2]

    def test_prefix_returned(self):
        parsed = parse_strm_url("https://h/openlist/d/Ani/01.mkv")
        assert parsed[1] == "/openlist"

    def test_rejects_non_strm(self):
        assert parse_strm_url("") is None
        assert parse_strm_url("   ") is None
        assert parse_strm_url("just some text") is None
        assert parse_strm_url("https://h/other/path.mkv") is None
        assert parse_strm_url("/api/fs/get") is None

    def test_only_first_line(self):
        parsed = parse_strm_url("https://h/d/Ani/01.mkv\nhttps://h/d/Other/02.mkv")
        assert parsed[2] == "/Ani/01.mkv"


class TestUrlHost:
    """host 提取：用于跨实例防护。"""

    def test_basic(self):
        assert url_host("https://a.example.com") == "a.example.com"

    def test_with_port_and_subpath(self):
        assert url_host("https://a.example.com:8443/openlist") == "a.example.com:8443"

    def test_case_insensitive(self):
        assert url_host("https://A.Example.COM") == "a.example.com"

    def test_trailing_slash(self):
        assert url_host("https://a.example.com/") == "a.example.com"

    def test_empty(self):
        assert url_host("") == ""
        assert url_host(None) == ""


class TestParseTasksRobustness:
    """回归测试：审计发现的行式解析健壮性问题。

    简单按 `|` 切分并固定取第 1/2/3/4 段会导致静默错位：
    任务名含 `|` → 字段整体错位；规则含 `|`（正则交替）→ 正则被截断、cron 被污染。
    现改为「以 URL 为锚点 + 从行尾倒推」的切分。
    """

    def test_pipe_in_task_name(self):
        """任务名含 | 不能被截断，更不能导致字段错位。"""
        tasks, _ = parse_tasks("电影|剧集 | https://s1.example.com | openlist-abc | /A#/out")
        assert len(tasks) == 1
        t = tasks[0]
        assert t.name == "电影|剧集"
        assert t.openlist_url == "https://s1.example.com"      # 关键：URL 未错位
        assert t.rules == "/A#/out"
        assert t.openlist_token == "openlist-abc"

    def test_pipe_in_regex_alternation(self):
        """规则里的正则交替 \\.(mkv|mp4)$ 必须完整保留，且不污染 cron。"""
        line = r"电影 | https://s1.example.com | openlist-abc | /EmbyCloud#/out#\.(mkv|mp4)$ | 0 30 4 * * *"
        tasks, warnings = parse_tasks(line)
        assert len(tasks) == 1
        t = tasks[0]
        assert t.rules == r"/EmbyCloud#/out#\.(mkv|mp4)$", f"正则被截断：{t.rules!r}"
        assert t.cron == "0 30 4 * * *", f"cron 被污染：{t.cron!r}"
        # 正则必须能被 parse_rules 正常解析
        rules, errs = parse_rules(t.rules)
        assert rules and not errs

    def test_pipe_in_both_name_and_regex(self):
        """两处都有 | 时仍应正确。"""
        line = r"A|B | https://s1.example.com | tok | /X#/out#\.(a|b)$ | 0 0 5 * * *"
        tasks, _ = parse_tasks(line)
        t = tasks[0]
        assert t.name == "A|B"
        assert t.rules == r"/X#/out#\.(a|b)$"
        assert t.cron == "0 0 5 * * *"

    def test_name_starting_with_bracket(self):
        """任务名以 [ 开头（如 [4K]电影）不能被误判为 JSON 而丢弃整份配置。"""
        text = ("[4K]电影 | https://s1.example.com | tok1 | /A#/out\n"
                "动漫 | https://s2.example.com | tok2 | /B#/out")
        tasks, warnings = parse_tasks(text)
        assert len(tasks) == 2, f"整份配置被丢弃，warnings={warnings}"
        assert tasks[0].name == "[4K]电影"
        assert tasks[0].rules == "/A#/out"
        assert tasks[1].name == "动漫"

    def test_url_anchor_requires_scheme(self):
        """没有合法 URL 的行应被拒绝并说明原因。"""
        tasks, warnings = parse_tasks("电影 | 不是地址 | tok | /A#/out")
        assert tasks == [] and warnings
        assert any("http" in w for w in warnings)

    def test_flags_and_cron_order_variants(self):
        """周期与选项可省略、可组合。"""
        cases = [
            ("x | https://a.com | t | /A#/out", "", False, False),
            ("x | https://a.com | t | /A#/out | 0 0 4 * * *", "0 0 4 * * *", False, False),
            ("x | https://a.com | t | /A#/out | force", "", True, False),
            ("x | https://a.com | t | /A#/out | 0 0 4 * * * | force,detect", "0 0 4 * * *", True, True),
        ]
        for line, cron, force, detect in cases:
            tasks, warnings = parse_tasks(line)
            assert len(tasks) == 1, f"{line} -> {warnings}"
            t = tasks[0]
            assert t.cron == cron, f"{line}: cron={t.cron!r}"
            assert t.force_overwrite is force, f"{line}: force={t.force_overwrite}"
            assert t.detect_broken is detect, f"{line}: detect={t.detect_broken}"
            assert t.rules == "/A#/out", f"{line}: rules={t.rules!r}"


class TestLegacyMigrationOnce:
    """回归测试：旧任务不能在用户清空任务列表后反复复活。"""

    def test_migration_flag_gates_legacy(self):
        """legacy_migrated=True 时忽略旧字段，不再生成任务。"""
        tasks, _ = parse_tasks(
            "",
            legacy_rules="/Old#/old/out",
            legacy_url="https://legacy.example.com",
            legacy_token="oldt",
        )
        assert len(tasks) == 1 and tasks[0].id == "default"

        # 模拟已迁移：调用方传空 legacy（插件在 _legacy_migrated 时就是这样做的）
        tasks2, _ = parse_tasks("", legacy_rules="", legacy_url="", legacy_token="")
        assert tasks2 == [], "已迁移后仍复活了旧任务"


class TestCronNormalization:
    """回归测试：6 段 cron 必须被规范化，否则任务静默不执行。

    审计发现：文档/表单/示例统一用 6 段（`0 30 4 * * *`），但
    `CronTrigger.from_crontab` 只接受 5 段 → 按文档配置的任务永远不会执行。
    """

    def _normalize(self, cron):
        # 直接测插件类的静态方法（不导入宿主依赖，用源码加载）
        import re as _re
        src = (PLUGIN_DIR / "__init__.py").read_text(encoding="utf-8")
        # 提取 _normalize_cron 的函数体，独立求值，避免导入宿主
        m = _re.search(
            r"    def _normalize_cron\(cron: str\) -> str:\n(?:.*\n)*?        return \" \"\.join\(fields\)\n",
            src,
        )
        assert m, "未能从源码中定位 _normalize_cron"
        ns = {}
        exec("def _normalize_cron(cron):\n" + "\n".join(
            line[4:] for line in m.group(0).splitlines()[1:]
        ), ns)
        return ns["_normalize_cron"](cron)

    def test_six_field_becomes_five(self):
        assert self._normalize("0 30 4 * * *") == "30 4 * * *"

    def test_five_field_unchanged(self):
        assert self._normalize("30 4 * * *") == "30 4 * * *"

    def test_six_field_with_different_seconds(self):
        # 秒字段被丢弃（APScheduler crontab 无秒语义）
        assert self._normalize("15 0 3 * * 1") == "0 3 * * 1"

    def test_extra_whitespace_handled(self):
        assert self._normalize("  0   30  4  *  *  *  ") == "30 4 * * *"

    def test_invalid_field_count_passes_through(self):
        # 非 5/6 段原样返回，交由调用方捕获解析错误
        assert self._normalize("* * *") == "* * *"

    def test_normalized_cron_is_accepted_by_apscheduler(self):
        """规范化后的结果必须能被 APScheduler 真正解析。"""
        apscheduler = pytest.importorskip("apscheduler.triggers.cron")
        for raw in ("0 30 4 * * *", "30 4 * * *", "0 0 6 * * *"):
            normalized = self._normalize(raw)
            trigger = apscheduler.CronTrigger.from_crontab(normalized)
            assert trigger is not None


class TestConcurrentScan:
    """并发遍历：正确性与提速。

    背景：每目录一次 HTTP 请求（实测约 111ms）。
    `/EmbyCloud` 有 852+ 目录，串行需 95 秒以上；
    并发 8 线程提速约 6 倍（16 线程无进一步收益）。

    并发只改变请求顺序，不改变结果——这组测试锁住这个不变量。
    """

    def _tree_client(self, tree, delay=0.0):
        """构造一棵树的 client；delay 用于放大延迟以观察并发效果。"""
        import threading

        calls = []
        lock = threading.Lock()

        def transport(method, url, json=None, headers=None):
            path = (json or {}).get("path", "")
            with lock:
                calls.append(path)
            if delay:
                time.sleep(delay)
            if url.endswith("/api/fs/list"):
                entries = tree.get(path)
                if entries is None:
                    return 200, {"code": 500, "message": "object not found"}, ""
                return 200, {"code": 200, "data": {"content": entries}}, ""
            return 404, {"code": 404}, ""

        return OpenListClient(BASE, token="t", transport=transport), calls

    def _wide_tree(self, n_children, files_per_child=1):
        """构造一个根目录 + n 个子目录的树。"""
        tree = {"/A": [{"name": f"d{i}", "is_dir": True} for i in range(n_children)]}
        for i in range(n_children):
            tree[f"/A/d{i}"] = [
                {"name": f"f{j}.mkv", "is_dir": False} for j in range(files_per_child)
            ]
        return tree

    def test_concurrent_result_matches_serial(self):
        """并发与串行的产出必须完全一致。"""
        tree = self._wide_tree(20, 2)

        c1, _ = self._tree_client(tree)
        serial = scan(c1, parse_rules("/A#/out")[0], workers=1)

        c2, _ = self._tree_client(tree)
        concurrent = scan(c2, parse_rules("/A#/out")[0], workers=8)

        assert serial.planned == concurrent.planned
        assert serial.downloads == concurrent.downloads
        assert serial.dirs_scanned == concurrent.dirs_scanned
        assert serial.videos_found == concurrent.videos_found
        assert serial.remote_files == concurrent.remote_files

    def test_results_are_sorted_regardless_of_completion_order(self):
        """并发完成顺序不定，产出顺序必须稳定。"""
        tree = self._wide_tree(30, 1)
        paths = []
        for _ in range(3):
            c, _ = self._tree_client(tree)
            r = scan(c, parse_rules("/A#/out")[0], workers=8)
            paths.append([p for p, _ in r.planned])
        assert paths[0] == paths[1] == paths[2]
        assert paths[0] == sorted(paths[0]), "产出未排序"

    def test_each_directory_requested_once(self):
        """同一目录只应被请求一次（BFS 不会重复访问）。

        关闭索引（use_index=False）以便只观察遍历行为：
        索引开启时会先探一次 `/api/fs/search`。
        """
        tree = self._wide_tree(15, 1)
        c, calls = self._tree_client(tree)
        scan(c, parse_rules("/A#/out")[0], workers=8, use_index=False)
        assert len(calls) == len(set(calls)), f"存在重复请求：{calls}"
        assert len(calls) == 16          # 1 个根 + 15 个子目录

    def test_concurrent_is_faster(self):
        """并发应显著快于串行（用人工延迟放大效果）。"""
        tree = self._wide_tree(16, 1)

        c1, _ = self._tree_client(tree, delay=0.05)
        t0 = time.time()
        scan(c1, parse_rules("/A#/out")[0], workers=1)
        serial = time.time() - t0

        c2, _ = self._tree_client(tree, delay=0.05)
        t0 = time.time()
        scan(c2, parse_rules("/A#/out")[0], workers=8)
        concurrent = time.time() - t0

        assert concurrent < serial / 2, f"并发 {concurrent:.2f}s 未快于串行 {serial:.2f}s"

    def test_failures_isolated_under_concurrency(self):
        """并发下单个目录失败同样不能影响其它目录。"""
        tree = self._wide_tree(12, 1)

        def transport(method, url, json=None, headers=None):
            path = (json or {}).get("path", "")
            if path == "/A/d5":
                return 554, None, ""          # 模拟网盘超时
            if url.endswith("/api/fs/list"):
                entries = tree.get(path)
                if entries is None:
                    return 200, {"code": 500, "message": "object not found"}, ""
                return 200, {"code": 200, "data": {"content": entries}}, ""
            return 404, {"code": 404}, ""

        client = OpenListClient(BASE, token="t", transport=transport, retry_attempts=1)
        r = scan(client, parse_rules("/A#/out")[0], workers=8)

        assert r.videos_found == 11, f"应成功 11 个，实际 {r.videos_found}"
        assert r.dirs_failed == 1
        assert any("d5" in e for e in r.errors)

    def test_cancellation_stops_early(self):
        """取消回调应能中断并发遍历。

        取消在**每层开始时**检查，因此用多层树才能观察到中断。
        """
        # 3 层深、每层 20 个分支，确保有足够多的层
        tree = {"/A": [{"name": f"d{i}", "is_dir": True} for i in range(20)]}
        for i in range(20):
            tree[f"/A/d{i}"] = [{"name": f"e{j}", "is_dir": True} for j in range(20)]
            for j in range(20):
                tree[f"/A/d{i}/e{j}"] = [{"name": "f.mkv", "is_dir": False}]

        c, _ = self._tree_client(tree)
        state = {"n": 0}

        def cancel():
            state["n"] += 1
            return state["n"] > 2

        r = scan(c, parse_rules("/A#/out")[0], workers=4, should_cancel=cancel)
        assert r.cancelled is True
        # 被中断时不应处理完整棵树
        assert r.dirs_scanned < 1 + 20 + 400


class TestIndexFirstScan:
    """双模式：索引优先，自动回退遍历。

    背景：OpenList 的 `/api/fs/search` 查服务端本地索引，`parent` 递归匹配整棵
    子树，一次查询即可拿到全部文件；索引未启用/未建完时会超时或报错，
    此时必须**无痛回退**到并发遍历，且两条路径产出必须一致。
    """

    def _client(self, tree, index_ok=True, index_nodes=None, index_error=False):
        """构造同时支持 list 与 search 的 client。"""
        calls = {"list": [], "search": 0}

        def transport(method, url, json=None, headers=None):
            if url.endswith("/api/fs/search"):
                calls["search"] += 1
                if index_error:
                    return 554, None, ""            # 索引不可用
                if not index_ok:
                    return 200, {"code": 500, "message": "search not available"}, ""
                nodes = index_nodes or []
                # 简单实现 parent 递归匹配
                parent = (json or {}).get("parent", "/")
                scope = (json or {}).get("scope", 0)
                page = (json or {}).get("page", 1)
                per = (json or {}).get("per_page", 1000)
                hits = []
                for n in nodes:
                    if scope == 2 and n.get("is_dir"):
                        continue
                    if scope == 1 and not n.get("is_dir"):
                        continue
                    p = n.get("parent", "")
                    if parent == "/" or p == parent or p.startswith(parent.rstrip("/") + "/"):
                        hits.append(n)
                start = (page - 1) * per
                return 200, {"code": 200, "data": {
                    "total": len(hits),
                    "content": hits[start:start + per]}}, ""
            if url.endswith("/api/fs/list"):
                path = (json or {}).get("path", "")
                calls["list"].append(path)
                entries = tree.get(path)
                if entries is None:
                    return 200, {"code": 500, "message": "object not found"}, ""
                return 200, {"code": 200, "data": {"content": entries}}, ""
            return 404, {"code": 404}, ""

        return OpenListClient(BASE, token="t", transport=transport), calls

    def _index_nodes(self):
        """索引记录：parent 指向深层目录，name 是文件名。"""
        return [
            {"parent": "/A/d0", "name": "1.mkv", "is_dir": False, "size": 100},
            {"parent": "/A/d1", "name": "2.mp4", "is_dir": False, "size": 200},
            {"parent": "/A/d1", "name": "sub.srt", "is_dir": False, "size": 10},
            {"parent": "/A/d2", "name": "junk.tmp", "is_dir": False, "size": 1},
        ]

    def test_index_path_used_when_available(self):
        """索引可用时应走索引，且不发 list 请求。"""
        tree = {"/A": [{"name": "d0", "is_dir": True},
                       {"name": "d1", "is_dir": True},
                       {"name": "d2", "is_dir": True}]}
        client, calls = self._client(tree, index_nodes=self._index_nodes())
        r = scan(client, parse_rules("/A#/out")[0], use_index=True)

        assert calls["search"] >= 1, "未查询索引"
        assert calls["list"] == [], f"索引可用时不该遍历：{calls['list']}"

        names = sorted(p.rsplit("/", 1)[-1] for p, _ in r.planned)
        assert names == ["1.strm", "2.strm"], f"实际 {names}"
        assert r.videos_found == 2
        # 字幕走下载，临时文件被过滤
        assert len(r.downloads) == 1
        assert r.skipped_files == 1

    def test_fallback_when_index_unavailable(self):
        """索引返回错误时应自动回退遍历，并拿到完整结果。"""
        tree = {
            "/A": [{"name": "d0", "is_dir": True}],
            "/A/d0": [{"name": "1.mkv", "is_dir": False}],
        }
        client, calls = self._client(tree, index_error=True)
        r = scan(client, parse_rules("/A#/out")[0], use_index=True)

        assert calls["search"] >= 1, "应尝试过索引"
        assert calls["list"], "未回退到遍历"
        names = [p.rsplit("/", 1)[-1] for p, _ in r.planned]
        assert names == ["1.strm"], f"实际 {names}"

    def test_fallback_when_index_disabled(self):
        """索引未启用（code != 200）时回退。"""
        tree = {
            "/A": [{"name": "d0", "is_dir": True}],
            "/A/d0": [{"name": "1.mkv", "is_dir": False}],
        }
        client, calls = self._client(tree, index_ok=False)
        r = scan(client, parse_rules("/A#/out")[0], use_index=True)
        assert calls["list"], "未回退到遍历"
        assert r.videos_found == 1

    def test_use_index_false_skips_search(self):
        """显式关闭索引时不应查询搜索接口。"""
        tree = {
            "/A": [{"name": "d0", "is_dir": True}],
            "/A/d0": [{"name": "1.mkv", "is_dir": False}],
        }
        client, calls = self._client(tree, index_nodes=self._index_nodes())
        r = scan(client, parse_rules("/A#/out")[0], use_index=False)
        assert calls["search"] == 0, "不该查询索引"
        assert r.videos_found == 1

    def test_fallback_when_index_returns_empty(self):
        """索引返回 0 条（未建完）时应回退，避免漏文件。"""
        tree = {
            "/A": [{"name": "d0", "is_dir": True}],
            "/A/d0": [{"name": "1.mkv", "is_dir": False}],
        }
        client, calls = self._client(tree, index_nodes=[])   # 索引为空
        r = scan(client, parse_rules("/A#/out")[0], use_index=True)
        assert calls["list"], "索引为空时应回退"
        assert r.videos_found == 1

    def test_index_result_outside_root_ignored(self):
        """索引是全局的，落在扫描根之外的记录必须被忽略。"""
        nodes = [
            {"parent": "/A", "name": "in.mkv", "is_dir": False, "size": 1},
            {"parent": "/B", "name": "out.mkv", "is_dir": False, "size": 1},
        ]
        tree = {"/A": [{"name": "in.mkv", "is_dir": False}]}
        client, _ = self._client(tree, index_nodes=nodes)
        r = scan(client, parse_rules("/A#/out")[0], use_index=True)
        names = [p.rsplit("/", 1)[-1] for p, _ in r.planned]
        assert names == ["in.strm"], f"越界记录未被过滤：{names}"

    def test_index_respects_exclude(self):
        """索引路径同样要应用 exclude 规则。"""
        nodes = [
            {"parent": "/A/keep", "name": "a.mkv", "is_dir": False, "size": 1},
            {"parent": "/A/skip", "name": "b.mkv", "is_dir": False, "size": 1},
        ]
        tree = {"/A": [{"name": "keep", "is_dir": True},
                       {"name": "skip", "is_dir": True}]}
        client, _ = self._client(tree, index_nodes=nodes)
        r = scan(client, parse_rules(r"/A#/out##/skip")[0], use_index=True)
        names = [p.rsplit("/", 1)[-1] for p, _ in r.planned]
        assert names == ["a.strm"], f"exclude 未生效：{names}"

    def test_index_pagination(self):
        """索引分页应能翻完全部结果。"""
        nodes = [{"parent": "/A", "name": f"f{i}.mkv", "is_dir": False, "size": 1}
                 for i in range(25)]
        tree = {"/A": [{"name": "x", "is_dir": True}]}
        client, _ = self._client(tree, index_nodes=nodes)
        # page_size 通过 scan 内部默认值控制，这里用 25 条验证不漏
        r = scan(client, parse_rules("/A#/out")[0], use_index=True)
        assert r.videos_found == 25, f"分页漏条：{r.videos_found}"

    def test_index_direct_url_uses_base_path(self):
        """索引路径生成的直链必须带上 base_path。"""
        nodes = [{"parent": "/A/d0", "name": "1.mkv", "is_dir": False, "size": 1}]
        tree = {"/A": [{"name": "d0", "is_dir": True}]}
        client, _ = self._client(tree, index_nodes=nodes)
        r = scan(client, parse_rules("/A#/out")[0], base_path="/EmbyCloud")
        assert r.planned, "无产出"
        _local, content = r.planned[0]
        assert "/EmbyCloud/A/d0/1.mkv" in content, f"base_path 未生效：{content}"


class TestTransientErrorResilience:
    """回归测试：单个目录出错不能中止整轮扫描。

    真实故障：`/EmbyCloud/HDHive` 间歇性返回 HTTP 554 + 空响应体
    （上游 115 网盘超时）。过去 `list_dir` 直接向上抛，被 `scan()` 的规则级
    catch 捕获，导致该规则**后续所有目录全部丢失**——`/EmbyCloud` 只扫到
    8 个目录就停了，日志里也只有一行「非 JSON」。
    """

    def _client(self, failing_paths, existing=None):
        """构造一个对指定路径返回 554 空响应的 client。"""
        calls = []

        def transport(method, url, json=None, headers=None):
            path = (json or {}).get("path", "")
            calls.append(path)
            if path in failing_paths:
                # 模拟 115 超时：OpenList 返回非标准码且响应体为空
                return 554, None, ""
            payload = existing if existing is not None else {}
            if url.endswith("/api/fs/list"):
                entries = payload.get(path)
                if entries is None:
                    return 200, {"code": 500, "message": "object not found"}, ""
                return 200, {"code": 200, "data": {"content": entries}}, ""
            return 404, {"code": 404}, ""

        return OpenListClient(BASE, token="t", transport=transport,
                              retry_attempts=1), calls

    def test_transient_error_does_not_abort_whole_rule(self):
        """一个子目录失败，其它子目录仍必须被扫描。"""
        tree = {
            "/A": [
                {"name": "ok1", "is_dir": True},
                {"name": "bad", "is_dir": True},
                {"name": "ok2", "is_dir": True},
            ],
            "/A/ok1": [{"name": "1.mkv", "is_dir": False}],
            "/A/bad": [{"name": "x.mkv", "is_dir": False}],
            "/A/ok2": [{"name": "2.mkv", "is_dir": False}],
        }
        client, _ = self._client({"/A/bad"}, tree)
        rules, _ = parse_rules("/A#/out")
        result = scan(client, rules)

        planned = sorted(p.rsplit("/", 1)[-1] for p, _ in result.planned)
        assert planned == ["1.strm", "2.strm"], f"失败目录之后的目录被丢弃：{planned}"
        assert result.dirs_failed == 1
        assert any("bad" in e for e in result.errors)

    def test_error_message_includes_status(self):
        """错误信息必须带 HTTP 状态码，否则无法定位。"""
        client, _ = self._client({"/A/bad"})
        with pytest.raises(OpenListError) as exc:
            client.list_dir("/A/bad", retry_transient=False)
        assert "554" in str(exc.value)

    def test_transient_is_retried(self):
        """瞬时错误应被重试，而不是直接失败。"""
        attempts = {"n": 0}

        def transport(method, url, json=None, headers=None):
            attempts["n"] += 1
            if attempts["n"] < 3:
                return 554, None, ""            # 前两次失败
            return 200, {"code": 200, "data": {"content": []}}, ""

        client = OpenListClient(BASE, token="t", transport=transport,
                                retry_attempts=3)
        assert client.list_dir("/A") == []
        assert attempts["n"] == 3

    def test_permanent_error_not_retried(self):
        """4xx 是确定性错误，重试无意义。"""
        attempts = {"n": 0}

        def transport(method, url, json=None, headers=None):
            attempts["n"] += 1
            return 200, {"code": 403, "message": "password required"}, ""

        client = OpenListClient(BASE, token="t", transport=transport,
                                retry_attempts=3)
        with pytest.raises(OpenListError):
            client.list_dir("/A")
        assert attempts["n"] == 1

    def test_root_failure_still_reported(self):
        """根目录失败说明规则不可用，应记录为规则级错误。"""
        client, _ = self._client({"/A"})
        rules, _ = parse_rules("/A#/out")
        result = scan(client, rules)
        assert result.errors, "根目录失败必须被报告"
        assert result.planned == []

    def test_transport_exception_becomes_transient(self):
        """传输层抛异常（超时/重置）应按瞬时错误处理。"""
        def transport(method, url, json=None, headers=None):
            raise TimeoutError("connection timed out")

        client = OpenListClient(BASE, token="t", transport=transport,
                                retry_attempts=2)
        with pytest.raises(OpenListError) as exc:
            client.list_dir("/A")
        assert exc.value.transient is True


class TestFileNameRoundTrip:
    """命名规则的端到端一致性。"""

    def test_video_becomes_strm_without_double_extension(self):
        from mp_plugin_openliststrm.strmutil import strm_target_path

        # 剧名.mkv -> 剧名.strm（不是 剧名.mkv.strm）
        assert strm_target_path("/o", "/A/剧名.mkv", "/A") == "/o/剧名.strm"
        for ext in (".mp4", ".ts", ".iso", ".rmvb", ".m2ts", ".webm"):
            got = strm_target_path("/o", f"/A/x{ext}", "/A")
            assert got == "/o/x.strm", f"{ext} -> {got}"

    def test_subtitle_keeps_extension(self):
        from mp_plugin_openliststrm.strmutil import strm_target_path

        got = strm_target_path("/o", "/A/x.ass", "/A",
                               replace_extension=False, add_strm_suffix=False)
        assert got == "/o/x.ass"


class TestEndToEndNaming:
    """端到端回归：scan 计划 → 落盘 → 检测/预览/清空 必须能看到产物。

    写入端产物必须能被 `rglob("*.strm")` 找到，否则检测/预览/清空
    对插件自己的产物永远是 0 条，使「先检测再确认清理」这套安全流程失效。
    """

    def _scan_and_write(self, tmp_path, tree):
        """跑完整的 scan → 落盘链路，返回输出目录与 scan 结果。"""
        out = tmp_path / "out"
        rules, _ = parse_rules(f"/A#{out}")

        def transport(method, url, json=None, headers=None):
            if url.endswith("/api/fs/list"):
                p = (json or {}).get("path", "/")
                if p in tree:
                    return 200, {"code": 200, "data": {"content": tree[p]}}
            return 200, {"code": 500, "message": "object not found"}

        client = OpenListClient(BASE, token="t", transport=transport)
        result = scan(client, rules, video_ext=VIDEO, download_ext=[".srt"])

        # 复刻插件落盘行为：strm 写内容，下载项写实体文件
        for target, content in result.planned:
            path = Path(target)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        for target, _remote in result.downloads:
            path = Path(target)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("SUB", encoding="utf-8")
        return out, result

    def test_written_strm_is_discoverable_by_rglob(self, tmp_path):
        """核心断言：落盘的 strm 必须能被 rglob("*.strm") 找到。"""
        tree = {"/A": [{"name": "movie.mkv", "is_dir": False},
                       {"name": "sub.srt", "is_dir": False}]}
        out, result = self._scan_and_write(tmp_path, tree)

        written = sorted(p.name for p in out.rglob("*") if p.is_file())
        strm_found = sorted(p.name for p in out.rglob("*.strm"))

        assert result.planned, "应有 strm 计划"
        assert strm_found == ["movie.strm"], f"产物未被 *.strm 匹配：磁盘={written}"
        # 字幕是实体文件，不能带 .strm 后缀
        assert "sub.srt" in written
        assert not list(out.rglob("*.srt.strm"))

    def test_iter_strm_files_sees_products(self, tmp_path):
        """cleanup.iter_strm_files 必须能看到插件自己写的产物。"""
        from mp_plugin_openliststrm.cleanup import iter_strm_files

        tree = {"/A": [{"name": "a.mkv", "is_dir": False},
                       {"name": "b.mp4", "is_dir": False}]}
        out, _ = self._scan_and_write(tmp_path, tree)

        found = sorted(p.name for p in iter_strm_files(out))
        assert found == ["a.strm", "b.strm"]

    def test_collect_broken_scans_products(self, tmp_path):
        """collect_broken 必须把插件自己的产物纳入扫描（total_scanned > 0）。"""
        tree = {"/A": [{"name": "a.mkv", "is_dir": False}]}
        out, _ = self._scan_and_write(tmp_path, tree)

        # 远端已不存在 → 应被判定失效
        client = OpenListClient(BASE, token="t", transport=FakeTreeTransport(set()))
        rules, _ = parse_rules(f"/A#{out}")
        plan = collect_broken(rules, client=client, remote_existing=None)

        assert plan.total_scanned == 1, "插件自己的产物未被检测流程看到"
        assert plan.count == 1
        assert plan.broken[0].strm_path.name == "a.strm"

    def test_clear_preview_counts_products(self, tmp_path):
        """预览清空必须能统计到产物（用与 _collect_all_strm 相同的 rglob）。"""
        tree = {"/A": [{"name": "a.mkv", "is_dir": False},
                       {"name": "b.mkv", "is_dir": False}]}
        out, _ = self._scan_and_write(tmp_path, tree)

        counted = sorted(out.rglob("*.strm"))
        assert len(counted) == 2, "清空预览统计不到产物"


class TestCrossInstanceSafety:
    """回归测试：多实例共用输出目录时，绝不能误删属于其它实例的有效 strm。

    这是审计发现的**阻断级数据丢失 bug**：用 A 实例的 client 去校验 B 实例的 strm，
    必然得到 object not found，从而把有效文件判为失效并删除。
    """

    def _make(self, tmp_path, name, url):
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(url, encoding="utf-8")
        return p

    def test_other_instance_strm_is_skipped(self, tmp_path):
        """属于其它实例的 strm 必须跳过，不能被判定为失效。"""
        out = tmp_path / "shared"
        out.mkdir()
        self._make(out, "mine.strm", "https://server-a.example.com/d/A/a.mkv")
        self._make(out, "other.strm", "https://server-b.example.com/d/B/b.mkv")

        # 用 A 的 client：B 的文件在 A 上当然不存在
        client = OpenListClient("https://server-a.example.com", token="t",
                                transport=FakeTreeTransport(set()))
        rules, _ = parse_rules(f"/A#{out}")
        plan = collect_broken(rules, client=client, remote_existing=None,
                              expected_host="server-a.example.com")

        # 只有 A 的 strm 参与判定；B 的被跳过
        assert plan.total_scanned == 2
        assert plan.skipped_other_instance == 1
        assert [i.strm_path.name for i in plan.broken] == ["mine.strm"]

    def test_valid_other_instance_strm_survives_cleanup(self, tmp_path):
        """端到端：其它实例的有效 strm 在清理后必须仍然存在。"""
        out = tmp_path / "shared"
        out.mkdir()
        mine = self._make(out, "mine.strm", "https://server-a.example.com/d/A/gone.mkv")
        other = self._make(out, "other.strm", "https://server-b.example.com/d/B/alive.mkv")

        client = OpenListClient("https://server-a.example.com", token="t",
                                transport=FakeTreeTransport(set()))
        rules, _ = parse_rules(f"/A#{out}")
        plan = collect_broken(rules, client=client, remote_existing=None,
                              expected_host="server-a.example.com")
        stats = execute_cleanup(plan, rules, delete_strm=True,
                                expected_host="server-a.example.com")

        assert stats["strm_deleted"] == 1
        assert not mine.exists()          # 本实例的失效 strm 被删
        assert other.exists(), "其它实例的有效 strm 被误删！"   # 关键断言

    def test_execute_cleanup_refuses_cross_instance_even_if_in_plan(self, tmp_path):
        """第二道防线：即使 plan 里混入了其它实例的项，execute_cleanup 也必须拒绝。"""
        out = tmp_path / "shared"
        out.mkdir()
        other = self._make(out, "other.strm", "https://server-b.example.com/d/B/alive.mkv")

        from mp_plugin_openliststrm.cleanup import BrokenStrm, CleanupPlan
        plan = CleanupPlan(broken=[BrokenStrm(
            strm_path=other,
            raw_url="https://server-b.example.com/d/B/alive.mkv",
            remote_path="/B/alive.mkv",
            reason="伪造为失效",
        )])
        rules, _ = parse_rules(f"/B#{out}")
        stats = execute_cleanup(plan, rules, delete_strm=True,
                                expected_host="server-a.example.com")

        assert stats["strm_deleted"] == 0
        assert stats["skipped_other_instance"] == 1
        assert other.exists(), "跨实例防护失效！"

    def test_relative_url_skipped_when_host_expected(self, tmp_path):
        """相对路径无法判断归属，保守跳过而不是判失效。"""
        out = tmp_path / "shared"
        out.mkdir()
        rel = out / "rel.strm"
        rel.write_text("/d/A/a.mkv", encoding="utf-8")

        client = OpenListClient("https://a.example.com", token="t",
                                transport=FakeTreeTransport(set()))
        rules, _ = parse_rules(f"/A#{out}")
        plan = collect_broken(rules, client=client, remote_existing=None,
                              expected_host="a.example.com")
        assert plan.broken == []
        assert plan.skipped_other_instance == 1

    def test_no_host_check_when_expected_empty(self, tmp_path):
        """expected_host 为空时保持旧行为（单实例场景）。"""
        out = tmp_path / "solo"
        out.mkdir()
        self._make(out, "a.strm", "https://any.example.com/d/A/gone.mkv")
        client = OpenListClient("https://any.example.com", token="t",
                                transport=FakeTreeTransport(set()))
        rules, _ = parse_rules(f"/A#{out}")
        plan = collect_broken(rules, client=client, remote_existing=None)
        assert len(plan.broken) == 1
        assert plan.skipped_other_instance == 0


# ===================================================================== 失效检测
class FakeTreeTransport:
    """按路径是否存在返回 /api/fs/get 结果。"""

    def __init__(self, existing):
        self.existing = set(existing)
        self.calls = []

    def __call__(self, method, url, json=None, headers=None):
        self.calls.append((method, url, json))
        if url.endswith("/api/fs/get"):
            path = (json or {}).get("path", "")
            if path in self.existing:
                return 200, {"code": 200, "data": {"name": path.split("/")[-1], "is_dir": False}}
            return 200, {"code": 500, "message": "object not found"}
        return 404, {"code": 404}


class TestCollectBroken:
    def _make_strm(self, root: Path, rel: str, url: str) -> Path:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(url, encoding="utf-8")
        return path

    def test_detects_missing_via_api(self, tmp_path):
        out = tmp_path / "out"
        self._make_strm(out, "gone.mkv.strm", "https://h/d/Ani/gone.mkv")
        self._make_strm(out, "here.mkv.strm", "https://h/d/Ani/here.mkv")

        client = OpenListClient(BASE, token="t",
                                transport=FakeTreeTransport({"/Ani/here.mkv"}))
        rules, _ = parse_rules(f"/Ani#{out}")
        plan = collect_broken(rules, client=client, remote_existing=None)

        assert plan.count == 1
        assert plan.broken[0].remote_path == "/Ani/gone.mkv"
        assert plan.total_scanned == 2

    def test_coarse_filter_then_verify(self, tmp_path):
        """粗筛集合不完整时，精查应能纠正误判。"""
        out = tmp_path / "out"
        # 两个 strm 都不在粗筛集合里，但只有一个真的不存在
        self._make_strm(out, "a.strm", "https://h/d/Ani/a.mkv")
        self._make_strm(out, "b.strm", "https://h/d/Ani/b.mkv")

        client = OpenListClient(BASE, token="t",
                                transport=FakeTreeTransport({"/Ani/a.mkv"}))
        rules, _ = parse_rules(f"/Ani#{out}")
        # 粗筛集合为空（模拟遍历遗漏），精查后只有 b 被判定失效
        plan = collect_broken(rules, client=client, remote_existing=set())

        assert plan.count == 1
        assert plan.broken[0].remote_path == "/Ani/b.mkv"

    def test_skips_unparsable_content(self, tmp_path):
        out = tmp_path / "out"
        self._make_strm(out, "manual.strm", "https://other-site.example/media.mkv")
        self._make_strm(out, "text.strm", "not a url at all")

        client = OpenListClient(BASE, token="t", transport=FakeTreeTransport(set()))
        rules, _ = parse_rules(f"/Ani#{out}")
        plan = collect_broken(rules, client=client, remote_existing=None)

        assert plan.count == 0                       # 绝不误删非本插件生成的内容
        assert plan.skipped_unparsable == 2

    def test_missing_dir_is_ignored(self, tmp_path):
        rules, _ = parse_rules(f"/Ani#{tmp_path / 'nonexistent'}")
        client = OpenListClient(BASE, token="t", transport=FakeTreeTransport(set()))
        plan = collect_broken(rules, client=client, remote_existing=None)
        assert plan.count == 0 and plan.total_scanned == 0

    def test_cancel(self, tmp_path):
        out = tmp_path / "out"
        self._make_strm(out, "a.strm", "https://h/d/Ani/a.mkv")
        rules, _ = parse_rules(f"/Ani#{out}")
        client = OpenListClient(BASE, token="t", transport=FakeTreeTransport(set()))
        plan = collect_broken(rules, client=client, remote_existing=None,
                              should_cancel=lambda: True)
        assert plan.cancelled

    def test_max_verify_limit(self, tmp_path):
        out = tmp_path / "out"
        for i in range(5):
            self._make_strm(out, f"f{i}.strm", f"https://h/d/Ani/f{i}.mkv")
        rules, _ = parse_rules(f"/Ani#{out}")
        client = OpenListClient(BASE, token="t", transport=FakeTreeTransport(set()))
        plan = collect_broken(rules, client=client, remote_existing=set(), max_verify=2)
        assert plan.count == 2
        assert plan.errors                        # 提示达到上限


class TestAttachHardlinks:
    def test_attaches_ids_and_hardlinks(self, tmp_path):
        out = tmp_path / "out"
        out.mkdir(parents=True)
        strm = out / "a.strm"
        strm.write_text("https://h/d/Ani/a.mkv", encoding="utf-8")

        # 媒体库侧的硬链接（真的创建硬链接来验证 samefile 判定）
        library = tmp_path / "library"
        library.mkdir()
        link = library / "a.strm"
        try:
            link.hardlink_to(strm)
        except OSError:
            pytest.skip("当前文件系统不支持硬链接")

        from mp_plugin_openliststrm.cleanup import BrokenStrm
        plan = type("P", (), {"broken": [BrokenStrm(
            strm_path=strm, raw_url="", remote_path="/Ani/a.mkv", reason="test",
        )]})()

        def lookup(_path):
            return [{"id": 42, "dest": str(link)}]

        attach_hardlinks(plan, [], lookup)
        item = plan.broken[0]
        assert item.transfer_ids == [42]
        assert item.hardlinks == [link]

    def test_non_hardlink_not_included(self, tmp_path):
        out = tmp_path / "out"
        out.mkdir(parents=True)
        strm = out / "a.strm"
        strm.write_text("x", encoding="utf-8")

        # 另一个独立文件（不是硬链接）
        other = tmp_path / "other.strm"
        other.write_text("different", encoding="utf-8")

        from mp_plugin_openliststrm.cleanup import BrokenStrm
        plan = type("P", (), {"broken": [BrokenStrm(
            strm_path=strm, raw_url="", remote_path="/Ani/a.mkv", reason="test",
        )]})()

        attach_hardlinks(plan, [], lambda _p: [{"id": 1, "dest": str(other)}])
        assert plan.broken[0].hardlinks == []       # 不是同一份数据，不纳入
        assert plan.broken[0].transfer_ids == [1]


class TestSafeDelete:
    def test_deletes_within_root(self, tmp_path):
        target = tmp_path / "a.strm"
        target.write_text("x", encoding="utf-8")
        ok, _ = safe_delete(target, [tmp_path])
        assert ok and not target.exists()

    def test_refuses_outside_root(self, tmp_path):
        inside = tmp_path / "root"
        inside.mkdir()
        outside = tmp_path / "outside.strm"
        outside.write_text("x", encoding="utf-8")
        ok, message = safe_delete(outside, [inside])
        assert not ok and outside.exists()
        assert "拒绝" in message

    def test_refuses_directory(self, tmp_path):
        d = tmp_path / "adir"
        d.mkdir()
        ok, _ = safe_delete(d, [tmp_path])
        assert not ok and d.exists()

    def test_missing_file(self, tmp_path):
        ok, message = safe_delete(tmp_path / "nope.strm", [tmp_path])
        assert not ok and "不存在" in message

    def test_is_within_nested(self, tmp_path):
        nested = tmp_path / "a" / "b"
        nested.mkdir(parents=True)
        assert is_within(nested / "c.strm", [tmp_path])
        assert not is_within(tmp_path.parent / "elsewhere.strm", [tmp_path])


class TestSameInode:
    def test_same_file(self, tmp_path):
        a = tmp_path / "a"
        a.write_text("x", encoding="utf-8")
        assert same_inode(a, a)

    def test_hardlink_is_same(self, tmp_path):
        a = tmp_path / "a"
        a.write_text("x", encoding="utf-8")
        b = tmp_path / "b"
        try:
            b.hardlink_to(a)
        except OSError:
            pytest.skip("不支持硬链接")
        assert same_inode(a, b)

    def test_copy_is_not_same(self, tmp_path):
        a = tmp_path / "a"
        a.write_text("x", encoding="utf-8")
        b = tmp_path / "b"
        b.write_text("x", encoding="utf-8")
        assert not same_inode(a, b)

    def test_missing_returns_false(self, tmp_path):
        a = tmp_path / "a"
        a.write_text("x", encoding="utf-8")
        assert not same_inode(a, tmp_path / "nope")


class TestExecuteCleanup:
    def _plan(self, tmp_path, *, hardlink=True):
        out = tmp_path / "out"
        out.mkdir(parents=True)
        strm = out / "a.strm"
        strm.write_text("https://h/d/Ani/a.mkv", encoding="utf-8")

        links = []
        if hardlink:
            library = tmp_path / "library"
            library.mkdir()
            link = library / "a.strm"
            try:
                link.hardlink_to(strm)
                links.append(link)
            except OSError:
                pass

        from mp_plugin_openliststrm.cleanup import BrokenStrm, CleanupPlan
        plan = CleanupPlan(broken=[BrokenStrm(
            strm_path=strm, raw_url="", remote_path="/Ani/a.mkv", reason="test",
            hardlinks=links, transfer_ids=[7, 8],
        )])
        return plan, strm, links

    def test_delete_strm_only_by_default(self, tmp_path):
        plan, strm, links = self._plan(tmp_path)
        rules, _ = parse_rules(f"/Ani#{tmp_path / 'out'}")
        stats = execute_cleanup(plan, rules, delete_strm=True)
        assert stats["strm_deleted"] == 1
        assert stats["link_deleted"] == 0
        assert stats["record_deleted"] == 0
        assert not strm.exists()
        if links:
            assert links[0].exists()                 # 未开启时不动硬链接

    def test_delete_hardlinks_when_enabled(self, tmp_path):
        plan, strm, links = self._plan(tmp_path)
        if not links:
            pytest.skip("不支持硬链接")
        rules, _ = parse_rules(f"/Ani#{tmp_path / 'out'}")
        stats = execute_cleanup(plan, rules, delete_strm=True, delete_hardlinks=True)
        assert stats["link_deleted"] == 1
        assert not links[0].exists()

    def test_delete_records_when_enabled(self, tmp_path):
        plan, _, _ = self._plan(tmp_path, hardlink=False)
        rules, _ = parse_rules(f"/Ani#{tmp_path / 'out'}")
        deleted = []
        stats = execute_cleanup(
            plan, rules, delete_strm=True, delete_records=True,
            record_deleter=lambda rid: (deleted.append(rid), True)[1],
        )
        assert stats["record_deleted"] == 2
        assert deleted == [7, 8]

    def test_record_deleter_failure_counted(self, tmp_path):
        plan, _, _ = self._plan(tmp_path, hardlink=False)
        rules, _ = parse_rules(f"/Ani#{tmp_path / 'out'}")
        stats = execute_cleanup(
            plan, rules, delete_strm=True, delete_records=True,
            record_deleter=lambda rid: False,
        )
        assert stats["record_failed"] == 2

    def test_record_deleter_exception_isolated(self, tmp_path):
        plan, _, _ = self._plan(tmp_path, hardlink=False)
        rules, _ = parse_rules(f"/Ani#{tmp_path / 'out'}")

        def boom(_rid):
            raise RuntimeError("db down")

        stats = execute_cleanup(plan, rules, delete_strm=True,
                                delete_records=True, record_deleter=boom)
        assert stats["record_failed"] == 2
        assert stats["messages"]

    def test_no_delete_when_all_disabled(self, tmp_path):
        plan, strm, links = self._plan(tmp_path)
        rules, _ = parse_rules(f"/Ani#{tmp_path / 'out'}")
        stats = execute_cleanup(plan, rules, delete_strm=False)
        assert stats["strm_deleted"] == 0
        assert strm.exists()                         # 全部关闭时什么都不删
