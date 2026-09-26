"""OpenList Strm 插件核心逻辑单元测试。

测试只依赖标准库与 pytest，通过**合成包**加载插件子模块，
避免导入插件主模块（其依赖 MoviePilot 宿主 `app.*` 与数十个第三方包）。

运行：
    python -m pytest tests/v3/openliststrm -v
"""

import importlib.util
import sys
import types
from pathlib import Path

import pytest

# --------------------------------------------------------------------- 合成包加载
PLUGIN_ID = "openliststrm"
PLUGIN_DIR = Path(__file__).resolve().parents[3] / "plugins.v3" / PLUGIN_ID
PKG_NAME = f"mp_plugin_{PLUGIN_ID}"


def _load_plugin_package():
    """把插件目录注册为合成包，仅加载纯逻辑子模块（不执行 __init__.py）。"""
    if PKG_NAME in sys.modules:
        return sys.modules[PKG_NAME]

    pkg = types.ModuleType(PKG_NAME)
    pkg.__path__ = [str(PLUGIN_DIR)]
    sys.modules[PKG_NAME] = pkg

    for sub in ("strmutil", "openlist", "scanner"):
        name = f"{PKG_NAME}.{sub}"
        spec = importlib.util.spec_from_file_location(name, PLUGIN_DIR / f"{sub}.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)  # type: ignore[union-attr]
    return pkg


_load_plugin_package()

from mp_plugin_openliststrm.openlist import OpenListClient, OpenListError  # noqa: E402
from mp_plugin_openliststrm.scanner import parse_rules, scan  # noqa: E402
from mp_plugin_openliststrm.strmutil import (  # noqa: E402
    build_direct_url,
    encode_remote_path,
    is_meta_file,
    is_video_file,
    iter_entries,
    join_remote_path,
    normalize_remote_path,
    response_ok,
    strm_target_path,
)

BASE = "https://openlist.example.com"
VIDEO = [".mkv", ".mp4"]


# --------------------------------------------------------------------- 路径工具
class TestNormalizeRemotePath:
    def test_adds_leading_slash(self):
        assert normalize_remote_path("EmbyCloud/Movie") == "/EmbyCloud/Movie"

    def test_backslash_to_slash(self):
        assert normalize_remote_path("\\Ani\\2019-1") == "/Ani/2019-1"

    def test_dot_segments_collapsed(self):
        assert normalize_remote_path("/Ani/./2019-1") == "/Ani/2019-1"
        assert normalize_remote_path("/Ani/sub/../2019-1") == "/Ani/2019-1"

    def test_root_above_root_stays_root(self):
        # 对齐 OpenList FixAndCleanPath：根目录之上仍是根目录
        assert normalize_remote_path("..") == "/"
        assert normalize_remote_path("/../..") == "/"

    def test_double_slash_collapsed(self):
        assert normalize_remote_path("//Ani//x") == "/Ani/x"

    def test_empty_becomes_root(self):
        assert normalize_remote_path("") == "/"
        assert normalize_remote_path(None) == "/"


class TestJoinRemotePath:
    def test_basic(self):
        assert join_remote_path("/Ani", "2019-1") == "/Ani/2019-1"

    def test_from_root(self):
        assert join_remote_path("/", "data") == "/data"

    def test_name_with_slash_is_normalized(self):
        assert join_remote_path("/A", "b/c") == "/A/b/c"


class TestEncodeRemotePath:
    def test_encodes_chinese_and_spaces(self):
        encoded = encode_remote_path("/Ani/中文 目录/文件.mkv")
        assert " " not in encoded
        assert "中文" not in encoded
        assert encoded.count("/") == 2          # 3 段 => 2 个分隔符

    def test_encodes_hash_and_question(self):
        encoded = encode_remote_path("/A/b#c?d%e.mkv")
        assert "#" not in encoded and "?" not in encoded

    def test_plain_ascii_unchanged(self):
        assert encode_remote_path("/Ani/2019-1") == "Ani/2019-1"

    def test_leading_slash_stripped(self):
        assert not encode_remote_path("/Ani/x").startswith("/")

    def test_roundtrip_decodes(self):
        import urllib.parse
        original = "/Ani/中文/[ANi] 番剧 01.mkv"
        encoded = encode_remote_path(original)
        assert urllib.parse.unquote(encoded) == original.lstrip("/")


class TestBuildDirectUrl:
    def test_basic(self):
        assert build_direct_url(BASE, "/Ani/2019-1/01.mp4") == f"{BASE}/d/Ani/2019-1/01.mp4"

    def test_trailing_slash_on_base(self):
        assert build_direct_url(BASE + "/", "/a.mkv") == f"{BASE}/d/a.mkv"

    def test_encoded_output(self):
        url = build_direct_url(BASE, "/Ani/中文/01.mp4")
        assert url.startswith(f"{BASE}/d/")
        assert "中文" not in url

    def test_unencoded_mode(self):
        assert build_direct_url(BASE, "/Ani/2019-1/01.mp4", encode=False) == f"{BASE}/d/Ani/2019-1/01.mp4"

    def test_site_url_subpath_prefix(self):
        # 实例挂在子路径下时（site_url 含 /openlist），前缀需保留
        url = build_direct_url(f"{BASE}/openlist", "/Ani/a.mkv")
        assert url == f"{BASE}/openlist/d/Ani/a.mkv"


class TestExtensionDetection:
    @pytest.mark.parametrize("name", ["a.mkv", "b.MP4", "c.Ts"])
    def test_video_yes(self, name):
        assert is_video_file(name, [".mkv", ".mp4", ".ts"])

    @pytest.mark.parametrize("name", ["a.nfo", "b.jpg", "noext"])
    def test_video_no(self, name):
        assert not is_video_file(name, VIDEO)

    def test_meta(self):
        assert is_meta_file("a.nfo", [".nfo", ".srt"])
        assert not is_meta_file("a.mkv", [".nfo"])


class TestStrmTargetPath:
    """命名规则：视频替换扩展名为 .strm，字幕保留原名。"""

    def test_strips_remote_root(self):
        assert strm_target_path("/out", "/EmbyCloud/Movie/a.mkv", "/EmbyCloud") == "/out/Movie/a.strm"

    def test_root_keeps_full_path(self):
        assert strm_target_path("/out", "/Ani/x/a.mkv", "/") == "/out/Ani/x/a.strm"

    def test_chinese_preserved_on_disk(self):
        assert strm_target_path("/out", "/Ani/中文/a.mkv", "/Ani") == "/out/中文/a.strm"

    def test_trailing_slash_in_root_dir(self):
        assert strm_target_path("/out/", "/Ani/a.mkv", "/Ani") == "/out/a.strm"

    def test_replaces_video_extension(self):
        """核心：剧名.mkv -> 剧名.strm（与 CloudStrm 等同类插件的命名一致）。"""
        cases = [
            ("/A/剧集.mkv", "/out/剧集.strm"),
            ("/A/剧集.mp4", "/out/剧集.strm"),
            ("/A/剧集.mkv.avi", "/out/剧集.mkv.strm"),      # 只替换最后一个扩展名
            ("/A/我的剧集.S01E01.2160p.mkv", "/out/我的剧集.S01E01.2160p.strm"),
        ]
        for remote, want in cases:
            got = strm_target_path("/out", remote, "/A")
            assert got == want, f"{remote} -> {got}，期望 {want}"

    def test_download_keeps_original_name(self):
        """下载实体文件保留原名，不加也不换后缀。"""
        got = strm_target_path("/out", "/Ani/中文/a.srt", "/Ani",
                               replace_extension=False, add_strm_suffix=False)
        assert got == "/out/中文/a.srt"

    def test_no_extension_gets_suffix(self):
        """无扩展名的文件追加 .strm，避免生成与目录同名的文件。"""
        assert strm_target_path("/out", "/A/movie", "/A") == "/out/movie.strm"

    def test_append_mode_still_available(self):
        """旧的「追加」风格仍可选择。"""
        got = strm_target_path("/out", "/A/x.mkv", "/A",
                               replace_extension=False, add_strm_suffix=True)
        assert got == "/out/x.mkv.strm"

    def test_video_and_strm_source_do_not_collide(self):
        """同目录 a.mkv 与 a.mkv.strm 不能映射到同一文件。

        替换规则下：a.mkv -> a.strm，a.mkv.strm -> a.mkv.strm（splitext 保留 .strm）。
        两者不同，不会互相覆盖。
        """
        a = strm_target_path("/out", "/A/a.mkv", "/A")
        b = strm_target_path("/out", "/A/a.mkv.strm", "/A")
        assert a != b, f"路径冲突：{a}"

    def test_suffix_is_lowercase(self):
        assert strm_target_path("/out", "/A/x.MKV", "/A").endswith(".strm")


class TestIterEntries:
    def test_standard_shape(self):
        payload = {"code": 200, "data": {"content": [{"name": "a"}, {"name": "b"}], "total": 2}}
        assert len(iter_entries(payload)) == 2

    def test_data_is_list(self):
        assert len(iter_entries({"code": 200, "data": [{"name": "a"}]})) == 1

    def test_missing_content(self):
        assert iter_entries({"code": 200, "data": {}}) == []

    def test_garbage_input(self):
        assert iter_entries(None) == []
        assert iter_entries("string") == []


class TestResponseOk:
    def test_int_200(self):
        assert response_ok({"code": 200})

    def test_str_200(self):
        assert response_ok({"code": "200"})

    def test_401(self):
        assert not response_ok({"code": 401})

    def test_garbage(self):
        assert not response_ok(None)
        assert not response_ok({"code": None})


# --------------------------------------------------------------------- 规则解析
class TestParseRules:
    def test_single_rule(self):
        rules, errors = parse_rules("/EmbyCloud#/volume1/video/strm/source")
        assert not errors
        assert rules[0].remote_path == "/EmbyCloud"
        assert rules[0].local_dir == "/volume1/video/strm/source"

    def test_multi_line(self):
        rules, _ = parse_rules("/A#/out/a\n/Ani#/out/ani")
        assert [r.remote_path for r in rules] == ["/A", "/Ani"]

    def test_pipe_separator(self):
        rules, _ = parse_rules("/A|/out/a")
        assert len(rules) == 1 and rules[0].local_dir == "/out/a"

    def test_comments_and_blank_lines_skipped(self):
        rules, _ = parse_rules("# 注释\n\n/A#/out")
        assert len(rules) == 1

    def test_missing_local_dir_reports_error(self):
        rules, errors = parse_rules("/only/path")
        assert rules == [] and errors

    def test_with_include_exclude(self):
        rules, errors = parse_rules("/A#/out#电影#预告")
        assert not errors
        assert rules[0].include == "电影" and rules[0].exclude == "预告"

    def test_invalid_regex_reported_but_kept(self):
        rules, errors = parse_rules("/A#/out#[bad(")
        assert rules and errors

    def test_matches_uses_regex(self):
        rules, _ = parse_rules("/A#/out#Movie#Sample")
        rule = rules[0]
        assert rule.matches("/A/Movie/x.mkv")
        assert not rule.matches("/A/Movie/Sample/x.mkv")
        assert not rule.matches("/A/TV/x.mkv")


# --------------------------------------------------------------------- 客户端
class FakeTransport:
    """可编排的假传输：按 URL 后缀返回预设响应，并记录调用。"""

    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def __call__(self, method, url, json=None, headers=None):
        self.calls.append((method, url, json, headers))
        for suffix, resp in self.responses.items():
            if url.endswith(suffix):
                # 支持传 callable 以模拟「第一次失败、第二次成功」
                return resp() if callable(resp) else resp
        return 404, {"code": 404, "message": "not found"}


class TestOpenListClient:
    def test_token_sent_without_bearer(self):
        transport = FakeTransport({
            "/api/fs/list": (200, {"code": 200, "data": {"content": [{"name": "a"}], "total": 1}}),
        })
        OpenListClient(BASE, token="mytoken", transport=transport).list_dir("/")
        auth = transport.calls[0][3]["Authorization"]
        assert auth == "mytoken" and not auth.startswith("Bearer")

    def test_login_when_no_token(self):
        transport = FakeTransport({
            "/api/auth/login": (200, {"code": 200, "data": {"token": "fresh"}}),
            "/api/fs/list": (200, {"code": 200, "data": {"content": []}}),
        })
        client = OpenListClient(BASE, username="u", password="p", transport=transport)
        client.list_dir("/")
        assert client._token == "fresh"
        assert transport.calls[0][1].endswith("/api/auth/login")

    def test_login_failure_raises(self):
        transport = FakeTransport({
            "/api/auth/login": (200, {"code": 401, "message": "Invalid username or password"}),
        })
        with pytest.raises(OpenListError):
            OpenListClient(BASE, username="u", password="bad", transport=transport).login()

    def test_401_on_list_gives_clear_error(self):
        transport = FakeTransport({
            "/api/fs/list": (200, {"code": 401, "message": "token is expired"}),
        })
        client = OpenListClient(BASE, token="expired", transport=transport)
        with pytest.raises(OpenListError) as exc:
            client.list_dir("/", allow_relogin=False)
        assert "未授权" in str(exc.value)

    def test_401_triggers_relogin_once(self):
        """token 失效时应自动重登并重试一次（OpenList token 默认 48h，且重启即失效）。"""
        state = {"n": 0}

        def list_resp():
            state["n"] += 1
            if state["n"] == 1:
                return 200, {"code": 401, "message": "token is invalidated"}
            return 200, {"code": 200, "data": {"content": [{"name": "a"}], "total": 1}}

        transport = FakeTransport({
            "/api/auth/login": (200, {"code": 200, "data": {"token": "new"}}),
            "/api/fs/list": list_resp,
        })
        client = OpenListClient(BASE, username="u", password="p", transport=transport)
        entries = client.list_dir("/")
        assert len(entries) == 1
        assert state["n"] == 2                      # 重试了一次
        assert client._token == "new"

    def test_per_page_zero_requests_all(self):
        """per_page=0 是 OpenList「返回全部」的约定，必须原样发送。"""
        transport = FakeTransport({
            "/api/fs/list": (200, {"code": 200, "data": {"content": []}}),
        })
        OpenListClient(BASE, token="t", transport=transport).list_dir("/Ani")
        body = transport.calls[0][2]
        assert body["per_page"] == 0
        assert body["path"] == "/Ani"
        assert body["refresh"] is False             # 只读遍历必须 false

    def test_object_not_found_message_is_readable(self):
        transport = FakeTransport({
            "/api/fs/list": (200, {"code": 500, "message": "object not found"}),
        })
        client = OpenListClient(BASE, token="t", transport=transport)
        # 默认容错：路径不存在不抛异常，返回空列表
        assert client.list_dir("/nope", tolerate_not_found=True) == []
        with pytest.raises(OpenListError):
            client.list_dir("/nope", tolerate_not_found=False)

    def test_network_failure_raises(self):
        client = OpenListClient(BASE, token="t", transport=lambda *a, **k: (0, None))
        with pytest.raises(OpenListError):
            client.list_dir("/")

    def test_missing_url_raises(self):
        client = OpenListClient("", token="t", transport=lambda *a, **k: (200, {}))
        with pytest.raises(OpenListError):
            client.list_dir("/")

    def test_base_path_detection(self):
        """账号 base_path 会影响 API 路径语义，需要显式暴露给上层。"""
        transport = FakeTransport({
            "/api/me": (200, {"code": 200, "data": {"base_path": "/EmbyCloud"}}),
            "/api/fs/list": (200, {"code": 200, "data": {"content": []}}),
        })
        client = OpenListClient(BASE, token="t", transport=transport)
        assert client.fetch_base_path() == "/EmbyCloud"


# --------------------------------------------------------------------- 扫描
def make_tree_transport(tree):
    """把 {路径: [条目]} 转成假传输；未知路径模拟 OpenList 的 code:500。"""

    def _t(method, url, json=None, headers=None):
        if url.endswith("/api/fs/list"):
            path = (json or {}).get("path", "/")
            if path in tree:
                return 200, {"code": 200, "data": {"content": tree[path], "total": len(tree[path])}}
            return 200, {"code": 500, "message": f"object not found: {path}"}
        return 404, {"code": 404}

    return _t


def d(name):
    return {"name": name, "is_dir": True, "size": 0}


def f(name, size=100):
    return {"name": name, "is_dir": False, "size": size}


class TestScan:
    def test_recursive_collects_videos(self):
        tree = {
            "/EmbyCloud": [d("Movie"), d("TV"), f("readme.txt")],
            "/EmbyCloud/Movie": [f("a.mkv"), f("b.mp4"), f("c.nfo")],
            "/EmbyCloud/TV": [d("S01")],
            "/EmbyCloud/TV/S01": [f("e01.mkv")],
        }
        rules, _ = parse_rules("/EmbyCloud#/out")
        client = OpenListClient(BASE, token="t", transport=make_tree_transport(tree))
        result = scan(client, rules, video_ext=VIDEO, download_ext=[".nfo"])

        assert result.videos_found == 3
        assert result.dirs_scanned == 4
        assert sorted(p for p, _ in result.planned) == [
            "/out/Movie/a.strm", "/out/Movie/b.strm", "/out/TV/S01/e01.strm"
        ]
        # 视频生成 strm；nfo 归入下载队列
        assert [p for p, _ in result.downloads] == ["/out/Movie/c.nfo"]

    def test_video_and_download_are_separated(self):
        """视频→strm，字幕/NFO→下载，两类产物必须分开。"""
        tree = {"/A": [f("movie.mkv"), f("movie.srt"), f("movie.nfo"), f("poster.jpg")]}
        rules, _ = parse_rules("/A#/out")
        client = OpenListClient(BASE, token="t", transport=make_tree_transport(tree))
        result = scan(client, rules, video_ext=VIDEO,
                      download_ext=[".srt", ".nfo", ".jpg"])

        assert [p for p, _ in result.planned] == ["/out/movie.strm"]
        assert sorted(p for p, _ in result.downloads) == [
            "/out/movie.nfo", "/out/movie.srt", "/out/poster.jpg"
        ]
        # strm 内容必须是 URL，下载项必须是 (本地, 远端) 且远端为绝对路径
        assert result.planned[0][1].startswith(BASE)
        assert all(remote.startswith("/A/") for _, remote in result.downloads)

    def test_download_records_remote_size(self):
        """下载项要记录远端大小，供判断是否需要更新。"""
        tree = {"/A": [{"name": "a.srt", "is_dir": False, "size": 2048}]}
        rules, _ = parse_rules("/A#/out")
        client = OpenListClient(BASE, token="t", transport=make_tree_transport(tree))
        result = scan(client, rules, video_ext=VIDEO, download_ext=[".srt"])
        assert result.remote_sizes == {"/A/a.srt": 2048}

    def test_unlisted_extension_is_skipped(self):
        """既不在视频也不在下载列表的扩展名应被忽略。"""
        tree = {"/A": [f("a.mkv"), f("b.xyz"), f("c.iso")]}
        rules, _ = parse_rules("/A#/out")
        client = OpenListClient(BASE, token="t", transport=make_tree_transport(tree))
        result = scan(client, rules, video_ext=VIDEO, download_ext=[".srt"])
        assert [p for p, _ in result.planned] == ["/out/a.strm"]
        assert result.downloads == []

    def test_strm_content_is_direct_url(self):
        tree = {"/Ani": [f("01.mp4")]}
        rules, _ = parse_rules("/Ani#/out")
        client = OpenListClient(BASE, token="t", transport=make_tree_transport(tree))
        result = scan(client, rules, video_ext=VIDEO)
        path, content = result.planned[0]
        assert path == "/out/01.strm"
        assert content == f"{BASE}/d/Ani/01.mp4"

    def test_exclude_skips_subtree(self):
        tree = {
            "/A": [d("keep"), d("skip")],
            "/A/keep": [f("a.mkv")],
            "/A/skip": [f("b.mkv")],
        }
        rules, _ = parse_rules("/A#/out##/skip")
        client = OpenListClient(BASE, token="t", transport=make_tree_transport(tree))
        result = scan(client, rules, video_ext=VIDEO)
        assert [p for p, _ in result.planned] == ["/out/keep/a.strm"]
        assert result.skipped_by_rule >= 1

    def test_include_filters_paths(self):
        tree = {"/A": [f("movie.mkv"), f("sample.mkv")]}
        rules, _ = parse_rules("/A#/out#movie")
        client = OpenListClient(BASE, token="t", transport=make_tree_transport(tree))
        result = scan(client, rules, video_ext=VIDEO)
        assert [p for p, _ in result.planned] == ["/out/movie.strm"]

    def test_include_does_not_prune_parent_dirs(self):
        """include 用于筛文件名时，不应因父目录不匹配而剪掉整棵子树。"""
        tree = {
            "/A": [d("Season 01")],
            "/A/Season 01": [f("movie.mkv"), f("other.mkv")],
        }
        rules, _ = parse_rules(r"/A#/out#\.mkv$")
        client = OpenListClient(BASE, token="t", transport=make_tree_transport(tree))
        result = scan(client, rules, video_ext=VIDEO)
        # 两个 .mkv 都符合 include，父目录 Season 01 虽不含 ".mkv" 也必须被递归
        assert sorted(p for p, _ in result.planned) == [
            "/out/Season 01/movie.strm", "/out/Season 01/other.strm"
        ]

    def test_include_matching_nothing_yields_no_error(self):
        tree = {"/A": [f("a.mkv")]}
        rules, _ = parse_rules("/A#/out#nomatch-xyz")
        client = OpenListClient(BASE, token="t", transport=make_tree_transport(tree))
        result = scan(client, rules, video_ext=VIDEO)
        assert result.planned == [] and not result.errors

    def test_error_in_one_rule_does_not_abort_others(self):
        """某条规则的目录不可达时，其它规则仍应继续执行。"""
        tree = {"/Good": [f("a.mkv")]}

        def transport(method, url, json=None, headers=None):
            if url.endswith("/api/fs/list"):
                path = (json or {}).get("path", "/")
                if path.startswith("/Bad"):
                    return 0, None                      # 模拟网络失败
                if path in tree:
                    return 200, {"code": 200, "data": {"content": tree[path], "total": len(tree[path])}}
            return 404, {"code": 404}

        rules, _ = parse_rules("/Bad#/out1\n/Good#/out2")
        client = OpenListClient(BASE, token="t", transport=transport)
        result = scan(client, rules, video_ext=VIDEO)
        assert [p for p, _ in result.planned] == ["/out2/a.strm"]
        assert result.errors                        # 失败的那条被记录

    def test_missing_path_does_not_count_as_error(self):
        """路径不存在（object not found）属正常情况，不应污染错误列表。"""
        rules, _ = parse_rules("/Gone#/out")
        client = OpenListClient(BASE, token="t", transport=make_tree_transport({}))
        result = scan(client, rules, video_ext=VIDEO)
        assert result.planned == []
        assert not result.errors

    def test_cancel_stops_scan(self):
        tree = {"/A": [f("a.mkv")]}
        rules, _ = parse_rules("/A#/out")
        client = OpenListClient(BASE, token="t", transport=make_tree_transport(tree))
        result = scan(client, rules, video_ext=VIDEO, should_cancel=lambda: True)
        assert result.cancelled and result.planned == []

    def test_max_depth_guard(self):
        tree = {"/A": [d("l1")], "/A/l1": [d("l2")], "/A/l1/l2": [f("deep.mkv")]}
        rules, _ = parse_rules("/A#/out")
        client = OpenListClient(BASE, token="t", transport=make_tree_transport(tree))
        result = scan(client, rules, video_ext=VIDEO, max_depth=1)
        assert result.planned == [] and result.errors

    def test_empty_directory(self):
        rules, _ = parse_rules("/A#/out")
        client = OpenListClient(BASE, token="t", transport=make_tree_transport({"/A": []}))
        result = scan(client, rules, video_ext=VIDEO)
        assert result.planned == [] and not result.errors

    def test_chinese_filename_encoded_in_url_but_kept_on_disk(self):
        tree = {"/Ani": [f("中文名.mkv")]}
        rules, _ = parse_rules("/Ani#/out")
        client = OpenListClient(BASE, token="t", transport=make_tree_transport(tree))
        result = scan(client, rules, video_ext=VIDEO)
        path, content = result.planned[0]
        assert path == "/out/中文名.strm"
        assert "中文" not in content

    def test_base_path_prepended_for_direct_url(self):
        """账号 base_path 非根时：API 走相对路径，/d/ 直链必须带上 base_path 前缀。"""
        tree = {"/Ani": [f("01.mkv")]}          # API 看到的路径（服务端已拼 base_path）
        rules, _ = parse_rules("/Ani#/out")
        client = OpenListClient(BASE, token="t", transport=make_tree_transport(tree))
        result = scan(client, rules, video_ext=VIDEO, base_path="/EmbyCloud")
        path, content = result.planned[0]
        assert path == "/out/01.strm"
        assert content == f"{BASE}/d/EmbyCloud/Ani/01.mkv"

    def test_base_path_not_duplicated(self):
        """用户已在规则里写了完整前缀时，不应重复拼接。"""
        tree = {"/EmbyCloud/Ani": [f("01.mkv")]}
        rules, _ = parse_rules("/EmbyCloud/Ani#/out")
        client = OpenListClient(BASE, token="t", transport=make_tree_transport(tree))
        result = scan(client, rules, video_ext=VIDEO, base_path="/EmbyCloud")
        _, content = result.planned[0]
        assert content == f"{BASE}/d/EmbyCloud/Ani/01.mkv"
        assert "/EmbyCloud/EmbyCloud/" not in content

    def test_root_base_path_unchanged(self):
        tree = {"/Ani": [f("01.mkv")]}
        rules, _ = parse_rules("/Ani#/out")
        client = OpenListClient(BASE, token="t", transport=make_tree_transport(tree))
        result = scan(client, rules, video_ext=VIDEO, base_path="/")
        _, content = result.planned[0]
        assert content == f"{BASE}/d/Ani/01.mkv"
