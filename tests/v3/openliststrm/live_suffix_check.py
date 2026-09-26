"""验证 .strm 后缀修复：真实实例端到端跑 scan → 落盘 → 检测。

只读远端；产物写入临时目录，验证后自动删除。

用法：
    python tests/v3/openliststrm/live_suffix_check.py <base_url> <token>
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
    m = importlib.util.module_from_spec(spec)
    sys.modules[f"{PKG}.{sub}"] = m
    spec.loader.exec_module(m)

from mp_plugin_openliststrm.cleanup import (  # noqa: E402
    collect_broken,
    iter_strm_files,
    url_host,
)
from mp_plugin_openliststrm.openlist import OpenListClient  # noqa: E402
from mp_plugin_openliststrm.scanner import parse_rules, scan  # noqa: E402

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

    print("=" * 76)
    print("验证 .strm 后缀闭环：scan → 落盘 → 检测/预览")
    print("=" * 76)

    client = OpenListClient(base_url=base, token=token, transport=transport)
    client.login()
    print("\n[1] 认证成功")

    target = "/Ani/2019-1"
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "out"
        rules, _ = parse_rules(f"{target}#{out}")

        print(f"\n[2] 扫描 {target}")
        result = scan(client, rules)
        print(f"    目录 {result.dirs_scanned}，视频 {result.videos_found}，"
              f"附属 {result.metas_found}")
        print(f"    strm 计划 {result.planned_count}，下载计划 {result.download_count}")

        # 落盘（复刻插件行为）
        for local, content in result.planned:
            p = Path(local)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
        for local, _remote in result.downloads:
            p = Path(local)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("x", encoding="utf-8")

        print("\n[3] 磁盘产物统计")
        all_files = [p for p in out.rglob("*") if p.is_file()]
        strm_files = [p for p in out.rglob("*.strm")]
        print(f"    磁盘文件总数       : {len(all_files)}")
        print(f"    其中 *.strm 匹配   : {len(strm_files)}")
        print(f"    计划 strm 数       : {result.planned_count}")

        ok1 = len(strm_files) == result.planned_count
        print(f"    {'PASS' if ok1 else 'FAIL'}  strm 产物全部可被 *.strm 匹配")

        if result.planned:
            print("\n    样本（前 3 个）:")
            for p in strm_files[:3]:
                print(f"      {p.name[:66]}")

        if result.downloads:
            print("\n    下载项样本（不得带 .strm 后缀）:")
            for local, _ in result.downloads[:3]:
                print(f"      {Path(local).name[:66]}")
            bad = [p for p in all_files if p.name.endswith(".strm")
                   and any(str(p) == local for local, _ in result.downloads)]
            print(f"    {'PASS' if not bad else 'FAIL'}  下载项未被误加 .strm 后缀")

        print("\n[4] iter_strm_files（检测/清空用的遍历）")
        found = list(iter_strm_files(out))
        print(f"    发现 {len(found)} 个 strm")
        ok2 = len(found) == result.planned_count
        print(f"    {'PASS' if ok2 else 'FAIL'}  遍历结果与产物数量一致")

        print("\n[5] collect_broken（失效检测）")
        plan = collect_broken(rules, client=client, remote_existing=None,
                              expected_host=url_host(base))
        print(f"    total_scanned = {plan.total_scanned}（修复前为 0）")
        print(f"    broken = {plan.count}")
        ok3 = plan.total_scanned == result.planned_count
        print(f"    {'PASS' if ok3 else 'FAIL'}  检测流程能看到全部产物")
        # 这些都是有效文件，normalize 后不应被判失效
        ok4 = plan.count == 0
        print(f"    {'PASS' if ok4 else 'FAIL'}  有效 strm 未被误判失效")

        print("\n" + "=" * 76)
        if ok1 and ok2 and ok3 and ok4:
            print("✅ 全部通过：strm 命名闭环正确")
        else:
            print("❌ 存在失败项")
            sys.exit(1)
        print("=" * 76)


if __name__ == "__main__":
    main()
