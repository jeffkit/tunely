/**
 * binary_frames 帧编解码（协议 v2 T2，docs/PROTOCOL_V2.md §1）
 *
 * WS binary 帧（仅 tcp_data 一个类型）：
 *   [0x02]     1B  协议版本标记（v2）
 *   [0x01]     1B  帧类型：0x01 = tcp_data
 *   [16B]      conn_id，UUID v4 原始字节（JSON 控制面仍是 36 字符串形式）
 *   [payload]  原始字节（无 base64、无 JSON、无 sequence——WS 有序，接收侧不依赖）
 *
 * tcp_close 保持 JSON（低频、携带 error）。解码遇版本/类型/长度不对抛 Error，
 * 调用方按畸形帧丢弃（F10 语义）。UUID 转换手写 parse/format，不引第三方依赖。
 */

/** 协议版本标记（v2） */
export const FRAME_PROTOCOL_VERSION = 0x02;
/** 帧类型：tcp_data */
export const FRAME_TYPE_TCP_DATA = 0x01;
/** 帧头长度：1B version + 1B type + 16B conn_id */
export const FRAME_HEADER_LEN = 18;

const UUID_STRING_LEN = 36;
const UUID_BYTE_LEN = 16;
/** UUID 中连字符出现的下标（8-4-4-4-12） */
const UUID_DASH_POSITIONS = [8, 13, 18, 23];

/**
 * UUID 36 字符串 → 16 原始字节。
 *
 * 仅做格式校验（长度 + 连字符位置 + hex 字符），不校验版本/变体位——
 * 与 python `uuid.UUID(...).bytes` 语义一致（任意 16 字节可往返）。
 * 畸形输入抛 Error。
 */
export function uuidToBytes(uuid: string): Uint8Array {
  const invalid = () => new Error(`invalid uuid: ${uuid}`);
  if (typeof uuid !== 'string' || uuid.length !== UUID_STRING_LEN) {
    throw invalid();
  }
  for (const pos of UUID_DASH_POSITIONS) {
    if (uuid[pos] !== '-') throw invalid();
  }
  const out = new Uint8Array(UUID_BYTE_LEN);
  let outIdx = 0;
  let pending = -1;
  for (let i = 0; i < UUID_STRING_LEN; i++) {
    if (UUID_DASH_POSITIONS.includes(i)) continue;
    const v = parseInt(uuid[i], 16);
    if (Number.isNaN(v)) throw invalid();
    if (pending < 0) {
      pending = v;
    } else {
      out[outIdx++] = (pending << 4) | v;
      pending = -1;
    }
  }
  return out;
}

/**
 * 16 原始字节 → 小写 36 字符 UUID 字符串。长度不对抛 Error。
 */
export function bytesToUuid(bytes: Uint8Array): string {
  if (bytes.length !== UUID_BYTE_LEN) {
    throw new Error(`invalid uuid bytes length: ${bytes.length}`);
  }
  let hex = '';
  for (const byte of bytes) {
    hex += byte.toString(16).padStart(2, '0');
  }
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20, 32)}`;
}

/**
 * tcp_data 二进制帧编码：0x02 0x01 + UUID 原始字节 + 原始 payload。
 * conn_id 非 UUID 字符串抛 Error。
 */
export function encodeTcpDataFrame(connId: string, payload: Uint8Array): Buffer {
  const frame = Buffer.alloc(FRAME_HEADER_LEN + payload.length);
  frame[0] = FRAME_PROTOCOL_VERSION;
  frame[1] = FRAME_TYPE_TCP_DATA;
  frame.set(uuidToBytes(connId), 2);
  if (payload.length > 0) {
    frame.set(payload, FRAME_HEADER_LEN);
  }
  return frame;
}

/**
 * tcp_data 二进制帧解码 → { connId（36 字符串）, payload（原始字节，帧内视图） }。
 *
 * 版本/类型/长度不对抛 Error（调用方按畸形帧丢弃，F10 语义）。
 * 返回的 payload 是入参帧的 subarray 视图（零拷贝）；需长期持有时自行复制。
 */
export function decodeTcpDataFrame(frame: Uint8Array): {
  connId: string;
  payload: Uint8Array;
} {
  if (frame.length < FRAME_HEADER_LEN) {
    throw new Error(`invalid frame: too short (${frame.length})`);
  }
  if (frame[0] !== FRAME_PROTOCOL_VERSION) {
    throw new Error(`invalid frame: unsupported version 0x${frame[0].toString(16)}`);
  }
  if (frame[1] !== FRAME_TYPE_TCP_DATA) {
    throw new Error(`invalid frame: unsupported type 0x${frame[1].toString(16)}`);
  }
  return {
    connId: bytesToUuid(frame.subarray(2, FRAME_HEADER_LEN)),
    payload: frame.subarray(FRAME_HEADER_LEN),
  };
}
