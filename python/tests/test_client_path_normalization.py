"""
client.normalize_path 单元测试（@-SSRF 路径拼接防护回归）

服务端下发的 path 若不以 "/" 开头（如 "@evil/"），直接拼接会改写 URL
authority（http://127.0.0.1:3080@evil/ → 请求打到 evil 主机）。
normalize_path 保证 path 恒以 "/" 开头。
"""

from tunely.client import normalize_path


class TestNormalizePath:
    """@-SSRF 路径归一化"""

    def test_at_path_gets_prefixed(self):
        """以 @ 开头的 path 应加 "/" 前缀，落在 path 部分而非 authority"""
        assert normalize_path("@evil/x") == "/@evil/x"

    def test_absolute_path_unchanged(self):
        """已以 / 开头的 path 原样返回"""
        assert normalize_path("/ok") == "/ok"

    def test_empty_path_becomes_root(self):
        """空 path 归一化为根路径 /"""
        assert normalize_path("") == "/"
