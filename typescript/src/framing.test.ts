/**
 * binary_frames 帧编解码单测（协议 v2 T2，docs/PROTOCOL_V2.md §1）
 *
 * 覆盖：
 * - roundtrip：encode → decode 还原 conn_id（36 字符串）与 payload 原始字节；
 * - 帧布局：版本/类型标记字节、UUID 原始字节位置、空 payload；
 * - 畸形帧拒绝：过短 / 错版本 / 错类型 / 非 UUID conn_id / 错误字节数；
 * - UUID 转换：手写 parse/format 与标准格式互转（含大小写 hex、畸形输入）。
 */

import { describe, it, expect } from 'vitest';
import {
  FRAME_HEADER_LEN,
  FRAME_PROTOCOL_VERSION,
  FRAME_TYPE_TCP_DATA,
  bytesToUuid,
  decodeTcpDataFrame,
  encodeTcpDataFrame,
  uuidToBytes,
} from './framing.js';

const CONN_ID = '3f2a1b4c-5d6e-4f80-9a1b-2c3d4e5f6a7b';

describe('framing - 帧编解码 roundtrip', () => {
  it('encode → decode 还原 conn_id 与 payload 原始字节', () => {
    const payload = Buffer.from('hello tcp \x00\x01\xff binary');
    const frame = encodeTcpDataFrame(CONN_ID, payload);

    const { connId, payload: decoded } = decodeTcpDataFrame(frame);
    expect(connId).toBe(CONN_ID);
    expect(Buffer.from(decoded).equals(payload)).toBe(true);
  });

  it('帧布局：[0]=0x02 版本、[1]=0x01 类型、[2..18]=UUID 原始字节、无 sequence', () => {
    const payload = Buffer.from('abc');
    const frame = encodeTcpDataFrame(CONN_ID, payload);

    expect(frame.length).toBe(FRAME_HEADER_LEN + 3);
    expect(frame[0]).toBe(FRAME_PROTOCOL_VERSION);
    expect(frame[0]).toBe(0x02);
    expect(frame[1]).toBe(FRAME_TYPE_TCP_DATA);
    expect(frame[1]).toBe(0x01);

    const expectedUuid = Buffer.concat(
      CONN_ID.replace(/-/g, '').match(/.{2}/g)!.map((h) => Buffer.from(h, 'hex'))
    );
    expect(Buffer.from(frame.subarray(2, 18)).equals(expectedUuid)).toBe(true);
    expect(Buffer.from(frame.subarray(18)).toString()).toBe('abc');
  });

  it('空 payload：仅 18 字节帧头，解码得空 payload', () => {
    const frame = encodeTcpDataFrame(CONN_ID, new Uint8Array(0));
    expect(frame.length).toBe(FRAME_HEADER_LEN);

    const { connId, payload } = decodeTcpDataFrame(frame);
    expect(connId).toBe(CONN_ID);
    expect(payload.length).toBe(0);
  });

  it('大 payload（64KB）roundtrip 无损', () => {
    const payload = Buffer.from(
      Array.from({ length: 65536 }, (_, i) => i % 256)
    );
    const frame = encodeTcpDataFrame(CONN_ID, payload);
    const { payload: decoded } = decodeTcpDataFrame(frame);
    expect(Buffer.from(decoded).equals(payload)).toBe(true);
  });
});

describe('framing - 畸形帧拒绝（F10 语义：解码抛错，调用方丢弃）', () => {
  it('过短帧（< 18 字节）拒绝', () => {
    expect(() => decodeTcpDataFrame(new Uint8Array(0))).toThrow(/too short/);
    expect(() => decodeTcpDataFrame(new Uint8Array(17))).toThrow(/too short/);
  });

  it('错误协议版本标记拒绝', () => {
    const frame = encodeTcpDataFrame(CONN_ID, Buffer.from('x'));
    frame[0] = 0x01;
    expect(() => decodeTcpDataFrame(frame)).toThrow(/version/);
  });

  it('错误帧类型拒绝（仅 tcp_data=0x01 一个类型）', () => {
    const frame = encodeTcpDataFrame(CONN_ID, Buffer.from('x'));
    frame[1] = 0x02;
    expect(() => decodeTcpDataFrame(frame)).toThrow(/type/);
  });

  it('恰好 18 字节的纯帧头是合法空数据帧（不拒绝）', () => {
    const frame = new Uint8Array(18);
    frame[0] = 0x02;
    frame[1] = 0x01;
    const { payload } = decodeTcpDataFrame(frame);
    expect(payload.length).toBe(0);
  });
});

describe('framing - UUID 转换（36 字符串 ↔ 16 字节，手写 parse/format）', () => {
  it('uuidToBytes → bytesToUuid 往返一致', () => {
    expect(bytesToUuid(uuidToBytes(CONN_ID))).toBe(CONN_ID);
  });

  it('bytesToUuid 输出小写规范格式', () => {
    const bytes = uuidToBytes('ABCDEF01-2345-4678-9ABC-DEF012345678');
    expect(bytesToUuid(bytes)).toBe('abcdef01-2345-4678-9abc-def012345678');
  });

  it('畸形 UUID 拒绝：长度 / 连字符位置 / 非 hex 字符', () => {
    expect(() => uuidToBytes('not-a-uuid')).toThrow(/invalid uuid/);
    expect(() => uuidToBytes(CONN_ID.replace(/-/g, ''))).toThrow(/invalid uuid/); // 32 字符无连字符
    expect(() => uuidToBytes(CONN_ID.slice(1))).toThrow(/invalid uuid/);
    const badDash = CONN_ID.split('');
    badDash[13] = 'x';
    expect(() => uuidToBytes(badDash.join(''))).toThrow(/invalid uuid/);
    const badHex = CONN_ID.split('');
    badHex[5] = 'g';
    expect(() => uuidToBytes(badHex.join(''))).toThrow(/invalid uuid/);
  });

  it('bytesToUuid 长度不对拒绝', () => {
    expect(() => bytesToUuid(new Uint8Array(15))).toThrow(/length/);
    expect(() => bytesToUuid(new Uint8Array(17))).toThrow(/length/);
  });

  it('与服务端 python uuid.UUID(conn_id).bytes 布局一致（已知向量）', () => {
    // python: uuid.UUID('00112233-4455-4677-8899-aabbccddeeff').bytes
    const expected = Buffer.from('00112233445546778899aabbccddeeff', 'hex');
    expect(Buffer.from(uuidToBytes('00112233-4455-4677-8899-aabbccddeeff')).equals(expected)).toBe(
      true
    );
    expect(bytesToUuid(new Uint8Array(expected))).toBe('00112233-4455-4677-8899-aabbccddeeff');
  });
});
