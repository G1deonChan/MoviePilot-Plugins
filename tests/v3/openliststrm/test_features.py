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
)
from mp_plugin_openliststrm.openlist import OpenListClient  # noqa: E402
from mp_plugin_openliststrm.scanner import parse_rules, scan  # noqa: E402
from mp_plugin_openliststrm.tasks import (  # noqa: E402
    TaskConfig,
    parse_tasks,
    tasks_to_json,
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
    def test_empty_yields_no_tasks(self):
        tasks, warnings = parse_tasks("")
        assert tasks == [] and warnings == []

    def test_basic_task(self):
        raw = json.dumps([{"id": "t1", "name": "电影", "rules": "/A#/out", "cron": "0 0 4 * * *"}])
        tasks, warnings = parse_tasks(raw)
        assert len(tasks) == 1
        assert tasks[0].id == "t1"
        assert tasks[0].name == "电影"
        assert tasks[0].cron == "0 0 4 * * *"
        assert not warnings

    def test_legacy_migration(self):
        tasks, warnings = parse_tasks("", legacy_rules="/A#/out", legacy_cron="0 0 4 * * *")
        assert len(tasks) == 1
        assert tasks[0].id == "default"
        assert tasks[0].rules == "/A#/out"
        assert tasks[0].cron == "0 0 4 * * *"
        assert any("迁移" in w for w in warnings)

    def test_invalid_json_warns(self):
        tasks, warnings = parse_tasks("{oops")
        assert tasks == [] and warnings

    def test_invalid_id_rejected(self):
        raw = json.dumps([{"id": "bad id!", "rules": "/A#/out"}])
        tasks, warnings = parse_tasks(raw)
        assert tasks == [] and warnings

    def test_missing_rules_rejected(self):
        raw = json.dumps([{"id": "t1", "rules": ""}])
        tasks, warnings = parse_tasks(raw)
        assert tasks == [] and warnings

    def test_duplicate_id_skipped(self):
        raw = json.dumps([
            {"id": "dup", "rules": "/A#/out"},
            {"id": "dup", "rules": "/B#/out"},
        ])
        tasks, warnings = parse_tasks(raw)
        assert len(tasks) == 1 and warnings

    def test_auto_id_when_missing(self):
        raw = json.dumps([{"rules": "/A#/out"}])
        tasks, _ = parse_tasks(raw)
        assert tasks[0].id == "task_1"

    def test_flags_parsed(self):
        raw = json.dumps([{
            "id": "t1", "rules": "/A#/out",
            "enabled": False, "force_overwrite": True, "delete_missing": True, "detect_broken": True,
        }])
        tasks, _ = parse_tasks(raw)
        t = tasks[0]
        assert t.enabled is False
        assert t.force_overwrite is True
        assert t.delete_missing is True
        assert t.detect_broken is True

    def test_roundtrip(self):
        raw = json.dumps([{"id": "t1", "name": "x", "rules": "/A#/out", "cron": "0 0 4 * * *"}])
        tasks, _ = parse_tasks(raw)
        again, _ = parse_tasks(tasks_to_json(tasks))
        assert again[0].to_json() == tasks[0].to_json()

    def test_non_array_rejected(self):
        tasks, warnings = parse_tasks(json.dumps({"id": "t1"}))
        assert tasks == [] and warnings


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
        assert parsed[1] == expected

    def test_percent_decoding(self):
        parsed = parse_strm_url("https://h/d/Ani/%E4%B8%AD%E6%96%87/01.mkv")
        assert parsed[1] == "/Ani/中文/01.mkv"

    def test_fullwidth_tilde_roundtrip(self):
        # 真实样本：全角波浪号必须正确解码
        url = "https://alist.decanas.top/d/Ani/2019-1/%E8%BC%9D%E5%A4%9C%E5%A7%AC%EF%BD%9E%E5%A4%A9/01.mp4"
        parsed = parse_strm_url(url)
        assert "～" in parsed[1]

    def test_prefix_returned(self):
        parsed = parse_strm_url("https://h/openlist/d/Ani/01.mkv")
        assert parsed[0] == "/openlist"

    def test_rejects_non_strm(self):
        assert parse_strm_url("") is None
        assert parse_strm_url("   ") is None
        assert parse_strm_url("just some text") is None
        assert parse_strm_url("https://h/other/path.mkv") is None
        assert parse_strm_url("/api/fs/get") is None

    def test_only_first_line(self):
        parsed = parse_strm_url("https://h/d/Ani/01.mkv\nhttps://h/d/Other/02.mkv")
        assert parsed[1] == "/Ani/01.mkv"


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
