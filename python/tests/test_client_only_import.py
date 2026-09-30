"""
客户端单独导入的依赖隔离测试（PEP 562 惰性导入回归）。

背景：
    tunely 包同时包含服务端 SDK 与客户端 SDK。历史上 `python/tunely/__init__.py`
    在顶部 eager import 了 `.server`（fastapi + sqlalchemy）与 `.app`
    （fastapi + pydantic-settings），导致只想嵌入客户端的用户只要
    `from tunely.client import TunnelClient`（Python 会先执行包的 __init__.py）
    就必然把整套服务端依赖拉进进程。

    现在服务端相关符号改为模块级 __getattr__ 惰性导入，本测试用 subprocess 断言
    这一点 —— 必须用子进程，因为当前 pytest 进程的 sys.modules 早已被其它
    测试污染，无法可靠地断言 "未导入"。
"""

import os
import pathlib
import subprocess
import sys

# python/ 目录（本文件位于 python/tests/ 下）
PACKAGE_ROOT = pathlib.Path(__file__).resolve().parents[1]


def run_python(code: str) -> str:
    """在干净的子进程里执行 code，返回 stdout（失败时附带 stderr 断言信息）。"""
    env = dict(os.environ)
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        str(PACKAGE_ROOT) if not existing else os.pathsep.join([str(PACKAGE_ROOT), existing])
    )

    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(PACKAGE_ROOT),
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, (
        f"subprocess 执行失败（returncode={proc.returncode}）\n"
        f"--- code ---\n{code}\n"
        f"--- stdout ---\n{proc.stdout}\n"
        f"--- stderr ---\n{proc.stderr}"
    )
    return proc.stdout.strip()


def test_client_module_import_does_not_load_server_deps():
    """import tunely.client 不应把 fastapi / sqlalchemy 拉进 sys.modules。"""
    out = run_python(
        "import sys; import tunely.client; "
        "print('fastapi' in sys.modules, 'sqlalchemy' in sys.modules)"
    )
    assert out == "False False", out


def test_package_import_does_not_load_server_deps():
    """import tunely 也不应加载 fastapi / sqlalchemy，且不导入 server/app 子模块。"""
    out = run_python(
        "import sys; import tunely; "
        "print('fastapi' in sys.modules, 'sqlalchemy' in sys.modules, "
        "'tunely.server' in sys.modules, 'tunely.app' in sys.modules)"
    )
    assert out == "False False False False", out


def test_lazy_server_symbols_still_importable():
    """from tunely import TunnelServer 仍可用（此时加载 fastapi 属预期行为）。"""
    out = run_python(
        "import sys; from tunely import TunnelServer, TunnelManager; "
        "print(TunnelServer.__name__, TunnelManager.__name__, 'fastapi' in sys.modules)"
    )
    name_server, name_manager, has_fastapi = out.split()
    assert name_server == "TunnelServer", out
    assert name_manager == "TunnelManager", out
    assert has_fastapi == "True", out


def test_lazy_app_symbols_still_importable():
    """from tunely import create_full_app / run_app 仍可用。"""
    out = run_python(
        "from tunely import create_full_app, run_app; "
        "print(create_full_app.__name__, run_app.__name__)"
    )
    assert out == "create_full_app run_app", out


def test_lazy_attribute_access_and_caching():
    """import tunely; tunely.TunnelServer 惰性可取，且首次访问后缓存进 globals()。"""
    out = run_python(
        "import sys; import tunely; "
        "before = 'fastapi' in sys.modules; "
        "cls = tunely.TunnelServer; "
        "after = 'fastapi' in sys.modules; "
        "cached = 'TunnelServer' in vars(tunely); "
        "print(before, after, cached, cls.__name__, tunely.TunnelServer is cls)"
    )
    before, after, cached, cls_name, is_same = out.split()
    assert before == "False", out
    assert after == "True", out
    assert cached == "True", out
    assert cls_name == "TunnelServer", out
    assert is_same == "True", out


def test_client_symbols_available_without_server_deps():
    """客户端与协议侧符号可直接取用，且不触发服务端依赖。"""
    out = run_python(
        "import sys; import tunely; "
        "names = (tunely.TunnelClient.__name__, tunely.TunnelClientConfig.__name__, "
        "tunely.TunnelServerConfig.__name__, tunely.TunnelRequest.__name__, "
        "tunely.MessageType.__name__, tunely.__version__); "
        "print(*names); "
        "print('fastapi' in sys.modules, 'sqlalchemy' in sys.modules)"
    )
    lines = out.splitlines()
    assert lines[0].split() == [
        "TunnelClient",
        "TunnelClientConfig",
        "TunnelServerConfig",
        "TunnelRequest",
        "MessageType",
        "0.11.1",
    ], out
    assert lines[1] == "False False", out


def test_all_and_dir_keep_backward_compatibility():
    """__all__ 不缩水；dir(tunely) 在未触发惰性导入时也包含服务端符号。"""
    out = run_python(
        "import sys; import tunely; "
        "d = dir(tunely); "
        "missing = [n for n in tunely.__all__ if n not in d]; "
        "print(len(tunely.__all__), missing, 'fastapi' in sys.modules)"
    )
    count, missing, has_fastapi = out.split()
    assert int(count) == 19, out
    assert missing == "[]", out
    assert has_fastapi == "False", out


def test_star_import_still_exports_full_api():
    """from tunely import * 仍导出 __all__ 中的全部符号。"""
    out = run_python(
        "import tunely; ns = {}; exec('from tunely import *', ns); "
        "exported = sorted(k for k in ns if not k.startswith('__')); "
        "print(sorted(n for n in tunely.__all__ if n != '__version__') == exported)"
    )
    assert out == "True", out


def test_unknown_attribute_raises_attribute_error():
    """未知属性仍抛 AttributeError（惰性导入不得吞掉它）。"""
    out = run_python(
        "import tunely\n"
        "try:\n"
        "    tunely.definitely_not_a_symbol\n"
        "except AttributeError as exc:\n"
        "    print('AttributeError', 'definitely_not_a_symbol' in str(exc))\n"
    )
    assert out == "AttributeError True", out
