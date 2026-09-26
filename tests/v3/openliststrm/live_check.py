"""对真实 OpenList 实例做端到端验证（只读，不写任何文件）。

用法：
    python tests/v3/openliststrm/live_check.py <base_url> <token|user:pass>

验证内容：
  1. 认证是否成功
  2. /api/fs/list 是否按契约返回 content/total
  3. 递归遍历与 strm 内容生成是否符合预期
  4. 抽样验证生成的 /d/ 直链是否可访问
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
for sub in ("strmutil", "openlist", "scanner"):
    spec = importlib.util.spec_from_file_location(f"{PKG}.{sub}", PLUGIN_DIR / f"{sub}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"{PKG}.{sub}"] = mod
    spec.loader.exec_module(mod)

from mp_plugin_openliststrm.openlist import OpenListClient, OpenListError  # noqa: E402
from mp_plugin_openliststrm.scanner import parse_rules, scan  # noqa: E402

try:
    import requests
except ImportError:
    print("需要 requests：pip install requests")
    sys.exit(1)


def make_transport(verbose=True):
    """用 requests 实现传输层。"""

    def _t(method, url, json=None, headers=None):
        try:
            if method.upper() == "GET":
                r = requests.get(url, headers=headers, params=json, timeout=30)
            else:
                r = requests.post(url, headers=headers, json=json, timeout=60)
        except Exception as err:
            if verbose:
                print(f"  [net] {method} {url} -> {err}")
            return 0, None
        # OpenList 的 API 错误也是 HTTP 200，必须看 code
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
    cred = sys.argv[2]

    token = ""
    username = password = ""
    if ":" in cred:
        username, password = cred.split(":", 1)
    else:
        token = cred

    transport = make_transport()
    client = OpenListClient(
        base_url=base, token=token,
        username=username, password=password,
        transport=transport,
    )

    print("=" * 78)
    print(f"OpenList 端到端验证：{base}")
    print("=" * 78)

    # 1. 认证
    print("\n[1] 认证")
    try:
        client.login()
        print("    ✓ 认证成功" + (f"（token 长度 {len(client._token)}）" if client._token else ""))
    except OpenListError as err:
        print(f"    ✗ 认证失败：{err}")
        sys.exit(1)

    # 2. base_path
    print("\n[2] 账号 base_path")
    try:
        bp = client.fetch_base_path()
        print(f"    ✓ base_path = {bp}")
    except OpenListError as err:
        print(f"    ⚠ 读取失败（按 / 处理）：{err}")
        bp = "/"

    # 3. 列根目录
    print("\n[3] 列出根目录")
    try:
        entries = client.list_dir("/")
        print(f"    ✓ 根目录共 {len(entries)} 个条目")
        for e in entries[:10]:
            kind = "DIR " if e.get("is_dir") else "FILE"
            print(f"        {kind} {e.get('name')}")
        if len(entries) > 10:
            print(f"        ... 其余 {len(entries) - 10} 个")
    except OpenListError as err:
        print(f"    ✗ 列目录失败：{err}")
        sys.exit(1)

    # 4. 递归扫描（选一个真实存在的目录）
    target = None
    for e in entries:
        if e.get("is_dir"):
            target = "/" + str(e.get("name"))
            break

    if not target:
        print("\n[4] 根目录下没有子目录，跳过递归测试")
        return

    print(f"\n[4] 递归扫描 {target}")
    rules, errs = parse_rules(f"{target}#/tmp/strm-out")
    if errs:
        print(f"    规则解析提示：{errs}")

    result = scan(client, rules, base_path=bp)
    print(f"    ✓ 扫描目录 {result.dirs_scanned} 个")
    print(f"      视频 {result.videos_found} 个 / 文件 {result.files_seen} 个")
    print(f"      计划生成 {result.planned_count} 个 strm")
    if result.errors:
        for msg in result.errors[:5]:
            print(f"      ⚠ {msg}")

    # 5. 抽样展示 strm 内容
    if result.planned:
        print("\n[5] strm 内容抽样")
        for local, content in result.planned[:5]:
            print(f"    {local}")
            print(f"      -> {content}")

        # 6. 验证直链可用性
        print("\n[6] 抽样验证 /d/ 直链（HEAD 请求）")
        for _, content in result.planned[:3]:
            try:
                r = requests.head(content, allow_redirects=False, timeout=20)
                print(f"    HTTP {r.status_code}  {content[:96]}")
            except Exception as err:
                print(f"    请求异常：{err}")
    else:
        print("\n[5] 未发现视频文件（该目录下可能没有视频）")

    print("\n" + "=" * 78)
    print("验证完成（全程只读，未写入任何文件）")


if __name__ == "__main__":
    main()
