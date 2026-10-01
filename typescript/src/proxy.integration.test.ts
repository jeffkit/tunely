/**
 * 集成测试：客户端出站经 mock CONNECT 代理的完整隧道环。
 *
 * 拓扑：TunnelClient（真实 ws 连接 + https-proxy-agent） → mock CONNECT 代理
 * → 假隧道服务端（ws.WebSocketServer） → auth → 隧道连通（tcp_connect/tcp_data）。
 * 本文件不 mock 'ws'，全部走真实 socket。
 *
 * 覆盖：
 * - CONNECT 请求行字节级正确（authority 取自 server URL）且代理只被连一次；
 * - auth 消息经代理到达服务端，auth_ok 下发后 onConnect 触发；
 * - 经代理的 tcp_connect/tcp_data 双向转发照常工作；
 * - SOCKS 等非法代理配置在构造期 fail fast。
 */

import { describe, it, expect, afterEach } from 'vitest';
import * as http from 'http';
import * as net from 'net';
import { WebSocketServer } from 'ws';
import type { WebSocket } from 'ws';
import { TunnelClient } from './client.js';
import { encodeTcpDataFrame, decodeTcpDataFrame } from './framing.js';

const RECV_TIMEOUT = 3000;

interface MockProxy {
  port: number;
  connectCount: () => number;
  firstRequestLine: () => string;
  close: () => Promise<void>;
}

/** 最小 CONNECT 代理：记录请求行 → 连目标 → 回 200 → 双向裸搬运 */
async function startMockProxy(): Promise<MockProxy> {
  let connectCount = 0;
  let firstRequestLine = '';
  const server = http.createServer(() => {
    throw new Error('mock 代理只处理 CONNECT');
  });
  server.on('connect', (req, clientSocket, head) => {
    if (!firstRequestLine) {
      firstRequestLine = `${req.method} ${req.url} HTTP/${req.httpVersion}`;
    }
    connectCount++;
    const [host, port] = (req.url ?? '').split(':');
    const upstream = net.connect(parseInt(port, 10), host, () => {
      clientSocket.write('HTTP/1.1 200 Connection established\r\n\r\n');
      if (head && head.length) upstream.write(head);
      upstream.pipe(clientSocket);
      clientSocket.pipe(upstream);
    });
    upstream.on('error', () => clientSocket.destroy());
    clientSocket.on('error', () => upstream.destroy());
  });

  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
  const addr = server.address() as net.AddressInfo;
  return {
    port: addr.port,
    connectCount: () => connectCount,
    firstRequestLine: () => firstRequestLine,
    close: () =>
      new Promise<void>((resolve) => {
        server.close(() => resolve());
        // 已建立的隧道 socket 随进程退出，无需逐一枚举
        server.closeAllConnections?.();
      }),
  };
}

interface FakeTunnelServer {
  port: number;
  receivedAuth: () => Record<string, unknown> | null;
  sendTcpConnect: (connId: string) => void;
  sendTcp: (payload: Buffer) => void;
  nextTcpFromClient: () => Promise<{ connId: string; payload: Buffer }>;
  close: () => Promise<void>;
}

/** 假隧道服务端：校验 auth → 回 auth_ok；可下发 tcp_data / 接收回传帧 */
async function startFakeTunnelServer(): Promise<FakeTunnelServer> {
  const wss = new WebSocketServer({ host: '127.0.0.1', port: 0 });
  let receivedAuth: Record<string, unknown> | null = null;
  let clientWs: WebSocket | null = null;
  let tcpResolver: ((v: { connId: string; payload: Buffer }) => void) | null = null;

  wss.on('connection', (ws) => {
    clientWs = ws;
    ws.on('message', (data: Buffer, isBinary: boolean) => {
      if (isBinary) {
        const frame = decodeTcpDataFrame(data);
        tcpResolver?.({ connId: frame.connId, payload: Buffer.from(frame.payload) });
        return;
      }
      const msg = JSON.parse(data.toString());
      if (msg.type === 'auth') {
        receivedAuth = msg;
        ws.send(
          JSON.stringify({
            type: 'auth_ok',
            domain: 'via-proxy',
            tunnel_id: 't-1',
            capabilities: ['binary_frames'],
          })
        );
      }
    });
  });

  // ws v8：构造传 port 即立即开始监听（无 listen 方法），等 'listening' 事件
  await new Promise<void>((resolve) => wss.once('listening', resolve));
  const addr = wss.address() as net.AddressInfo;
  return {
    port: addr.port,
    receivedAuth: () => receivedAuth,
    sendTcpConnect: (connId: string) => {
      if (!clientWs) throw new Error('客户端尚未连入');
      clientWs.send(JSON.stringify({ type: 'tcp_connect', conn_id: connId }));
    },
    sendTcp: (payload: Buffer) => {
      if (!clientWs) throw new Error('客户端尚未连入');
      clientWs.send(encodeTcpDataFrame('11111111-2222-4333-8444-555555555555', payload));
    },
    nextTcpFromClient: () =>
      new Promise((resolve, reject) => {
        const timer = setTimeout(
          () => reject(new Error('等待客户端 tcp 帧超时')),
          RECV_TIMEOUT
        );
        tcpResolver = (v) => {
          clearTimeout(timer);
          resolve(v);
        };
      }),
    close: () =>
      new Promise<void>((resolve) => {
        // 先断开存量客户端（WebSocketServer 无 closeAllConnections）
        for (const client of wss.clients) client.terminate();
        wss.close(() => resolve());
      }),
  };
}

describe('出站经 mock CONNECT 代理的完整隧道环', () => {
  const disposables: Array<() => Promise<void>> = [];

  afterEach(async () => {
    for (const dispose of [...disposables].reverse()) {
      await dispose();
    }
    disposables.length = 0;
  });

  it('客户端 → 代理 → 服务端：CONNECT 建隧道，auth 成功，onConnect 触发', async () => {
    const proxy = await startMockProxy();
    disposables.push(proxy.close);
    const server = await startFakeTunnelServer();
    disposables.push(server.close);

    const client = new TunnelClient({
      serverUrl: `ws://127.0.0.1:${server.port}`,
      token: 'tok-proxy-e2e',
      targetUrl: 'http://127.0.0.1:1',
      reconnectInterval: 100,
      proxy: `http://127.0.0.1:${proxy.port}`,
    });
    const stopped = client.run().catch(() => {});
    disposables.push(async () => {
      client.stop();
      await stopped.catch(() => {});
    });

    const domain = await new Promise<string>((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error('等待 onConnect 超时')), RECV_TIMEOUT);
      client.on('onConnect', (d) => {
        clearTimeout(timer);
        resolve(d);
      });
    });

    expect(domain).toBe('via-proxy');
    // auth 经代理到达服务端（CONNECT 隧道内的 WS 握手 + 认证全通）
    const auth = server.receivedAuth();
    expect(auth).not.toBeNull();
    expect((auth as Record<string, unknown>).token).toBe('tok-proxy-e2e');
    // 代理侧：恰好一次 CONNECT，请求行 authority 取自 server URL
    expect(proxy.firstRequestLine()).toBe(
      `CONNECT 127.0.0.1:${server.port} HTTP/1.1`
    );
    expect(proxy.connectCount()).toBe(1);
  });

  it('经代理的隧道连通：tcp_connect/tcp_data 双向转发照常（binary 帧）', async () => {
    const proxy = await startMockProxy();
    disposables.push(proxy.close);
    const server = await startFakeTunnelServer();
    disposables.push(server.close);

    // 本地 echo 目标：收到的字节原样回发
    const echo = net.createServer((socket) => {
      socket.on('data', (d) => socket.write(d));
    });
    await new Promise<void>((resolve) => echo.listen(0, '127.0.0.1', resolve));
    const echoPort = (echo.address() as net.AddressInfo).port;
    disposables.push(
      () => new Promise<void>((r) => echo.close(() => r()))
    );

    const client = new TunnelClient({
      serverUrl: `ws://127.0.0.1:${server.port}`,
      token: 'tok-proxy-e2e',
      targetUrl: `http://127.0.0.1:${echoPort}`,
      reconnectInterval: 100,
      proxy: `http://127.0.0.1:${proxy.port}`,
    });
    const stopped = client.run().catch(() => {});
    disposables.push(async () => {
      client.stop();
      await stopped.catch(() => {});
    });

    await new Promise<string>((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error('等待 onConnect 超时')), RECV_TIMEOUT);
      client.on('onConnect', (d) => {
        clearTimeout(timer);
        resolve(d);
      });
    });

    // 服务端下发 tcp_connect → 客户端连本地 echo → 下发数据经回环 → 回传帧带原 payload
    server.sendTcpConnect('11111111-2222-4333-8444-555555555555');
    const payload = Buffer.from('hello-through-proxy');
    await new Promise((r) => setTimeout(r, 100)); // 等客户端建立到 echo 的连接
    server.sendTcp(payload);
    const got = await server.nextTcpFromClient();
    expect(got.connId).toBe('11111111-2222-4333-8444-555555555555');
    expect(got.payload.equals(payload)).toBe(true);
    // 代理全程只被连一次（单条 WS 连接复用同一条 CONNECT 隧道）
    expect(proxy.connectCount()).toBe(1);
  });

  it('SOCKS 等非法代理配置在构造期 fail fast', () => {
    expect(
      () =>
        new TunnelClient({
          serverUrl: 'ws://127.0.0.1:1',
          token: 't',
          targetUrl: 'http://127.0.0.1:2',
          proxy: 'socks5://127.0.0.1:1080',
        })
    ).toThrow(/SOCKS/);
  });
});
