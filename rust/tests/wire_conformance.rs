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

/// 发送侧 wire 最小变化：auth() 构造的空 capabilities 不出现在线上 JSON
#[test]
fn auth_serialization_omits_empty_capabilities() {
    let wire = Message::auth("tok", false).to_json();
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

    // 非空 capabilities 正常序列化（供 T2/T3 客户端声明用）
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
