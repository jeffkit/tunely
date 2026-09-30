/**
 * 多隧道形态测试（JSON 配置 / TUNELY_TUNNELS env，与 rust/python 形态二同构）
 */
import { describe, expect, it } from 'vitest';
import { resolveTunnels } from './multitunnel.js';

describe('resolveTunnels 多隧道', () => {
  it('多条目展开：name 缺省 tunnel-N、target 回落顶层、全局项共享', () => {
    const out = resolveTunnels({
      file: {
        server: 'wss://srv/ws/tunnel',
        target: 'http://fallback:1',
        tunnel: [
          { name: 'dsh', token: ' t-dsh ', target: 'http://127.0.0.1:3098' },
          { token: 't-p2' },
        ],
      },
    });
    expect(out).toHaveLength(2);
    expect(out[0].name).toBe('dsh');
    expect(out[0].token).toBe('t-dsh');
    expect(out[0].targetUrl).toBe('http://127.0.0.1:3098');
    // 第 2 条无 name/target：name 缺省 tunnel-2，target 回落顶层
    expect(out[1].name).toBe('tunnel-2');
    expect(out[1].targetUrl).toBe('http://fallback:1');
    expect(out[1].serverUrl).toBe('wss://srv/ws/tunnel');
    expect(out[1].reconnectSecs).toBe(5);
  });

  it('TUNELY_TUNNELS env（JSON 数组）等价于文件内数组', () => {
    const out = resolveTunnels({
      envTunnels: JSON.stringify([
        { name: 'a', token: 'ta' },
        { name: 'b', token: 'tb' },
      ]),
      file: { server: 'ws://f' },
    });
    expect(out).toHaveLength(2);
    expect(out.map((t) => t.name)).toEqual(['a', 'b']);
    expect(out[0].serverUrl).toBe('ws://f');
  });

  it('单隧道形态：CLI > file > 默认，name=null', () => {
    const out = resolveTunnels({
      file: { token: 't-file', target: 'http://file' },
      cliToken: 't-cli',
      cliTarget: 'http://cli',
      cliReconnectSecs: 9,
      cliForce: true,
    });
    expect(out).toHaveLength(1);
    expect(out[0]).toMatchObject({
      name: null,
      token: 't-cli',
      targetUrl: 'http://cli',
      reconnectSecs: 9,
      force: true,
    });
  });

  it('多隧道 + 单隧道 CLI 来源 → 拒绝', () => {
    const file = { tunnel: [{ token: 't' }] };
    expect(() => resolveTunnels({ file, cliToken: 'x' })).toThrow(/--token/);
    expect(() => resolveTunnels({ file, cliTarget: 'http://x' })).toThrow(/--target/);
  });

  it('TUNELY_TUNNELS + 单隧道 CLI 来源 → 拒绝', () => {
    expect(() =>
      resolveTunnels({
        envTunnels: '[{"token":"t"}]',
        cliToken: 'x',
      })
    ).toThrow(/单隧道/);
  });

  it('多隧道缺 token → 报序号', () => {
    expect(() =>
      resolveTunnels({
        file: { tunnel: [{ token: 'a' }, { target: 'http://x' }] },
      })
    ).toThrow(/第 2 条/);
  });

  it('TUNELY_TUNNELS 非法 JSON → 明确报错', () => {
    expect(() => resolveTunnels({ envTunnels: '{oops' })).toThrow(/JSON/);
  });

  it('单隧道缺 token → 报错（无 [[tunnel]] 时）', () => {
    expect(() => resolveTunnels({ file: { server: 'ws://f' } })).toThrow(/token/);
  });
});
