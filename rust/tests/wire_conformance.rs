//! 跨实现 wire 协议 conformance 测试（Rust 侧）
//!
//! fixture: ../spec/conformance/wire.json（仓库根，cargo test 的 cwd 是 rust/），
//! 与 Python 侧（python/tests/test_wire_conformance.py）消费同一份数据。
//!
//! 每个场景：input_json 是服务端/客户端实际发出的 compact JSON
//! （Python 服务端 model_dump_json 风格，可能带 timestamp 等多余字段），
//! 经 `Message::parse` → `serde_json::to_value` 后，必须满足 expect：
//! - `["type"]` 与 expect.type 一致；
//! - expect.fields 中每个键与解析后 wire 值逐键相等（serde_json::Value 比较）。

use serde_json::Value;
use tunely::protocol::Message;

fn load_scenarios() -> Vec<Value> {
    let raw = std::fs::read_to_string("../spec/conformance/wire.json")
        .expect("读取 ../spec/conformance/wire.json 失败（cargo test 的 cwd 应为 rust/）");
    let fixture: Value = serde_json::from_str(&raw).expect("fixture 不是合法 JSON");
    let scenarios = fixture["scenarios"]
        .as_array()
        .expect("fixture 缺少 scenarios 数组")
        .clone();
    assert!(
        scenarios.len() >= 15,
        "fixture 场景数不足 15: {}",
        scenarios.len()
    );
    scenarios
}

#[test]
fn wire_conformance_all_scenarios() {
    let scenarios = load_scenarios();

    for scenario in &scenarios {
        let name = scenario["name"].as_str().expect("场景缺少 name");

        // deprecated 场景（TCP-only 收敛退役的 http 消息族，docs/MIGRATION_TCP_ONLY.md §4.3）
        // 退出逐字段断言；解析容忍仍保留（0.11 服务端/客户端仍须接受旧 wire 输入）
        if scenario.get("deprecated").and_then(Value::as_bool) == Some(true) {
            Message::parse(scenario["input_json"].as_str().expect("场景缺少 input_json"))
                .unwrap_or_else(|e| panic!("{name}: deprecated 场景仍必须可解析: {e}"));
            continue;
        }

        let input_json = scenario["input_json"]
            .as_str()
            .expect("场景缺少 input_json");
        let expect = &scenario["expect"];

        // 解析必须成功：多余字段（如 timestamp）应被容忍
        let msg = Message::parse(input_json)
            .unwrap_or_else(|e| panic!("{name}: 解析失败: {e}\n  input: {input_json}"));

        let actual = serde_json::to_value(&msg).expect("Message 序列化为 Value 失败");

        // type 必须一致
        let expected_type = expect["type"].as_str().expect("expect 缺少 type");
        assert_eq!(
            actual.get("type").and_then(Value::as_str),
            Some(expected_type),
            "{name}: type 不一致（actual={actual}）"
        );

        // fields 子集逐键相等
        let fields = expect["fields"].as_object().expect("expect 缺少 fields");
        for (key, expected) in fields {
            let actual_value = actual
                .get(key)
                .unwrap_or_else(|| panic!("{name}: 解析后 wire 缺少字段 {key}（actual={actual}）"));
            assert_eq!(
                actual_value, expected,
                "{name}: 字段 {key} 不一致（actual={actual_value}, expected={expected}）"
            );
        }
    }
}

// ============== 协议 v2 能力协商（capabilities）conformance ==============

/// 缺 capabilities 字段反序列化：Auth 默认空 vec（旧 wire 安全）
#[test]
fn auth_without_capabilities_defaults_empty() {
    let msg = Message::parse(r#"{"type":"auth","token":"tok","client_version":"0.1.0","force":false}"#)
        .expect("缺 capabilities 的 auth 必须可解析");
    match msg {
        Message::Auth { capabilities, .. } => assert!(capabilities.is_empty()),
        other => panic!("unexpected: {other:?}"),
    }
}

/// 缺 capabilities 字段反序列化：AuthOk 默认空 vec（0.7.3 服务端 wire 安全）
#[test]
fn auth_ok_without_capabilities_defaults_empty() {
    let msg = Message::parse(
        r#"{"type":"auth_ok","domain":"d","tunnel_id":"t1","server_version":"0.7.3"}"#,
    )
    .expect("缺 capabilities 的 auth_ok 必须可解析");
    match msg {
        Message::AuthOk { capabilities, .. } => assert!(capabilities.is_empty()),
        other => panic!("unexpected: {other:?}"),
    }
}

/// 带 capabilities 字段正常解析（服务端/对端发来的新形状）
#[test]
fn auth_with_capabilities_parses() {
    let msg = Message::parse(
        r#"{"type":"auth","token":"tok","capabilities":["binary_frames","chunked_http"]}"#,
    )
    .expect("带 capabilities 的 auth 必须可解析");
    match msg {
        Message::Auth { capabilities, .. } => {
            assert_eq!(capabilities, vec!["binary_frames", "chunked_http"])
        }
        other => panic!("unexpected: {other:?}"),
    }

    let ok = Message::parse(
        r#"{"type":"auth_ok","domain":"d","tunnel_id":"t1","capabilities":["binary_frames"]}"#,
    )
    .expect("带 capabilities 的 auth_ok 必须可解析");
    match ok {
        Message::AuthOk { capabilities, .. } => {
            assert_eq!(capabilities, vec!["binary_frames"])
        }
        other => panic!("unexpected: {other:?}"),
    }
}

/// 发送侧 wire 最小变化：空 capabilities 不出现在线上 JSON
/// （T2 起 Message::auth 恒声明 ["binary_frames"]，空 vec 场景仅限手构变体）
#[test]
fn auth_serialization_omits_empty_capabilities() {
    let wire = Message::Auth {
        token: "tok".into(),
        client_version: "0.0.0".into(),
        force: false,
        capabilities: vec![],
    }
    .to_json();
    assert!(
        !wire.contains("capabilities"),
        "空 capabilities 不应上线: {wire}"
    );

    let ok = Message::AuthOk {
        domain: "d".into(),
        tunnel_id: "t1".into(),
        server_version: Some("0.7.3".into()),
        capabilities: vec![],
    };
    assert!(
        !ok.to_json().contains("capabilities"),
        "空 capabilities 不应上线: {}",
        ok.to_json()
    );

    // 非空 capabilities 正常序列化（客户端声明用）
    let ok2 = Message::AuthOk {
        domain: "d".into(),
        tunnel_id: "t1".into(),
        server_version: None,
        capabilities: vec!["binary_frames".into()],
    };
    let wire2 = ok2.to_json();
    assert!(
        wire2.contains(r#""capabilities":["binary_frames"]"#),
        "{wire2}"
    );
}

/// T2/T4：Message::auth 恒声明已实现的 binary_frames + udp 能力（auth wire 含 capabilities）
#[test]
fn auth_declares_binary_frames() {
    let wire = Message::auth("tok", false).to_json();
    assert!(
        wire.contains(r#""capabilities":["binary_frames","udp"]"#),
        "auth 应声明 binary_frames + udp: {wire}"
    );
}

// ============== 协议 v2 T4：udp_open / udp_close conformance ==============

/// udp_open：服务端 wire（带 timestamp 冗余字段）解析 + 序列化形状
#[test]
fn udp_open_parses_and_serializes() {
    let msg = Message::parse(
        r#"{"type":"udp_open","session_id":"3f2a1b4c-5d6e-4f80-9a1b-2c3d4e5f6a7b","timestamp":"2026-09-29T02:00:06.000000+00:00"}"#,
    )
    .expect("udp_open 必须可解析（timestamp 冗余字段容忍）");
    match &msg {
        Message::UdpOpen { session_id } => {
            assert_eq!(session_id, "3f2a1b4c-5d6e-4f80-9a1b-2c3d4e5f6a7b")
        }
        other => panic!("unexpected: {other:?}"),
    }
    let wire = serde_json::to_value(&msg).unwrap();
    assert_eq!(wire["type"], "udp_open");
    assert_eq!(
        wire["session_id"],
        "3f2a1b4c-5d6e-4f80-9a1b-2c3d4e5f6a7b"
    );
}

/// udp_close：带 reason / 不带 reason 双形态 + 缺 reason 反序列化缺省安全
#[test]
fn udp_close_parses_with_and_without_reason() {
    let with_reason = Message::parse(
        r#"{"type":"udp_close","session_id":"3f2a1b4c-5d6e-4f80-9a1b-2c3d4e5f6a7b","reason":"idle timeout"}"#,
    )
    .expect("带 reason 的 udp_close 必须可解析");
    match &with_reason {
        Message::UdpClose { session_id, reason } => {
            assert_eq!(session_id, "3f2a1b4c-5d6e-4f80-9a1b-2c3d4e5f6a7b");
            assert_eq!(reason.as_deref(), Some("idle timeout"));
        }
        other => panic!("unexpected: {other:?}"),
    }

    let without_reason = Message::parse(
        r#"{"type":"udp_close","session_id":"3f2a1b4c-5d6e-4f80-9a1b-2c3d4e5f6a7b","timestamp":"now"}"#,
    )
    .expect("缺 reason 的 udp_close 必须可解析（缺省安全）");
    match &without_reason {
        Message::UdpClose { reason, .. } => assert!(reason.is_none()),
        other => panic!("unexpected: {other:?}"),
    }

    // reason=None 不上线（wire 最小变化）
    assert!(
        !without_reason.to_json().contains("reason"),
        "{}",
        without_reason.to_json()
    );
}
