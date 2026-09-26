"""下载器与文件分类测试。

覆盖「视频生成 strm / 字幕等下载实体文件」这条核心策略的实现正确性。

运行：
    python -m pytest tests/v3/openliststrm -v
"""

import importlib.util
import sys
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
    for sub in ("strmutil", "openlist", "treecache", "scanner", "tasks", "cleanup", "downloader"):
        name = f"{PKG_NAME}.{sub}"
        spec = importlib.util.spec_from_file_location(name, PLUGIN_DIR / f"{sub}.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)


_load()

from mp_plugin_openliststrm.downloader import (  # noqa: E402
    DownloadStats,
    atomic_write_bytes,
    download_all,
    human_size,
    needs_download,
)
from mp_plugin_openliststrm.strmutil import (  # noqa: E402
    DEFAULT_DOWNLOAD_EXT,
    DEFAULT_SKIP_DIRS,
    DEFAULT_SKIP_FILES,
    DEFAULT_VIDEO_EXT,
    classify_output,
    compile_globs,
    parse_multiline_list,
    should_skip_dir,
    should_skip_file,
)


# ===================================================================== 无关文件过滤
class TestShouldSkipDir:
    @pytest.mark.parametrize("name", [
        "@eaDir", "@tmp", "#recycle", "$RECYCLE.BIN", "lost+found",
        ".git", ".stfolder", ".Trash-1000", "System Volume Information",
    ])
    def test_builtin_junk_dirs_skipped(self, name):
        assert should_skip_dir(name, DEFAULT_SKIP_DIRS)

    def test_hidden_dir_skipped(self):
        assert should_skip_dir(".hidden", DEFAULT_SKIP_DIRS)

    def test_case_insensitive(self):
        assert should_skip_dir("@EADIR", DEFAULT_SKIP_DIRS)
        assert should_skip_dir("recycle", ["recycle"])

    @pytest.mark.parametrize("name", ["电影", "Ani", "Season 01", "2019-1"])
    def test_normal_dirs_kept(self, name):
        assert not should_skip_dir(name, DEFAULT_SKIP_DIRS)

    def test_empty_skipped(self):
        assert should_skip_dir("", DEFAULT_SKIP_DIRS)

    def test_user_extra_patterns(self):
        patterns = compile_globs(["*备份*", "temp*"])
        assert should_skip_dir("我的备份目录", DEFAULT_SKIP_DIRS, patterns)
        assert should_skip_dir("tempdir", DEFAULT_SKIP_DIRS, patterns)
        assert not should_skip_dir("电影", DEFAULT_SKIP_DIRS, patterns)


class TestShouldSkipFile:
    @pytest.mark.parametrize("name", [
        "Thumbs.db", "desktop.ini", ".DS_Store",
        "movie.tmp", "movie.part", "x.crdownload", "y.!qb", "z.aria2",
        "快捷方式.url", "广告.jpg", "最新地址.txt", "page.html",
    ])
    def test_builtin_junk_files_skipped(self, name):
        patterns = compile_globs(DEFAULT_SKIP_FILES)
        assert should_skip_file(name, (), patterns)

    @pytest.mark.parametrize("name", ["movie.mkv", "sub.srt", "poster.jpg", "tvshow.nfo"])
    def test_media_files_kept(self, name):
        patterns = compile_globs(DEFAULT_SKIP_FILES)
        assert not should_skip_file(name, (), patterns)

    def test_plain_txt_not_in_default_skip_list(self):
        """普通 txt 不在默认跳过表内——它会在分类阶段被自然忽略，无需额外过滤。"""
        patterns = compile_globs(DEFAULT_SKIP_FILES)
        assert not should_skip_file("readme.txt", (), patterns)
        assert classify_output("readme.txt", DEFAULT_VIDEO_EXT, DEFAULT_DOWNLOAD_EXT) == "skip"

    def test_user_extra_patterns(self):
        patterns = compile_globs(DEFAULT_SKIP_FILES + ["sample.*", "*.exe"])
        assert should_skip_file("Sample.mkv", (), patterns)
        assert should_skip_file("tool.exe", (), patterns)
        assert not should_skip_file("movie.mkv", (), patterns)

    def test_exact_name_match(self):
        assert should_skip_file("custom.dat", ["custom.dat"])
        assert not should_skip_file("other.dat", ["custom.dat"])

    def test_question_mark_wildcard(self):
        patterns = compile_globs(["file?.txt"])
        assert should_skip_file("file1.txt", (), patterns)
        assert not should_skip_file("file12.txt", (), patterns)


class TestParseMultilineList:
    def test_one_per_line(self):
        assert parse_multiline_list("a\nb\nc") == ["a", "b", "c"]

    def test_comma_separated(self):
        assert parse_multiline_list("a,b,c") == ["a", "b", "c"]

    def test_mixed(self):
        assert parse_multiline_list("a,b\nc") == ["a", "b", "c"]

    def test_chinese_comma(self):
        assert parse_multiline_list("a，b") == ["a", "b"]

    def test_comments_and_blanks_ignored(self):
        assert parse_multiline_list("# 注释\n\na\n") == ["a"]

    def test_empty(self):
        assert parse_multiline_list("") == []
        assert parse_multiline_list(None) == []


class TestCompileGlobs:
    def test_valid_patterns(self):
        assert len(compile_globs(["*.txt", "a?c"])) == 2

    def test_empty_and_blank_skipped(self):
        assert compile_globs(["", "  ", None]) == []

    def test_case_insensitive_matching(self):
        patterns = compile_globs(["*.TXT"])
        assert patterns[0].match("readme.txt")
        assert patterns[0].match("README.TXT")


# ===================================================================== 文件分类
class TestClassifyOutput:
    @pytest.mark.parametrize("name", ["a.mkv", "b.MP4", "c.TS"])
    def test_video_goes_to_strm(self, name):
        assert classify_output(name, DEFAULT_VIDEO_EXT, DEFAULT_DOWNLOAD_EXT) == "strm"

    @pytest.mark.parametrize("name", [
        "a.srt", "b.ass", "c.ssa", "d.sup", "e.vtt",   # 字幕
        "f.nfo", "g.xml",                              # 元数据
        "h.jpg", "i.png", "j.webp",                    # 图片
    ])
    def test_auxiliary_goes_to_download(self, name):
        assert classify_output(name, DEFAULT_VIDEO_EXT, DEFAULT_DOWNLOAD_EXT) == "download"

    @pytest.mark.parametrize("name", ["a.xyz", "b.rar", "noext", "c."])
    def test_unknown_skipped(self, name):
        assert classify_output(name, DEFAULT_VIDEO_EXT, DEFAULT_DOWNLOAD_EXT) == "skip"

    def test_case_insensitive(self):
        assert classify_output("A.SRT", DEFAULT_VIDEO_EXT, DEFAULT_DOWNLOAD_EXT) == "download"
        assert classify_output("A.MKV", DEFAULT_VIDEO_EXT, DEFAULT_DOWNLOAD_EXT) == "strm"

    def test_video_takes_priority_when_overlapping(self):
        """同一扩展名同时出现在两类里时，优先生成 strm。"""
        assert classify_output("a.mkv", [".mkv"], [".mkv"]) == "strm"

    def test_custom_lists_respected(self):
        assert classify_output("a.flac", [".mkv"], [".flac"]) == "download"
        assert classify_output("a.flac", [".flac"], [".srt"]) == "strm"


# ===================================================================== 下载判定
class TestNeedsDownload:
    def test_missing_file_needs_download(self, tmp_path):
        need, update = needs_download(tmp_path / "nope.srt", 100)
        assert need and not update

    def test_same_size_skipped(self, tmp_path):
        f = tmp_path / "a.srt"
        f.write_bytes(b"x" * 100)
        need, update = needs_download(f, 100)
        assert not need and not update

    def test_different_size_is_update(self, tmp_path):
        f = tmp_path / "a.srt"
        f.write_bytes(b"x" * 50)
        need, update = needs_download(f, 100)
        assert need and update

    def test_unknown_remote_size_skips_existing(self, tmp_path):
        """远端没给大小时，本地已有就跳过，避免无谓重复下载。"""
        f = tmp_path / "a.srt"
        f.write_bytes(b"x" * 50)
        need, _ = needs_download(f, 0)
        assert not need

    def test_unknown_remote_size_downloads_missing(self, tmp_path):
        need, _ = needs_download(tmp_path / "nope.srt", 0)
        assert need

    def test_directory_treated_as_missing(self, tmp_path):
        d = tmp_path / "adir"
        d.mkdir()
        need, _ = needs_download(d, 0)
        assert need


# ===================================================================== 原子写入
class TestAtomicWrite:
    def test_creates_file_and_parents(self, tmp_path):
        dest = tmp_path / "sub" / "dir" / "a.srt"
        atomic_write_bytes(dest, b"hello")
        assert dest.read_bytes() == b"hello"

    def test_overwrites_existing(self, tmp_path):
        dest = tmp_path / "a.srt"
        dest.write_bytes(b"old")
        atomic_write_bytes(dest, b"new")
        assert dest.read_bytes() == b"new"

    def test_no_leftover_part_files(self, tmp_path):
        dest = tmp_path / "a.srt"
        atomic_write_bytes(dest, b"data")
        leftovers = list(tmp_path.glob("*.part"))
        assert leftovers == []


# ===================================================================== 批量下载
class FakeDownloadTransport:
    """可编排的下载实现，记录调用。"""

    def __init__(self, content=b"data", fail_on=None):
        self.content = content
        self.fail_on = set(fail_on or [])
        self.calls = []

    def __call__(self, url, dest, timeout):
        self.calls.append((url, str(dest)))
        if url in self.fail_on:
            return False, "模拟失败"
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(self.content)
        return True, "OK"


class TestDownloadAll:
    def test_downloads_missing_files(self, tmp_path):
        transport = FakeDownloadTransport(b"abc")
        stats = download_all(
            [(str(tmp_path / "a.srt"), "/A/a.srt"),
             (str(tmp_path / "b.nfo"), "/A/b.nfo")],
            transport=transport,
        )
        assert stats.downloaded == 2
        assert stats.bytes_total == 6
        assert len(transport.calls) == 2
        assert (tmp_path / "a.srt").read_bytes() == b"abc"

    def test_skips_existing_same_size(self, tmp_path):
        target = tmp_path / "a.srt"
        target.write_bytes(b"abc")
        transport = FakeDownloadTransport(b"abc")
        stats = download_all(
            [(str(target), "/A/a.srt")],
            remote_sizes={"/A/a.srt": 3},
            transport=transport,
        )
        assert stats.skipped == 1
        assert stats.downloaded == 0
        assert transport.calls == []                 # 未发起请求

    def test_updates_when_size_differs(self, tmp_path):
        target = tmp_path / "a.srt"
        target.write_bytes(b"old")
        transport = FakeDownloadTransport(b"newcontent")
        stats = download_all(
            [(str(target), "/A/a.srt")],
            remote_sizes={"/A/a.srt": 10},
            transport=transport,
        )
        assert stats.updated == 1
        assert stats.downloaded == 0
        assert target.read_bytes() == b"newcontent"

    def test_force_redownloads(self, tmp_path):
        target = tmp_path / "a.srt"
        target.write_bytes(b"same")
        transport = FakeDownloadTransport(b"same")
        stats = download_all(
            [(str(target), "/A/a.srt")],
            remote_sizes={"/A/a.srt": 4},
            transport=transport, force=True,
        )
        assert stats.updated == 1                    # 强制时记为更新
        assert len(transport.calls) == 1

    def test_failure_is_isolated(self, tmp_path):
        """单个文件失败不应影响其它文件。"""
        transport = FakeDownloadTransport(b"ok", fail_on=["/A/bad.srt"])
        stats = download_all(
            [(str(tmp_path / "a.srt"), "/A/a.srt"),
             (str(tmp_path / "bad.srt"), "/A/bad.srt"),
             (str(tmp_path / "c.srt"), "/A/c.srt")],
            transport=transport,
        )
        assert stats.downloaded == 2
        assert stats.failed == 1
        assert stats.errors
        assert (tmp_path / "a.srt").exists()
        assert (tmp_path / "c.srt").exists()
        assert not (tmp_path / "bad.srt").exists()

    def test_cancel_stops_early(self, tmp_path):
        transport = FakeDownloadTransport(b"x")
        stats = download_all(
            [(str(tmp_path / f"{i}.srt"), f"/A/{i}.srt") for i in range(5)],
            transport=transport,
            should_cancel=lambda: True,
        )
        assert stats.total == 0
        assert transport.calls == []

    def test_max_files_limit(self, tmp_path):
        transport = FakeDownloadTransport(b"x")
        stats = download_all(
            [(str(tmp_path / f"{i}.srt"), f"/A/{i}.srt") for i in range(10)],
            transport=transport,
            max_files=3,
        )
        assert stats.downloaded == 3
        assert stats.errors                          # 提示达到上限

    def test_empty_list(self, tmp_path):
        stats = download_all([], transport=FakeDownloadTransport())
        assert stats.total == 0


class TestDownloadStats:
    def test_summary(self):
        stats = DownloadStats(downloaded=3, skipped=1, updated=2, failed=1, bytes_total=2048)
        text = stats.summary()
        assert "下载 3" in text
        assert "更新 2" in text
        assert "跳过 1" in text
        assert "失败 1" in text
        assert "KB" in text

    def test_total(self):
        assert DownloadStats(downloaded=1, skipped=2, updated=3, failed=4).total == 10


class TestHumanSize:
    @pytest.mark.parametrize("num,expected_unit", [
        (0, "B"), (512, "B"), (1024, "KB"), (1048576, "MB"), (1073741824, "GB"),
    ])
    def test_units(self, num, expected_unit):
        assert human_size(num).endswith(expected_unit)
