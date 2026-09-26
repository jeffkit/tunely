/**
 * Tunely TypeScript Client 单元测试
 *
 * 测试内容：
 * - 请求头清理逻辑（hop-by-hop headers 移除）
 * - 指数退避计算逻辑
 * - 连接状态管理（consecutiveRejectCount 重置）
 * - onDisconnect 在正常关闭时触发（Bug Fix 回归测试）
 */

import { EventEmitter } from 'events';
import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest';

// ================================================================
// Mock WebSocket — 用于 TunnelClient 集成测试
// vi.mock 会被 vitest 自动提升到文件顶部，在 import 之前执行
// ================================================================

class MockWebSocket extends EventEmitter {
  // TCP 模式的发送路径会检查 readyState === OPEN(1)
  readyState = 1;
  send = vi.fn();
  // close() 触发 close 事件，模拟连接被关闭（本端或对端）
  close = vi.fn(function (this: MockWebSocket) {
    this.emit('close');
  });
}

vi.mock('ws', () => ({
  // 静态常量 OPEN 供 client.ts 的 readyState 检查使用
  default: Object.assign(vi.fn(() => new MockWebSocket()), { OPEN: 1 }),
}));

/**
 * 提取并测试 header 清理逻辑
 * (与 client.ts handleRequest 中的逻辑一致)
 */
function cleanHeaders(headers: Record<string, string>): Record<string, string> {
  const cleanedHeaders: Record<string, string> = {};
  const skipHeaders = new Set([
    'host', 'connection', 'keep-alive', 'transfer-encoding',
    'te', 'trailer', 'upgrade', 'proxy-authorization',
    'proxy-connection',
  ]);
  for (const [key, value] of Object.entries(headers)) {
    if (!skipHeaders.has(key.toLowerCase())) {
      cleanedHeaders[key] = value;
    }
  }
  return cleanedHeaders;
}

/**
 * 提取并测试指数退避计算逻辑
 * (与 client.ts connect 中的逻辑一致)
 */
function calculateBackoffDelay(
  baseInterval: number,
  reconnectCount: number,
  consecutiveRejectCount: number,
): { minDelay: number; maxDelay: number; } {
  const backoffFactor = Math.min(reconnectCount + consecutiveRejectCount, 8);
  const maxDelayLimit = 300000; // 5 minutes max
  const delay = Math.min(baseInterval * Math.pow(2, backoffFactor - 1), maxDelayLimit);
  // Jitter range: delay * 0.8 to delay * 1.2
  return {
    minDelay: Math.round(delay * 0.8),
    maxDelay: Math.round(delay * 1.2),
  };
}

describe('Header Cleaning', () => {
  it('should remove hop-by-hop headers', () => {
    const input = {
      'Host': 'example.com',
      'Connection': 'keep-alive',
      'Content-Type': 'application/json',
      'Authorization': 'Bearer token123',
      'Transfer-Encoding': 'chunked',
      'Keep-Alive': 'timeout=5',
    };

    const cleaned = cleanHeaders(input);

    expect(cleaned).not.toHaveProperty('Host');
    expect(cleaned).not.toHaveProperty('Connection');
    expect(cleaned).not.toHaveProperty('Transfer-Encoding');
    expect(cleaned).not.toHaveProperty('Keep-Alive');
    expect(cleaned).toHaveProperty('Content-Type', 'application/json');
    expect(cleaned).toHaveProperty('Authorization', 'Bearer token123');
  });

  it('should handle case-insensitive header names', () => {
    const input = {
      'host': 'example.com',
      'HOST': 'example.com',
      'Host': 'example.com',
      'content-type': 'text/plain',
    };

    const cleaned = cleanHeaders(input);

    expect(cleaned).not.toHaveProperty('host');
    expect(cleaned).not.toHaveProperty('HOST');
    expect(cleaned).not.toHaveProperty('Host');
    expect(cleaned).toHaveProperty('content-type', 'text/plain');
  });

  it('should remove all proxy-related headers', () => {
    const input = {
      'Proxy-Authorization': 'Basic abc123',
      'Proxy-Connection': 'keep-alive',
      'X-Custom-Header': 'value',
    };

    const cleaned = cleanHeaders(input);

    expect(cleaned).not.toHaveProperty('Proxy-Authorization');
    expect(cleaned).not.toHaveProperty('Proxy-Connection');
    expect(cleaned).toHaveProperty('X-Custom-Header', 'value');
  });

  it('should return empty object for all-hop-by-hop headers', () => {
    const input = {
      'Host': 'example.com',
      'Connection': 'keep-alive',
    };

    const cleaned = cleanHeaders(input);
    expect(Object.keys(cleaned)).toHaveLength(0);
  });

  it('should pass through empty headers', () => {
    expect(cleanHeaders({})).toEqual({});
  });

  it('should preserve all non-hop-by-hop headers', () => {
    const input = {
      'Content-Type': 'application/json',
      'Accept': 'text/html',
      'X-Request-Id': 'abc-123',
      'Cache-Control': 'no-cache',
    };

    const cleaned = cleanHeaders(input);
    expect(cleaned).toEqual(input);
  });
});

describe('Exponential Backoff', () => {
  const baseInterval = 5000; // 5 seconds

  it('should start with base interval on first attempt', () => {
    const { minDelay, maxDelay } = calculateBackoffDelay(baseInterval, 1, 0);
    // Factor = min(1, 8) = 1, delay = 5000 * 2^0 = 5000
    expect(minDelay).toBe(4000); // 5000 * 0.8
    expect(maxDelay).toBe(6000); // 5000 * 1.2
  });

  it('should double delay on each reconnect', () => {
    const d1 = calculateBackoffDelay(baseInterval, 1, 0);
    const d2 = calculateBackoffDelay(baseInterval, 2, 0);
    const d3 = calculateBackoffDelay(baseInterval, 3, 0);

    // Factor 1: 5000*1 = 5000
    // Factor 2: 5000*2 = 10000
    // Factor 3: 5000*4 = 20000
    expect(d1.minDelay).toBe(4000);
    expect(d2.minDelay).toBe(8000);
    expect(d3.minDelay).toBe(16000);
  });

  it('should cap at 5 minutes (300000ms)', () => {
    // Factor 8 = max: 5000 * 2^7 = 640000, capped to 300000
    const { minDelay, maxDelay } = calculateBackoffDelay(baseInterval, 8, 0);
    expect(minDelay).toBe(240000); // 300000 * 0.8
    expect(maxDelay).toBe(360000); // 300000 * 1.2
  });

  it('should cap backoff factor at 8', () => {
    const d8 = calculateBackoffDelay(baseInterval, 8, 0);
    const d10 = calculateBackoffDelay(baseInterval, 10, 0);
    const d100 = calculateBackoffDelay(baseInterval, 100, 0);

    // All should have the same delay since factor is capped at 8
    expect(d8.minDelay).toBe(d10.minDelay);
    expect(d8.maxDelay).toBe(d100.maxDelay);
  });

  it('should accumulate reject count with reconnect count', () => {
    // reconnect=2 + reject=3 = factor 5
    const combined = calculateBackoffDelay(baseInterval, 2, 3);
    const equivalent = calculateBackoffDelay(baseInterval, 5, 0);

    expect(combined.minDelay).toBe(equivalent.minDelay);
    expect(combined.maxDelay).toBe(equivalent.maxDelay);
  });

  it('should handle zero reconnect count', () => {
    const { minDelay, maxDelay } = calculateBackoffDelay(baseInterval, 0, 0);
    // Factor = min(0, 8) = 0, delay = 5000 * 2^(-1) = 2500
    expect(minDelay).toBe(2000); // 2500 * 0.8
    expect(maxDelay).toBe(3000); // 2500 * 1.2
  });

  it('should increase delay for consecutive rejections', () => {
    const noReject = calculateBackoffDelay(baseInterval, 1, 0);
    const withReject = calculateBackoffDelay(baseInterval, 1, 2);

    // Factor 1 vs Factor 3: reject adds to backoff
    expect(withReject.minDelay).toBeGreaterThan(noReject.minDelay);
  });
});

// ================================================================
// Bug Fix 回归测试：onDisconnect 在 WebSocket 正常关闭时应当触发
//
// 【原始 Bug】
// ws.on('close') 处理函数只调用了 resolve()，没有调用 onDisconnect。
// 导致 tunnelService.status.connected 永远停留在 true，
// Desktop UI 会持续显示"已连接"和旧的 connectedAt 时间戳，
// 即使底层 WebSocket 已断开，也不会触发重连状态更新。
//
// 【修复】
// ws.on('close') 中检查 wasConnected，如果之前已建立连接则触发 onDisconnect。
// ================================================================

import { TunnelClient } from './client.js';

/** 辅助：创建真实 TunnelClient 并获取底层 MockWebSocket 实例 */
async function createConnectedClient(): Promise<{
  client: TunnelClient;
  mockWs: MockWebSocket;
  runPromise: Promise<void>;
  disconnectSpy: ReturnType<typeof vi.fn>;
  connectSpy: ReturnType<typeof vi.fn>;
}> {
  const WebSocketMock = vi.mocked((await import('ws')).default);
  let mockWs!: MockWebSocket;

  // 第一次 new WebSocket() 返回可控实例；第二次（重连）调用 close() 退出
  WebSocketMock.mockImplementationOnce(() => {
    mockWs = new MockWebSocket();
    return mockWs as any;
  }).mockImplementation(() => {
    const ws = new MockWebSocket();
    // 让第二次连接立即收到 close，使 run() 循环退出（stop 后不再重连）
    Promise.resolve().then(() => ws.emit('close'));
    return ws as any;
  });

  const client = new TunnelClient({
    serverUrl: 'ws://test-server',
    token: 'test-token',
    targetUrl: 'http://localhost:3000',
    reconnectInterval: 100,
  });

  const disconnectSpy = vi.fn();
  const connectSpy = vi.fn();
  client.on('onDisconnect', disconnectSpy);
  client.on('onConnect', connectSpy);

  const runPromise = client.run();

  // 等待 WebSocket 实例被创建并附上事件监听器
  await new Promise((r) => setImmediate(r));

  // 模拟服务端发送 AUTH_OK，触发 onConnect
  mockWs.emit(
    'message',
    Buffer.from(
      JSON.stringify({ type: 'auth_ok', domain: 'test-domain', tunnel_id: 'tid-001' })
    )
  );

  await new Promise((r) => setImmediate(r));

  return { client, mockWs, runPromise, disconnectSpy, connectSpy };
}

describe('TunnelClient - onDisconnect Bug Fix（回归测试）', () => {
  afterEach(() => {
    vi.clearAllMocks();
  });

  it('AUTH_OK 后服务端正常关闭连接，应触发 onDisconnect', async () => {
    // ✅ 验证修复：正常 close 路径（resolve）也应调用 onDisconnect
    const { client, mockWs, runPromise, disconnectSpy, connectSpy } =
      await createConnectedClient();

    // 确认已建立连接
    expect(connectSpy).toHaveBeenCalledWith('test-domain');

    // 服务端关闭 WebSocket（正常关闭，不是 error）
    mockWs.emit('close');

    // 停止客户端（阻止无限重连）
    client.stop();
    await runPromise.catch(() => {});

    // 修复后：即使是 clean close 也必须触发 onDisconnect
    expect(disconnectSpy).toHaveBeenCalledTimes(1);
  });

  it('连接建立前 close，不应触发 onDisconnect（wasConnected 守卫）', async () => {
    // ✅ 验证守卫逻辑：close 在 AUTH_OK 之前发生，不应误触发 onDisconnect
    const WebSocketMock = vi.mocked((await import('ws')).default);
    let mockWs!: MockWebSocket;

    WebSocketMock.mockImplementationOnce(() => {
      mockWs = new MockWebSocket();
      return mockWs as any;
    });

    const client = new TunnelClient({
      serverUrl: 'ws://test-server',
      token: 'test-token',
      targetUrl: 'http://localhost:3000',
    });

    const disconnectSpy = vi.fn();
    client.on('onDisconnect', disconnectSpy);

    const runPromise = client.run();
    await new Promise((r) => setImmediate(r));

    // 尚未收到 AUTH_OK，直接关闭（从未 connected）
    mockWs.emit('close');

    client.stop();
    await runPromise.catch(() => {});

    // 未建立连接，onDisconnect 不应触发
    expect(disconnectSpy).not.toHaveBeenCalled();
  });

  it('WebSocket error 触发的断开，仍应触发 onDisconnect（原有路径不受影响）', async () => {
    // ✅ 验证兼容性：error → reject → catch 路径依然工作
    const WebSocketMock = vi.mocked((await import('ws')).default);
    let mockWs!: MockWebSocket;

    WebSocketMock.mockImplementationOnce(() => {
      mockWs = new MockWebSocket();
      return mockWs as any;
    }).mockImplementation(() => {
      const ws = new MockWebSocket();
      Promise.resolve().then(() => ws.emit('close'));
      return ws as any;
    });

    const client = new TunnelClient({
      serverUrl: 'ws://test-server',
      token: 'test-token',
      targetUrl: 'http://localhost:3000',
      reconnectInterval: 100,
    });

    const disconnectSpy = vi.fn();
    client.on('onDisconnect', disconnectSpy);

    const runPromise = client.run();
    await new Promise((r) => setImmediate(r));

    // 先建立连接
    mockWs.emit(
      'message',
      Buffer.from(JSON.stringify({ type: 'auth_ok', domain: 'test-domain', tunnel_id: 'tid-001' }))
    );
    await new Promise((r) => setImmediate(r));

    // 触发 error（原有的 reject 路径）
    mockWs.emit('error', new Error('connection reset'));
    // error 后 ws 库通常也会 close，模拟之
    mockWs.emit('close');

    client.stop();
    await runPromise.catch(() => {});

    // error 路径 + close 路径各自可能都触发，至少应有 1 次
    expect(disconnectSpy).toHaveBeenCalled();
  });
});

// ================================================================
// WebSocket 构造选项：permessage-deflate
// 服务端（uvicorn/websockets）默认开启压缩，客户端需显式启用才能协商成功
// ================================================================

describe('TunnelClient - WebSocket 构造选项', () => {
  afterEach(() => {
    vi.clearAllMocks();
  });

  it('构造 WebSocket 时第二参数应包含 perMessageDeflate: true', async () => {
    const WebSocketMock = vi.mocked((await import('ws')).default);
    let mockWs!: MockWebSocket;

    WebSocketMock.mockImplementationOnce(() => {
      mockWs = new MockWebSocket();
      return mockWs as any;
    }).mockImplementation(() => {
      const ws = new MockWebSocket();
      Promise.resolve().then(() => ws.emit('close'));
      return ws as any;
    });

    const client = new TunnelClient({
      serverUrl: 'ws://test-server',
      token: 'test-token',
      targetUrl: 'http://localhost:3000',
      reconnectInterval: 100,
    });

    const runPromise = client.run();
    await new Promise((r) => setImmediate(r));
    client.stop();
    await runPromise.catch(() => {});

    // 构造函数被调用，且第二参数精确为 { perMessageDeflate: true }
    expect(WebSocketMock).toHaveBeenCalled();
    const calls = WebSocketMock.mock.calls as unknown as unknown[][];
    expect(calls.length).toBeGreaterThanOrEqual(1);
    const [url, options] = calls[0];
    expect(url).toBe('ws://test-server');
    expect(options).toEqual({ perMessageDeflate: true });
  });
});

// ================================================================
// TCP 隧道模式（tcp_connect / tcp_data / tcp_close）
//
// WebSocket 侧沿用 MockWebSocket，本地目标侧用 Node 内置 net
// 启动真实回环服务，端到端验证转发语义（对应 issue #1 验收标准）。
// ================================================================

import * as net from 'net';

/** 启动临时 TCP 服务（127.0.0.1 随机端口），返回实际端口与连接跟踪 */
async function startTcpServer(
  onData: (data: Buffer, socket: net.Socket) => void
): Promise<{
  server: net.Server;
  port: number;
  connections: net.Socket[];
  closedCount: () => number;
}> {
  const connections: net.Socket[] = [];
  let closed = 0;
  const server = net.createServer((socket) => {
    connections.push(socket);
    socket.on('close', () => closed++);
    socket.on('data', (data) => onData(data, socket));
  });
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
  const port = (server.address() as net.AddressInfo).port;
  return {
    server,
    port,
    connections,
    closedCount: () => closed,
  };
}

function closeServer(server: net.Server): Promise<void> {
  return new Promise((resolve) => server.close(() => resolve()));
}

/** 轮询直到 fn() 为真或超时，避免脆弱的固定 sleep */
async function waitUntil(fn: () => boolean, timeoutMs = 2000): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (!fn()) {
    if (Date.now() > deadline) throw new Error('waitUntil 超时');
    await new Promise((r) => setTimeout(r, 10));
  }
}

function sentMessages(mockWs: MockWebSocket): any[] {
  return (mockWs.send.mock.calls as string[][]).map((args) => JSON.parse(args[0]));
}

function sentOf(mockWs: MockWebSocket, type: string, connId?: string): any[] {
  return sentMessages(mockWs).filter(
    (m) => m.type === type && (connId === undefined || m.conn_id === connId)
  );
}

/** 创建 targetUrl 指向指定地址的已连接客户端（TCP 测试专用） */
async function createTcpTestClient(targetUrl: string): Promise<{
  client: TunnelClient;
  mockWs: MockWebSocket;
  runPromise: Promise<void>;
}> {
  const WebSocketMock = vi.mocked((await import('ws')).default);
  let mockWs!: MockWebSocket;

  WebSocketMock.mockImplementationOnce(() => {
    mockWs = new MockWebSocket();
    return mockWs as any;
  }).mockImplementation(() => {
    const ws = new MockWebSocket();
    Promise.resolve().then(() => ws.emit('close'));
    return ws as any;
  });

  const client = new TunnelClient({
    serverUrl: 'ws://test-server',
    token: 'test-token',
    targetUrl,
    reconnectInterval: 100,
  });

  const runPromise = client.run();
  await new Promise((r) => setImmediate(r));

  mockWs.emit(
    'message',
    Buffer.from(JSON.stringify({ type: 'auth_ok', domain: 'test-domain', tunnel_id: 'tid' }))
  );
  await new Promise((r) => setImmediate(r));

  return { client, mockWs, runPromise };
}

describe('TunnelClient - TCP 隧道模式', () => {
  let disposables: Array<() => Promise<void> | void>;

  beforeEach(() => {
    disposables = [];
  });

  afterEach(async () => {
    // 逆序清理：先停客户端再关服务，防止 socket 挂住事件循环
    for (const dispose of [...disposables].reverse()) {
      await dispose();
    }
    vi.clearAllMocks();
  });

  function trackClient(client: TunnelClient, runPromise: Promise<void>): void {
    disposables.push(async () => {
      client.stop();
      await runPromise.catch(() => {});
    });
  }

  it('tcp_connect 建立连接并双向转发数据，sequence 递增', async () => {
    const { server, port, connections } = await startTcpServer((data, socket) =>
      socket.write(data)
    );
    disposables.push(() => closeServer(server));

    const { client, mockWs, runPromise } = await createTcpTestClient(
      `http://127.0.0.1:${port}`
    );
    trackClient(client, runPromise);

    const connId = 'conn-echo';
    mockWs.emit(
      'message',
      Buffer.from(JSON.stringify({ type: 'tcp_connect', conn_id: connId }))
    );

    // 客户端已建立到目标的连接，且目标侧看到连接到达
    await waitUntil(() => connections.length === 1);
    await waitUntil(() => (client as any).tcpConnections.has(connId));

    // 模拟服务端下发数据。TCP 是字节流，连续写入可能被合并成一次 data 事件，
    // 因此逐段发送：等前一段回声到达后再发下一段，保证回声按段可分。
    const payload1 = Buffer.from('hello tcp');
    mockWs.emit(
      'message',
      Buffer.from(
        JSON.stringify({ type: 'tcp_data', conn_id: connId, data: payload1.toString('base64'), sequence: 0 })
      )
    );
    await waitUntil(() => sentOf(mockWs, 'tcp_data', connId).length >= 1);

    const payload2 = Buffer.from('你好，隧道');
    mockWs.emit(
      'message',
      Buffer.from(
        JSON.stringify({ type: 'tcp_data', conn_id: connId, data: payload2.toString('base64'), sequence: 1 })
      )
    );

    // 回环服务原样返回 → 客户端应发出两条 base64 tcp_data，sequence 0/1
    await waitUntil(() => sentOf(mockWs, 'tcp_data', connId).length >= 2);
    const chunks = sentOf(mockWs, 'tcp_data', connId);
    expect(Buffer.from(chunks[0].data, 'base64').equals(payload1)).toBe(true);
    expect(Buffer.from(chunks[1].data, 'base64').equals(payload2)).toBe(true);
    expect(chunks[0].sequence).toBe(0);
    expect(chunks[1].sequence).toBe(1);

    // 连接健康：不应有任何 tcp_close
    expect(sentOf(mockWs, 'tcp_close', connId)).toHaveLength(0);
  });

  it('字节级保真：HTTP 请求的 Host/Origin 等原始字节透传不改写', async () => {
    const responseBody = Buffer.from(
      'HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 17\r\n\r\nhello-from-target'
    );
    let received = Buffer.alloc(0);
    const { server, port, connections } = await startTcpServer((data, socket) => {
      received = Buffer.concat([received, data]);
      socket.write(responseBody);
    });
    disposables.push(() => closeServer(server));

    const { client, mockWs, runPromise } = await createTcpTestClient(
      `http://127.0.0.1:${port}`
    );
    trackClient(client, runPromise);

    const connId = 'conn-bytes';
    mockWs.emit(
      'message',
      Buffer.from(JSON.stringify({ type: 'tcp_connect', conn_id: connId }))
    );
    await waitUntil(() => connections.length === 1);

    // 原始 HTTP 报文（含 Host / Origin），TCP 模式必须原样到达目标
    const requestBytes = Buffer.from(
      'GET / HTTP/1.1\r\nHost: my-host.example.com\r\nOrigin: https://origin.example\r\n\r\n'
    );
    mockWs.emit(
      'message',
      Buffer.from(
        JSON.stringify({ type: 'tcp_data', conn_id: connId, data: requestBytes.toString('base64') })
      )
    );

    await waitUntil(() => received.length >= requestBytes.length);
    expect(received.equals(requestBytes)).toBe(true);

    await waitUntil(() => sentOf(mockWs, 'tcp_data', connId).length >= 1);
    const got = Buffer.from(sentOf(mockWs, 'tcp_data', connId)[0].data, 'base64');
    expect(got.equals(responseBody)).toBe(true);
  });

  it('目标侧断开连接：恰好回执一个 tcp_close（幂等），并从映射中移除', async () => {
    const { server, port, connections } = await startTcpServer((data, socket) =>
      socket.write(data)
    );
    disposables.push(() => closeServer(server));

    const { client, mockWs, runPromise } = await createTcpTestClient(
      `http://127.0.0.1:${port}`
    );
    trackClient(client, runPromise);

    const connId = 'conn-close';
    mockWs.emit(
      'message',
      Buffer.from(JSON.stringify({ type: 'tcp_connect', conn_id: connId }))
    );
    await waitUntil(() => connections.length === 1);

    // 目标侧强制断开 → 客户端 error/close 事件都会触发，但只能回执一个 tcp_close
    connections[0].destroy();

    await waitUntil(() => sentOf(mockWs, 'tcp_close', connId).length >= 1);
    // 再等一拍，确认没有重复回执
    await new Promise((r) => setTimeout(r, 50));
    expect(sentOf(mockWs, 'tcp_close', connId)).toHaveLength(1);
    expect((client as any).tcpConnections.has(connId)).toBe(false);
  });

  it('服务端发起 tcp_close：销毁本地连接且不回执', async () => {
    const { server, port, connections, closedCount } = await startTcpServer((data, socket) =>
      socket.write(data)
    );
    disposables.push(() => closeServer(server));

    const { client, mockWs, runPromise } = await createTcpTestClient(
      `http://127.0.0.1:${port}`
    );
    trackClient(client, runPromise);

    const connId = 'conn-server-close';
    mockWs.emit(
      'message',
      Buffer.from(JSON.stringify({ type: 'tcp_connect', conn_id: connId }))
    );
    await waitUntil(() => connections.length === 1);

    mockWs.emit(
      'message',
      Buffer.from(JSON.stringify({ type: 'tcp_close', conn_id: connId }))
    );

    // 本地 socket 被销毁 → 目标侧看到连接关闭
    await waitUntil(() => (client as any).tcpConnections.size === 0);
    await waitUntil(() => closedCount() === 1);
    // 不应向服务端回执 tcp_close
    await new Promise((r) => setTimeout(r, 50));
    expect(sentOf(mockWs, 'tcp_close', connId)).toHaveLength(0);
  });

  it('并发连接互不串扰', async () => {
    const { server, port, connections } = await startTcpServer((data, socket) =>
      socket.write(data)
    );
    disposables.push(() => closeServer(server));

    const { client, mockWs, runPromise } = await createTcpTestClient(
      `http://127.0.0.1:${port}`
    );
    trackClient(client, runPromise);

    const connA = 'conn-a';
    const connB = 'conn-b';
    for (const connId of [connA, connB]) {
      mockWs.emit(
        'message',
        Buffer.from(JSON.stringify({ type: 'tcp_connect', conn_id: connId }))
      );
    }
    await waitUntil(() => connections.length === 2);
    await waitUntil(
      () =>
        (client as any).tcpConnections.has(connA) && (client as any).tcpConnections.has(connB)
    );

    // 交错下发各自的数据
    const payloadA = Buffer.from('AAAA-data-for-a');
    const payloadB = Buffer.from('BBBB-data-for-b');
    mockWs.emit(
      'message',
      Buffer.from(JSON.stringify({ type: 'tcp_data', conn_id: connA, data: payloadA.toString('base64') }))
    );
    mockWs.emit(
      'message',
      Buffer.from(JSON.stringify({ type: 'tcp_data', conn_id: connB, data: payloadB.toString('base64') }))
    );

    await waitUntil(
      () =>
        sentOf(mockWs, 'tcp_data', connA).length >= 1 &&
        sentOf(mockWs, 'tcp_data', connB).length >= 1
    );

    // 各连接只收到自己的数据，echo 的 conn_id 正确回带
    expect(
      Buffer.from(sentOf(mockWs, 'tcp_data', connA)[0].data, 'base64').equals(payloadA)
    ).toBe(true);
    expect(
      Buffer.from(sentOf(mockWs, 'tcp_data', connB)[0].data, 'base64').equals(payloadB)
    ).toBe(true);
    // 两条连接的 sequence 各自从 0 开始
    expect(sentOf(mockWs, 'tcp_data', connA)[0].sequence).toBe(0);
    expect(sentOf(mockWs, 'tcp_data', connB)[0].sequence).toBe(0);
  });

  it('目标端口拒绝连接：及时回执带 error 的 tcp_close 而非挂起', async () => {
    // 先分配一个端口再释放，制造一个确定拒绝连接的端口
    const tempServer = net.createServer();
    await new Promise<void>((resolve) => tempServer.listen(0, '127.0.0.1', resolve));
    const closedPort = (tempServer.address() as net.AddressInfo).port;
    await new Promise<void>((resolve) => tempServer.close(() => resolve()));

    const { client, mockWs, runPromise } = await createTcpTestClient(
      `http://127.0.0.1:${closedPort}`
    );
    trackClient(client, runPromise);

    const connId = 'conn-refused';
    mockWs.emit(
      'message',
      Buffer.from(JSON.stringify({ type: 'tcp_connect', conn_id: connId }))
    );

    await waitUntil(() => sentOf(mockWs, 'tcp_close', connId).length >= 1);
    const closeMsg = sentOf(mockWs, 'tcp_close', connId)[0];
    expect(closeMsg.error).toContain('ECONNREFUSED');
    // 失败连接应从映射中移除，不泄漏
    await waitUntil(() => (client as any).tcpConnections.size === 0);
  });

  it('WebSocket 断开：丢弃全部本地 TCP 连接，无 socket 泄漏', async () => {
    const { server, port, connections, closedCount } = await startTcpServer((data, socket) =>
      socket.write(data)
    );
    disposables.push(() => closeServer(server));

    const { client, mockWs, runPromise } = await createTcpTestClient(
      `http://127.0.0.1:${port}`
    );
    trackClient(client, runPromise);

    for (const connId of ['conn-ws-1', 'conn-ws-2']) {
      mockWs.emit(
        'message',
        Buffer.from(JSON.stringify({ type: 'tcp_connect', conn_id: connId }))
      );
    }
    await waitUntil(() => connections.length === 2);

    // WebSocket 断开（服务端重启/网络故障）→ 本地连接全部销毁
    mockWs.emit('close');

    await waitUntil(() => closedCount() === 2);
    await waitUntil(() => (client as any).tcpConnections.size === 0);
  });
});
