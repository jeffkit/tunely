/**
 * WS-Tunnel wire 协议 conformance 测试
 *
 * 与 Python 服务端（uvicorn / websockets + pydantic）对齐线上 JSON 形状：
 * - 每个消息类型：从服务端风格的 compact JSON 字符串 parse，断言关键字段；
 * - 序列化 roundtrip：JSON.stringify → parseMessage 深度相等（键名与取值不变）；
 * - 未知类型容错：parse 不抛异常，type 字段原样保留、可与已知类型区分。
 *
 * 硬约束：线上 JSON 键名与取值必须与 Python 服务端 pydantic 模型兼容。
 */

import { describe, it, expect } from 'vitest';
import {
  MessageType,
  createAuthMessage,
  createPongMessage,
  createResponse,
  parseMessage,
  Message,
} from './protocol.js';

/** Python 服务端风格的时间戳（ISO-8601，含微秒与 +00:00 偏移） */
const TS = '2026-09-25T02:00:00.123456+00:00';

/** roundtrip：parse(stringify(msg)) 深度等于 msg，键名与取值完全不变 */
function roundtrip(message: Message): Message {
  const wire = JSON.stringify(message);
  return parseMessage(wire);
}

describe('Protocol Conformance - 认证消息', () => {
  it('auth（含 force=true）：Python 风格 JSON parse 并 roundtrip', () => {
    const wire =
      '{"type":"auth","token":"tok_dev_abc123","client_version":"0.1.0","force":true}';
    const msg = parseMessage(wire) as Extract<Message, { type: MessageType.AUTH }>;

    expect(msg.type).toBe(MessageType.AUTH);
    expect(msg.type).toBe('auth');
    expect(msg.token).toBe('tok_dev_abc123');
    expect(msg.client_version).toBe('0.1.0');
    expect(msg.force).toBe(true);
    expect(msg).toEqual(roundtrip(msg));
  });

  it('auth（force=false）：字段可缺省语义下取值为 false 并 roundtrip', () => {
    const wire =
      '{"type":"auth","token":"tok_dev_abc123","client_version":"0.1.0","force":false}';
    const msg = parseMessage(wire) as Extract<Message, { type: MessageType.AUTH }>;

    expect(msg.force).toBe(false);
    expect(msg).toEqual(roundtrip(msg));
  });

  it('auth_ok：domain / tunnel_id / server_version 并 roundtrip', () => {
    const wire =
      '{"type":"auth_ok","domain":"abc123.tunnel.example.com","tunnel_id":"tid-20260925-001","server_version":"0.3.0"}';
    const msg = parseMessage(wire) as Extract<Message, { type: MessageType.AUTH_OK }>;

    expect(msg.type).toBe(MessageType.AUTH_OK);
    expect(msg.type).toBe('auth_ok');
    expect(msg.domain).toBe('abc123.tunnel.example.com');
    expect(msg.tunnel_id).toBe('tid-20260925-001');
    expect(msg.server_version).toBe('0.3.0');
    expect(msg).toEqual(roundtrip(msg));
  });

  it('auth_error：error / code 并 roundtrip', () => {
    const wire =
      '{"type":"auth_error","error":"invalid token","code":"INVALID_TOKEN"}';
    const msg = parseMessage(wire) as Extract<Message, { type: MessageType.AUTH_ERROR }>;

    expect(msg.type).toBe(MessageType.AUTH_ERROR);
    expect(msg.type).toBe('auth_error');
    expect(msg.error).toBe('invalid token');
    expect(msg.code).toBe('INVALID_TOKEN');
    expect(msg).toEqual(roundtrip(msg));
  });
});

describe('Protocol Conformance - 请求-响应消息', () => {
  it('request：method / path / headers / body / timeout / timestamp 并 roundtrip', () => {
    const wire =
      '{"type":"request","id":"req-001","method":"POST","path":"/api/items","headers":{"content-type":"application/json","x-forwarded-host":"abc123.tunnel.example.com"},"body":"{\\"name\\":\\"widget\\"}","timeout":30,"timestamp":"' +
      TS +
      '"}';
    const msg = parseMessage(wire) as Extract<Message, { type: MessageType.REQUEST }>;

    expect(msg.type).toBe(MessageType.REQUEST);
    expect(msg.type).toBe('request');
    expect(msg.id).toBe('req-001');
    expect(msg.method).toBe('POST');
    expect(msg.path).toBe('/api/items');
    expect(msg.headers).toEqual({
      'content-type': 'application/json',
      'x-forwarded-host': 'abc123.tunnel.example.com',
    });
    expect(msg.body).toBe('{"name":"widget"}');
    expect(msg.timeout).toBe(30);
    expect(msg.timestamp).toBe(TS);
    expect(msg).toEqual(roundtrip(msg));
  });

  it('response：status / headers / body / error=null / duration_ms 并 roundtrip', () => {
    const wire =
      '{"type":"response","id":"req-001","status":200,"headers":{"content-type":"application/json"},"body":"{\\"ok\\":true}","error":null,"duration_ms":42,"timestamp":"' +
      TS +
      '"}';
    const msg = parseMessage(wire) as Extract<Message, { type: MessageType.RESPONSE }>;

    expect(msg.type).toBe(MessageType.RESPONSE);
    expect(msg.type).toBe('response');
    expect(msg.id).toBe('req-001');
    expect(msg.status).toBe(200);
    expect(msg.headers).toEqual({ 'content-type': 'application/json' });
    expect(msg.body).toBe('{"ok":true}');
    expect(msg.error).toBeNull();
    expect(msg.duration_ms).toBe(42);
    expect(msg.timestamp).toBe(TS);
    expect(msg).toEqual(roundtrip(msg));
  });
});

describe('Protocol Conformance - 流式响应消息（SSE）', () => {
  it('stream_start：status 与 text/event-stream headers 并 roundtrip', () => {
    const wire =
      '{"type":"stream_start","id":"req-sse","status":200,"headers":{"content-type":"text/event-stream","cache-control":"no-cache"},"timestamp":"' +
      TS +
      '"}';
    const msg = parseMessage(wire) as Extract<Message, { type: MessageType.STREAM_START }>;

    expect(msg.type).toBe(MessageType.STREAM_START);
    expect(msg.type).toBe('stream_start');
    expect(msg.id).toBe('req-sse');
    expect(msg.status).toBe(200);
    expect(msg.headers['content-type']).toBe('text/event-stream');
    expect(msg.timestamp).toBe(TS);
    expect(msg).toEqual(roundtrip(msg));
  });

  it('stream_chunk：data（含 SSE 换行）与 sequence 并 roundtrip', () => {
    const wire =
      '{"type":"stream_chunk","id":"req-sse","data":"data: hello\\n\\n","sequence":0,"timestamp":"' +
      TS +
      '"}';
    const msg = parseMessage(wire) as Extract<Message, { type: MessageType.STREAM_CHUNK }>;

    expect(msg.type).toBe(MessageType.STREAM_CHUNK);
    expect(msg.type).toBe('stream_chunk');
    expect(msg.id).toBe('req-sse');
    expect(msg.data).toBe('data: hello\n\n');
    expect(msg.sequence).toBe(0);
    expect(msg).toEqual(roundtrip(msg));
  });

  it('stream_end：error=null / duration_ms / total_chunks 并 roundtrip', () => {
    const wire =
      '{"type":"stream_end","id":"req-sse","error":null,"duration_ms":1500,"total_chunks":3,"timestamp":"' +
      TS +
      '"}';
    const msg = parseMessage(wire) as Extract<Message, { type: MessageType.STREAM_END }>;

    expect(msg.type).toBe(MessageType.STREAM_END);
    expect(msg.type).toBe('stream_end');
    expect(msg.id).toBe('req-sse');
    expect(msg.error).toBeNull();
    expect(msg.duration_ms).toBe(1500);
    expect(msg.total_chunks).toBe(3);
    expect(msg).toEqual(roundtrip(msg));
  });
});

describe('Protocol Conformance - TCP 模式消息', () => {
  it('tcp_connect：conn_id 并 roundtrip', () => {
    const wire =
      '{"type":"tcp_connect","conn_id":"conn-7f3a","timestamp":"' + TS + '"}';
    const msg = parseMessage(wire) as Extract<Message, { type: MessageType.TCP_CONNECT }>;

    expect(msg.type).toBe(MessageType.TCP_CONNECT);
    expect(msg.type).toBe('tcp_connect');
    expect(msg.conn_id).toBe('conn-7f3a');
    expect(msg.timestamp).toBe(TS);
    expect(msg).toEqual(roundtrip(msg));
  });

  it('tcp_data：data 为 base64（可解码回原始字节）并 roundtrip', () => {
    // base64("hello tcp")
    const wire =
      '{"type":"tcp_data","conn_id":"conn-7f3a","data":"aGVsbG8gdGNw","sequence":5,"timestamp":"' +
      TS +
      '"}';
    const msg = parseMessage(wire) as Extract<Message, { type: MessageType.TCP_DATA }>;

    expect(msg.type).toBe(MessageType.TCP_DATA);
    expect(msg.type).toBe('tcp_data');
    expect(msg.conn_id).toBe('conn-7f3a');
    // base64 语义锚定：解码必须得到原始字节
    expect(Buffer.from(msg.data, 'base64').toString('utf-8')).toBe('hello tcp');
    expect(msg.sequence).toBe(5);
    expect(msg).toEqual(roundtrip(msg));
  });

  it('tcp_close：error=null 正常关闭并 roundtrip', () => {
    const wire =
      '{"type":"tcp_close","conn_id":"conn-7f3a","error":null,"timestamp":"' + TS + '"}';
    const msg = parseMessage(wire) as Extract<Message, { type: MessageType.TCP_CLOSE }>;

    expect(msg.type).toBe(MessageType.TCP_CLOSE);
    expect(msg.type).toBe('tcp_close');
    expect(msg.conn_id).toBe('conn-7f3a');
    expect(msg.error).toBeNull();
    expect(msg).toEqual(roundtrip(msg));
  });

  it('tcp_close：error 携带失败原因（如目标拒绝）并 roundtrip', () => {
    const wire =
      '{"type":"tcp_close","conn_id":"conn-refused","error":"connect ECONNREFUSED 127.0.0.1:8080","timestamp":"' +
      TS +
      '"}';
    const msg = parseMessage(wire) as Extract<Message, { type: MessageType.TCP_CLOSE }>;

    expect(msg.error).toContain('ECONNREFUSED');
    expect(msg).toEqual(roundtrip(msg));
  });
});

describe('Protocol Conformance - 心跳消息', () => {
  it('ping：仅 type 与 timestamp 并 roundtrip', () => {
    const wire = '{"type":"ping","timestamp":"' + TS + '"}';
    const msg = parseMessage(wire) as Extract<Message, { type: MessageType.PING }>;

    expect(msg.type).toBe(MessageType.PING);
    expect(msg.type).toBe('ping');
    expect(msg.timestamp).toBe(TS);
    expect(msg).toEqual(roundtrip(msg));
  });

  it('pong：仅 type 与 timestamp 并 roundtrip', () => {
    const wire = '{"type":"pong","timestamp":"' + TS + '"}';
    const msg = parseMessage(wire) as Extract<Message, { type: MessageType.PONG }>;

    expect(msg.type).toBe(MessageType.PONG);
    expect(msg.type).toBe('pong');
    expect(msg.timestamp).toBe(TS);
    expect(msg).toEqual(roundtrip(msg));
  });
});

describe('Protocol Conformance - 客户端序列化形状（pydantic 兼容）', () => {
  it('createAuthMessage：线上键为 type/token/client_version/force，force 为布尔', () => {
    const wire = JSON.stringify(createAuthMessage('tok_local', true));
    const msg = parseMessage(wire) as Extract<Message, { type: MessageType.AUTH }>;

    expect(Object.keys(msg).sort()).toEqual([
      'client_version',
      'force',
      'token',
      'type',
    ]);
    expect(msg.type).toBe('auth');
    expect(msg.token).toBe('tok_local');
    expect(msg.force).toBe(true);
  });

  it('createPongMessage：线上键为 type/timestamp，type 为 "pong"', () => {
    const wire = JSON.stringify(createPongMessage());
    const msg = parseMessage(wire) as Extract<Message, { type: MessageType.PONG }>;

    expect(Object.keys(msg).sort()).toEqual(['timestamp', 'type']);
    expect(msg.type).toBe('pong');
    // timestamp 是合法 ISO 字符串
    expect(() => new Date(msg.timestamp!).toISOString()).not.toThrow();
  });

  it('createResponse：线上键覆盖 response 全部字段且不引入额外键', () => {
    const wire = JSON.stringify(
      createResponse('req-9', 504, null, {}, 'Target service timeout', 1234)
    );
    const msg = parseMessage(wire) as Extract<Message, { type: MessageType.RESPONSE }>;

    expect(Object.keys(msg).sort()).toEqual([
      'body',
      'duration_ms',
      'error',
      'headers',
      'id',
      'status',
      'timestamp',
      'type',
    ]);
    expect(msg.type).toBe('response');
    expect(msg.id).toBe('req-9');
    expect(msg.status).toBe(504);
    expect(msg.body).toBeNull();
    expect(msg.error).toBe('Target service timeout');
    expect(msg.duration_ms).toBe(1234);
  });
});

describe('Protocol Conformance - 未知类型容错', () => {
  it('未知类型消息 parse 不抛异常，type 字段原样保留', () => {
    const wire =
      '{"type":"mystery_v2","payload":{"x":1},"timestamp":"' + TS + '"}';

    let msg: Message;
    expect(() => {
      msg = parseMessage(wire);
    }).not.toThrow();

    const unknown = msg! as unknown as Record<string, unknown>;
    expect(unknown['type']).toBe('mystery_v2');
    expect(unknown['payload']).toEqual({ x: 1 });
    expect(unknown['timestamp']).toBe(TS);
  });

  it('未知类型可与所有已知 MessageType 区分', () => {
    const msg = parseMessage('{"type":"mystery_v2"}') as unknown as {
      type: string;
    };
    const knownTypes = new Set<string>(Object.values(MessageType));
    expect(knownTypes.has(msg.type)).toBe(false);
    // 且不落入任何已知分支的字符串值
    expect(msg.type).not.toBe('auth');
    expect(msg.type).not.toBe('request');
    expect(msg.type).not.toBe('ping');
  });

  it('MessageType 枚举值即线上 wire 字符串（snake_case）', () => {
    expect(MessageType.AUTH).toBe('auth');
    expect(MessageType.AUTH_OK).toBe('auth_ok');
    expect(MessageType.AUTH_ERROR).toBe('auth_error');
    expect(MessageType.REQUEST).toBe('request');
    expect(MessageType.RESPONSE).toBe('response');
    expect(MessageType.STREAM_START).toBe('stream_start');
    expect(MessageType.STREAM_CHUNK).toBe('stream_chunk');
    expect(MessageType.STREAM_END).toBe('stream_end');
    expect(MessageType.TCP_CONNECT).toBe('tcp_connect');
    expect(MessageType.TCP_DATA).toBe('tcp_data');
    expect(MessageType.TCP_CLOSE).toBe('tcp_close');
    expect(MessageType.PING).toBe('ping');
    expect(MessageType.PONG).toBe('pong');
  });
});
