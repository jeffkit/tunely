"""
CLI serve 配置管道测试

覆盖集成联调发现并修复的两个问题：
- `tunely serve` 的 -D 未显式给出时回退 env WS_TUNNEL_DATABASE_URL（此前被
  CLI 硬编码默认值覆盖，导致 env 配置的数据库被静默忽略）；
- 横幅版本号取运行代码自身的 tunely.__version__（此前优先 importlib.metadata，
  安装元数据过期时谎报旧版本，如曾显示 v0.7.0）。
"""

from click.testing import CliRunner

import tunely
from tunely.cli import _DEFAULT_DATABASE_URL, _pkg_version, _resolve_database_url, serve


def test_pkg_version_matches_source_version():
    """横幅/`--version` 显示的版本与运行代码自身一致（不信过期安装元数据）"""
    assert _pkg_version() == tunely.__version__


def test_resolve_database_url_prefers_cli_over_env(monkeypatch):
    monkeypatch.setenv("WS_TUNNEL_DATABASE_URL", "sqlite+aiosqlite:////tmp/env.db")
    assert (
        _resolve_database_url("sqlite+aiosqlite:///./cli.db")
        == "sqlite+aiosqlite:///./cli.db"
    )


def test_resolve_database_url_falls_back_to_env(monkeypatch):
    monkeypatch.setenv("WS_TUNNEL_DATABASE_URL", "sqlite+aiosqlite:////tmp/env.db")
    assert _resolve_database_url(None) == "sqlite+aiosqlite:////tmp/env.db"


def test_resolve_database_url_builtin_default_without_env(monkeypatch):
    monkeypatch.delenv("WS_TUNNEL_DATABASE_URL", raising=False)
    assert _resolve_database_url(None) == _DEFAULT_DATABASE_URL


def test_serve_banner_and_run_app_receive_effective_database(monkeypatch):
    """端到端：-D 缺省时 serve 横幅与 run_app 都用 env 解析出的数据库 URL"""
    received = {}

    def fake_run_app(**kwargs):
        received.update(kwargs)

    monkeypatch.setattr("tunely.app.run_app", fake_run_app)
    monkeypatch.setenv("WS_TUNNEL_DATABASE_URL", "sqlite+aiosqlite:////tmp/env.db")

    result = CliRunner().invoke(serve, [])
    assert result.exit_code == 0, result.output
    assert received["database_url"] == "sqlite+aiosqlite:////tmp/env.db"
    # 横幅打印的是生效值（而非被覆盖的内置默认）
    assert "sqlite+aiosqlite:////tmp/env.db" in result.output
    # 版本横幅与源码版本一致
    assert f"Tunely Server v{tunely.__version__}" in result.output


def test_serve_explicit_database_flag_wins(monkeypatch):
    """显式 -D 优先于 env（行为不变回归）"""
    received = {}

    def fake_run_app(**kwargs):
        received.update(kwargs)

    monkeypatch.setattr("tunely.app.run_app", fake_run_app)
    monkeypatch.setenv("WS_TUNNEL_DATABASE_URL", "sqlite+aiosqlite:////tmp/env.db")

    result = CliRunner().invoke(serve, ["-D", "sqlite+aiosqlite:///./cli.db"])
    assert result.exit_code == 0, result.output
    assert received["database_url"] == "sqlite+aiosqlite:///./cli.db"
