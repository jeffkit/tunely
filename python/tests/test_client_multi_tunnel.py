"""
多隧道形态回归测试（TOML [[tunnel]]，与 rust 客户端形态二同构）

load_client_settings_from_toml 的解析/回退/互斥校验；
TunnelClientConfig.name 字段与多隧道 runner 的日志前缀约定。
"""

import pytest
from tunely.config import TunnelClientConfig, load_client_settings_from_toml


def _write(tmp_path, text: str) -> str:
    p = tmp_path / "client.toml"
    p.write_text(text, encoding="utf-8")
    return str(p)


class TestMultiTunnelToml:
    def test_multi_entries_expand_with_fallbacks(self, tmp_path):
        path = _write(
            tmp_path,
            """
server = "wss://srv/ws/tunnel"
target = "http://fallback:1"

[[tunnel]]
name = "dsh"
token = " t-dsh "
target = "http://127.0.0.1:3098"

[[tunnel]]
token = "t-p2"
""",
        )
        out = load_client_settings_from_toml(path)
        assert len(out) == 2
        assert out[0]["name"] == "dsh"
        assert out[0]["token"] == "t-dsh"
        assert out[0]["target_url"] == "http://127.0.0.1:3098"
        # 第 2 条无 name/target：name 缺省 tunnel-2，target 回落顶层
        assert out[1]["name"] == "tunnel-2"
        assert out[1]["target_url"] == "http://fallback:1"
        # 全局项共享
        assert out[1]["server_url"] == "wss://srv/ws/tunnel"
        assert out[1]["reconnect_interval"] == 5.0

    def test_flat_form_is_single_tunnel(self, tmp_path):
        path = _write(
            tmp_path,
            'server = "ws://f"\ntoken = "t-file"\ntarget = "http://f:2"\n',
        )
        out = load_client_settings_from_toml(path)
        assert len(out) == 1
        assert out[0]["name"] is None
        assert out[0]["token"] == "t-file"

        # 构造 TunnelClientConfig 可直接展开（字段名对齐）
        cfg = TunnelClientConfig(**out[0])
        assert cfg.token == "t-file"
        assert cfg.name is None

    def test_cli_overrides_win_in_single_form(self, tmp_path):
        path = _write(tmp_path, 'token = "t-file"\ntarget = "http://file"\n')
        out = load_client_settings_from_toml(
            path,
            cli_token="t-cli",
            cli_target="http://cli",
            cli_reconnect=9.0,
            cli_force=True,
        )
        assert out[0]["token"] == "t-cli"
        assert out[0]["target_url"] == "http://cli"
        assert out[0]["reconnect_interval"] == 9.0
        assert out[0]["force"] is True

    def test_multi_rejects_single_tunnel_cli_flags(self, tmp_path):
        path = _write(tmp_path, "[[tunnel]]\ntoken = 't'\n")
        with pytest.raises(ValueError, match="--token"):
            load_client_settings_from_toml(path, cli_token="t-cli")
        with pytest.raises(ValueError, match="--target"):
            load_client_settings_from_toml(path, cli_target="http://x")

    def test_multi_reports_missing_token_with_index(self, tmp_path):
        path = _write(
            tmp_path, "[[tunnel]]\ntoken = 'a'\n\n[[tunnel]]\ntarget = 'http://x'\n"
        )
        with pytest.raises(ValueError, match="第 2 条"):
            load_client_settings_from_toml(path)

    def test_flat_missing_token_raises(self, tmp_path):
        path = _write(tmp_path, "server = 'ws://f'\n")
        with pytest.raises(ValueError, match="token"):
            load_client_settings_from_toml(path)

    def test_multi_configs_construct_and_carry_name(self, tmp_path):
        path = _write(
            tmp_path,
            "[[tunnel]]\nname = 'a'\ntoken = 'ta'\n[[tunnel]]\nname = 'b'\ntoken = 'tb'\n",
        )
        out = load_client_settings_from_toml(path)
        cfgs = [TunnelClientConfig(**s) for s in out]
        assert [c.name for c in cfgs] == ["a", "b"]
        # 日志前缀约定由 TunnelClient._log_prefix 消费
        from tunely.client import TunnelClient

        prefixes = [TunnelClient(config=c)._log_prefix for c in cfgs]
        assert prefixes == ["[a] ", "[b] "]
