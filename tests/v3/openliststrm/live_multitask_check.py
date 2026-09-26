"""真实环境验证：行式任务配置 + 无关文件过滤 + 每任务独立服务器。

只读远端；strm 写入临时目录，验证后自动清理。

用法：
    python tests/v3/openliststrm/live_multitask_check.py <base_url> <token>
"""

import importlib.util
import sys
import tempfile
import types
from pathlib import Path

PLUGIN_ID = "openliststrm"
PLUGIN_DIR = Path(__file__).resolve().parents[3] / "plugins.v3" / PLUGIN_ID
PKG = f"mp_plugin_{PLUGIN_ID}"

pkg = types.ModuleType(PKG)
pkg.__path__ = [str(PLUGIN_DIR)]
sys.modules[PKG] = pkg
for sub in ("strmutil", "openlist", "treecache", "scanner", "tasks", "cleanup", "downloader"):
    spec = importlib.util.spec_from_file_location(f"{PKG}.{sub}", PLUGIN_DIR / f"{sub}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"{PKG}.{sub}"] = mod
    spec.loader.exec_module(mod)

from mp_plugin_openliststrm.openlist import OpenListClient  # noqa: E402
from mp_plugin_openliststrm.scanner import parse_rules, scan  # noqa: E402
from mp_plugin_openliststrm.strmutil import (  # noqa: E402
    DEFAULT_DOWNLOAD_EXT,
    DEFAULT_VIDEO_EXT,
    parse_multiline_list,
)
from mp_plugin_openliststrm.tasks import parse_tasks, tasks_to_text  # noqa: E402

try:
    import requests
except ImportError:
    print("需要 requests")
    sys.exit(1)


def transport(method, url, json=None, headers=None):
    try:
        if method.upper() == "GET":
            r = requests.get(url, headers=headers, params=json, timeout=30)
        else:
            r = requests.post(url, headers=headers, json=json, timeout=30)
    except Exception:
        return 0, None
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, None


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(2)
    base, token = sys.argv[1].rstrip("/"), sys.argv[2]

    print("=" * 78)
    print("行式配置 + 文件过滤 真实环境验证")
    print("=" * 78)

    # ---------------- 1. 行式任务配置 ----------------
    print("\n[1] 行式任务配置解析")
    config_text = (
        "# 电影库（第一个 OpenList 实例）\n"
        f"电影 | {base} | {token} | /EmbyCloud/电影#/tmp/out-movie | 0 30 4 * * *\n"
        "\n"
        "# 动漫库（可指向另一个 OpenList，此处复用同一实例演示）\n"
        f"动漫 | {base} | {token} | /Ani#/tmp/out-ani | 0 30 6 * * * | detect\n"
    )
    tasks, warnings = parse_tasks(config_text)
    print(f"    解析出 {len(tasks)} 个任务")
    for t in tasks:
        print(f"      · {t.display_name:6} url={t.openlist_url}  凭据={t.credential}")
        print(f"        规则={t.rules!r}  周期={t.cron!r}  强制={t.force_overwrite} 检测={t.detect_broken}")
    if warnings:
        print(f"    提示：{warnings}")
    assert len(tasks) == 2, "应解析出 2 个任务"

    # 往返测试
    again, _ = parse_tasks(tasks_to_text(tasks))
    assert len(again) == 2 and again[0].to_json() == tasks[0].to_json()
    print("    ✓ 行式格式可往返（解析→序列化→再解析一致）")

    # ---------------- 2. 连接与过滤 ----------------
    client = OpenListClient(base_url=base, token=token, transport=transport)
    client.login()
    print("\n[2] 认证成功")

    skip_dirs = parse_multiline_list("")          # 用内置默认
    skip_files = parse_multiline_list("我的自定义排除*")

    with tempfile.TemporaryDirectory():
        rules, _ = parse_rules("/EmbyCloud/电影#/tmp/out-movie")
        print("\n[3] 扫描 /EmbyCloud/电影（含过滤）")
        result = scan(
            client, rules,
            video_ext=DEFAULT_VIDEO_EXT,
            download_ext=DEFAULT_DOWNLOAD_EXT,
            skip_dirs=skip_dirs,
            skip_files=skip_files,
        )
        print(f"    目录 {result.dirs_scanned} 个    文件 {result.files_seen} 个")
        print(f"    视频→strm {result.videos_found} 个")
        print(f"    附属→下载 {result.metas_found} 个")
        print(f"    跳过目录 {result.skipped_dirs} 个    跳过文件 {result.skipped_files} 个")
        if result.errors:
            for e in result.errors[:3]:
                print(f"    警告：{e}")

        # 展示过滤效果
        print("\n[4] 过滤效果抽样")
        for local, _ in result.planned[:3]:
            print(f"    strm  {Path(local).name[:70]}")
        for local, _ in result.downloads[:3]:
            print(f"    下载  {Path(local).name[:70]}")

        # 验证：被跳过的文件确实没有出现在产物里
        junk_like = [p for p, _ in result.downloads
                     if Path(p).suffix.lower() in (".txt", ".html", ".url", ".tmp", ".part")]
        print(f"\n    产物中的垃圾文件数量：{len(junk_like)}（应为 0）")
        assert not junk_like, "过滤未生效"

    # ---------------- 3. 多实例能力 ----------------
    print("\n[5] 多 OpenList 实例能力")
    print("    每个任务独立保存 url+凭据，可同时对接不同实例：")
    for t in tasks:
        print(f"      {t.display_name} -> {t.openlist_url}")

    print("\n" + "=" * 78)
    print("✅ 验证完成：行式配置可用、过滤生效、多实例结构就绪")
    print("=" * 78)


if __name__ == "__main__":
    main()
