"""在真实 OpenList 实例上验证目录树缓存的加速效果。

只读遍历，不写入任何 strm 文件。

用法：
    python tests/v3/openliststrm/live_cache_bench.py <base_url> <token> [扫描路径] [目录数上限]
"""

import importlib.util
import sys
import time
import types
from pathlib import Path

PLUGIN_ID = "openliststrm"
PLUGIN_DIR = Path(__file__).resolve().parents[3] / "plugins.v3" / PLUGIN_ID
PKG = f"mp_plugin_{PLUGIN_ID}"

pkg = types.ModuleType(PKG)
pkg.__path__ = [str(PLUGIN_DIR)]
sys.modules[PKG] = pkg
for sub in ("strmutil", "openlist", "treecache", "scanner"):
    spec = importlib.util.spec_from_file_location(f"{PKG}.{sub}", PLUGIN_DIR / f"{sub}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"{PKG}.{sub}"] = mod
    spec.loader.exec_module(mod)

from mp_plugin_openliststrm.openlist import OpenListClient  # noqa: E402
from mp_plugin_openliststrm.scanner import parse_rules, scan  # noqa: E402
from mp_plugin_openliststrm.treecache import TreeCache  # noqa: E402

try:
    import requests
except ImportError:
    print("需要 requests")
    sys.exit(1)


def make_transport(counter):
    """带请求计数的传输层。"""

    def _t(method, url, json=None, headers=None):
        counter["n"] += 1
        try:
            if method.upper() == "GET":
                r = requests.get(url, headers=headers, params=json, timeout=30)
            else:
                r = requests.post(url, headers=headers, json=json, timeout=60)
        except Exception:
            return 0, None
        try:
            body = r.json()
        except Exception:
            return r.status_code, None
        return r.status_code, body

    return _t


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(2)

    base = sys.argv[1].rstrip("/")
    token = sys.argv[2]
    target = sys.argv[3] if len(sys.argv) > 3 else "/Ani"
    limit = int(sys.argv[4]) if len(sys.argv) > 4 else 0

    print("=" * 78)
    print(f"目录树缓存加速验证：{base}")
    print(f"扫描路径：{target}   目录数上限：{limit or '不限'}")
    print("=" * 78)

    counter = {"n": 0}
    client = OpenListClient(base_url=base, token=token, transport=make_transport(counter))
    client.login()
    print(f"\n[认证] 成功，token 长度 {len(client._token)}")

    # 用临时目录放缓存，测试完删除
    cache_dir = Path(__file__).parent / ".bench_cache"
    cache_dir.mkdir(exist_ok=True)
    cache_path = cache_dir / "bench.json"
    if cache_path.exists():
        cache_path.unlink()

    rules, _ = parse_rules(f"{target}#/tmp/out")
    if limit:
        rules[0].exclude = ""       # 占位，不改语义

    # ---------------- 第一次扫描（冷缓存） ----------------
    print("\n" + "-" * 78)
    print("[1] 冷缓存扫描（首次，需逐目录请求）")
    cache1 = TreeCache(cache_path)
    counter["n"] = 0
    t0 = time.time()
    r1 = scan(client, rules, cache=cache1)
    t1 = time.time()
    cache1.save()
    print(f"    耗时：{t1 - t0:.1f}s   请求数：{counter['n']}")
    print(f"    目录：{r1.dirs_scanned}   视频：{r1.videos_found}   strm 计划：{r1.planned_count}")
    print(f"    缓存：{cache1.stats()}")

    if r1.dirs_scanned == 0:
        print("\n该路径下没有目录，无法验证缓存效果")
        return

    # ---------------- 第二次扫描（热缓存） ----------------
    print("\n" + "-" * 78)
    print("[2] 热缓存扫描（第二次，目录 mtime 未变时应命中缓存）")
    cache2 = TreeCache(cache_path)
    cache2.load()
    counter["n"] = 0
    t2 = time.time()
    r2 = scan(client, rules, cache=cache2)
    t3 = time.time()
    print(f"    耗时：{t3 - t2:.1f}s   请求数：{counter['n']}")
    print(f"    目录：{r2.dirs_scanned}   视频：{r2.videos_found}   strm 计划：{r2.planned_count}")
    print(f"    缓存：{cache2.stats()}")

    # ---------------- 结论 ----------------
    print("\n" + "=" * 78)
    speedup = (t1 - t0) / max(0.001, t3 - t2)
    req_cut = 100.0 * (1 - counter["n"] / max(1, r1.dirs_scanned))
    print("结论")
    print(f"  结果一致       : {'是' if r1.planned_count == r2.planned_count else '否'}")
    print(f"  耗时           : {(t1 - t0):.1f}s -> {(t3 - t2):.1f}s（约 {speedup:.1f}x）")
    print(f"  请求数         : {r1.dirs_scanned} -> {counter['n']}（减少 {req_cut:.0f}%）")
    print("=" * 78)

    # 清理
    cache_path.unlink(missing_ok=True)
    try:
        cache_dir.rmdir()
    except OSError:
        pass


if __name__ == "__main__":
    main()
