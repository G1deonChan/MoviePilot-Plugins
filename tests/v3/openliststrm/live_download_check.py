"""在真实 OpenList 上验证「视频→strm、字幕→下载」策略。

只读远端；下载的文件写入临时目录，验证后删除。

用法：
    python tests/v3/openliststrm/live_download_check.py <base_url> <token> <扫描路径>
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

from mp_plugin_openliststrm.downloader import download_all  # noqa: E402
from mp_plugin_openliststrm.openlist import OpenListClient  # noqa: E402
from mp_plugin_openliststrm.scanner import parse_rules, scan  # noqa: E402
from mp_plugin_openliststrm.strmutil import (  # noqa: E402
    DEFAULT_DOWNLOAD_EXT,
    DEFAULT_VIDEO_EXT,
)

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


def download_transport(url, dest, timeout):
    """用 requests 流式下载。"""
    try:
        with requests.get(url, stream=True, timeout=timeout) as r:
            if r.status_code >= 400:
                return False, f"HTTP {r.status_code}"
            dest.parent.mkdir(parents=True, exist_ok=True)
            with open(dest, "wb") as fh:
                for chunk in r.iter_content(64 * 1024):
                    if chunk:
                        fh.write(chunk)
        return True, "OK"
    except Exception as err:
        return False, str(err)


def main():
    if len(sys.argv) < 4:
        print(__doc__)
        sys.exit(2)
    base, token, target = sys.argv[1].rstrip("/"), sys.argv[2], sys.argv[3]

    print("=" * 78)
    print("视频→strm / 字幕→下载 策略验证")
    print(f"实例：{base}   扫描：{target}")
    print("=" * 78)

    client = OpenListClient(base_url=base, token=token, transport=transport)
    client.login()
    print("\n[认证] 成功")

    # 列出目标目录，先看文件类型构成
    entries = client.list_dir(target)
    print(f"\n[目录构成] {target} 共 {len(entries)} 项")
    by_ext = {}
    for e in entries:
        if e.get("is_dir"):
            continue
        ext = Path(str(e.get("name", ""))).suffix.lower()
        by_ext[ext] = by_ext.get(ext, 0) + 1
    for ext, count in sorted(by_ext.items(), key=lambda x: -x[1])[:12]:
        kind = "→ strm" if ext in DEFAULT_VIDEO_EXT else (
            "→ 下载" if ext in DEFAULT_DOWNLOAD_EXT else "→ 忽略")
        print(f"    {ext or '(无扩展名)':12} {count:5} 个  {kind}")

    # 扫描（用临时输出目录）
    with tempfile.TemporaryDirectory() as tmp:
        rules, _ = parse_rules(f"{target}#{tmp}")
        print("\n[扫描] 进行分类…")
        result = scan(
            client, rules,
            video_ext=DEFAULT_VIDEO_EXT,
            download_ext=DEFAULT_DOWNLOAD_EXT,
        )
        print(f"    视频（生成 strm）：{result.videos_found}")
        print(f"    附属文件（待下载）：{result.metas_found}")
        print(f"    远端文件总数：{result.files_seen}")

        if result.errors:
            print(f"    错误：{result.errors[:3]}")

        # 真正下载几个附属文件验证可行性
        sample = result.downloads[:3]
        if not sample:
            print("\n[下载] 该目录没有可下载的附属文件，跳过")
        else:
            print(f"\n[下载] 抽样下载 {len(sample)} 个附属文件")
            # 远端路径 -> 直链（与插件内 _download_files 的处理一致）
            items = [(local, client.direct_url(remote)) for local, remote in sample]
            sizes = {
                client.direct_url(remote): result.remote_sizes.get(remote, 0)
                for _, remote in sample
            }
            for _, remote in sample:
                print(f"    {remote}")
                print(f"      -> {client.direct_url(remote)[:110]}")
            stats = download_all(
                items,
                remote_sizes=sizes,
                transport=download_transport,
            )
            print(f"    结果：{stats.summary()}")
            for local, _ in sample:
                p = Path(local)
                if p.exists():
                    print(f"      {p.name:24} {p.stat().st_size:>9} 字节  OK")
            if stats.errors:
                for e in stats.errors:
                    print(f"      失败：{e}")

    print("\n" + "=" * 78)
    print("完成（远端只读；下载内容位于临时目录，已自动清理）")
    print("=" * 78)


if __name__ == "__main__":
    main()
