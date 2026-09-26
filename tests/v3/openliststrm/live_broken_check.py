"""在真实 OpenList 上验证失效 strm 检测逻辑。

对给定目录下的 strm 逐个反解 + API 校验，输出判定结果。
只读，不做任何删除。

用法：
    python tests/v3/openliststrm/live_broken_check.py <base_url> <token> <本地目录>
"""

import importlib.util
import sys
import types
from pathlib import Path

PLUGIN_ID = "openliststrm"
PLUGIN_DIR = Path(__file__).resolve().parents[3] / "plugins.v3" / PLUGIN_ID
PKG = f"mp_plugin_{PLUGIN_ID}"

pkg = types.ModuleType(PKG)
pkg.__path__ = [str(PLUGIN_DIR)]
sys.modules[PKG] = pkg
for sub in ("strmutil", "openlist", "treecache", "scanner", "tasks", "cleanup"):
    spec = importlib.util.spec_from_file_location(f"{PKG}.{sub}", PLUGIN_DIR / f"{sub}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"{PKG}.{sub}"] = mod
    spec.loader.exec_module(mod)

from mp_plugin_openliststrm.cleanup import collect_broken, parse_strm_url, url_host  # noqa: E402
from mp_plugin_openliststrm.openlist import OpenListClient  # noqa: E402

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
    if len(sys.argv) < 4:
        print(__doc__)
        sys.exit(2)
    base, token, local_dir = sys.argv[1].rstrip("/"), sys.argv[2], Path(sys.argv[3])

    print("=" * 78)
    print(f"失效 strm 检测验证：{base}")
    print(f"本地目录：{local_dir}")
    print("=" * 78)

    client = OpenListClient(base_url=base, token=token, transport=transport)
    client.login()

    print("\n[逐个反解]")
    for f in sorted(local_dir.rglob("*.strm")):
        content = f.read_text(encoding="utf-8", errors="replace")
        parsed = parse_strm_url(content)
        if parsed:
            host, prefix, remote = parsed          # (host, prefix, path)
            print(f"  {f.name:16} -> host={host or '(相对路径)'}  前缀={prefix or '(无)'}  路径={remote}")
        else:
            print(f"  {f.name:16} -> 无法解析（非本插件内容，会被跳过）")

    # 检测只依赖 strm 内容，远端根取任意值即可
    from mp_plugin_openliststrm.scanner import ScanRule
    rules = [ScanRule(remote_path="/Ani", local_dir=str(local_dir))]

    print("\n[检测结果]")
    plan = collect_broken(rules, client=client, remote_existing=None, verify_remote=True,
                          expected_host=url_host(base))
    print(f"  扫描 strm：{plan.total_scanned}")
    print(f"  跳过无法解析：{plan.skipped_unparsable}")
    print(f"  判定失效：{plan.count}")
    for item in plan.broken:
        print(f"    - {item.strm_path.name}")
        print(f"      远端：{item.remote_path}")
        print(f"      原因：{item.reason}")
    if plan.errors:
        print("  错误：")
        for e in plan.errors[:5]:
            print(f"    {e}")

    print("\n" + "=" * 78)
    print("预期：broken.strm 被判定失效；valid.strm 判定正常；manual.strm 被跳过")
    print("=" * 78)


if __name__ == "__main__":
    main()
