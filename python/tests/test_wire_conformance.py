"""
跨实现 wire 协议 conformance 测试（Python 侧）

fixture: spec/conformance/wire.json（仓库根），由 Python 与 Rust 两侧共同消费
（TS 侧已有内联 conformance 测试，见 typescript/src/protocol.conformance.test.ts）。

每个场景：input_json 是服务端/客户端实际发出的 compact JSON
（Python 服务端 model_dump_json 风格：snake_case、可能带 timestamp 等多余字段），
经 json.loads → tunely.protocol.parse_message 解析后，必须满足 expect：
- type 与 expect.type 一致；
- expect.fields 中每个键的取值一致（子集断言，多余字段必须被容忍）。
"""

import json
from pathlib import Path
from typing import Any

import pytest

from tunely.protocol import parse_message

# fixture 相对 python/ 目录: ../../spec/conformance/wire.json
FIXTURE_PATH = Path(__file__).resolve().parents[2] / "spec" / "conformance" / "wire.json"


def load_scenarios() -> list[dict[str, Any]]:
    data = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    scenarios = data["scenarios"]
    assert isinstance(scenarios, list) and len(scenarios) >= 15, (
        f"fixture 场景数异常: {len(scenarios)}"
    )
    return scenarios


SCENARIOS = load_scenarios()
# deprecated 场景（TCP-only 收敛退役的 http 消息族，docs/MIGRATION_TCP_ONLY.md §4.3）
# 退出逐字段断言；单独保留宽松解析测试（0.11 服务端仍须容忍这些输入，F10）。
DEPRECATED_SCENARIOS = [s for s in SCENARIOS if s.get("deprecated") is True]
ACTIVE_SCENARIOS = [s for s in SCENARIOS if s.get("deprecated") is not True]


@pytest.mark.parametrize(
    "scenario", ACTIVE_SCENARIOS, ids=[s["name"] for s in ACTIVE_SCENARIOS]
)
def test_wire_conformance(scenario: dict[str, Any]):
    """同一 input_json 解析后，type 与 expect.fields 子集必须成立。"""
    name = scenario["name"]
    wire_obj = json.loads(scenario["input_json"])
    msg = parse_message(wire_obj)
    dumped = msg.model_dump(mode="json")

    expect = scenario["expect"]

    assert dumped["type"] == expect["type"], (
        f"{name}: type 不一致: {dumped['type']!r} != {expect['type']!r}"
    )

    for key, expected in expect["fields"].items():
        assert key in dumped, f"{name}: 解析结果缺少字段 {key!r}（dump={dumped}）"
        assert dumped[key] == expected, (
            f"{name}: 字段 {key!r} 不一致: {dumped[key]!r} != {expected!r}"
        )


def test_deprecated_scenarios_inventory():
    """退役场景清单与收敛设计 §4.3 对齐（防标记遗漏/漂移）。"""
    expected = {
        "request_full", "request_minimal_get", "request_stream_ok",
        "response_success", "response_upstream_error",
        "stream_start_sse", "stream_chunk_with_sequence",
        "stream_chunk_encoding_base64", "stream_end_normal", "stream_end_error",
    }
    assert {s["name"] for s in DEPRECATED_SCENARIOS} == expected
    # 活跃场景 = auth 6 + tcp 5 + udp 3 + ping/pong 2 = 16
    assert len(ACTIVE_SCENARIOS) == 16


@pytest.mark.parametrize(
    "scenario", DEPRECATED_SCENARIOS, ids=[s["name"] for s in DEPRECATED_SCENARIOS]
)
def test_deprecated_scenarios_still_parse(scenario: dict[str, Any]):
    """0.11 deprecation 窗口：旧消息输入仍必须被容忍解析（不断言字段，1.0 才收紧为丢弃）"""
    wire_obj = json.loads(scenario["input_json"])
    msg = parse_message(wire_obj)  # 只要不抛异常即容忍
    assert msg is not None
