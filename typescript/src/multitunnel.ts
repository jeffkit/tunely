/**
 * 多隧道形态（形态二，与 rust/python 客户端同构）
 *
 * 配置来源（优先级：CLI > env > 配置文件 > 默认）：
 * - 配置文件（--config）：.toml 直接读 rust/python 同款 client.toml
 *   （smol-toml，TOML 1.0）；.json 为 TS 侧等价形态。键名一致：
 *     server / token / target / proxy / [[tunnel]](name|token|target) /
 *     reconnect_secs / force
 * - TUNELY_TUNNELS 环境变量：JSON 数组（systemd/launchd env 友好）
 *
 * 代理出站例外：proxy 明确「配置 > env > 无」（与 TUNELY_* 相反）——
 * 站点级配置覆盖部署环境注入的通用代理变量（HTTPS_PROXY 等），见 resolveProxy。
 *
 * 多隧道形态下禁止单隧道来源（--token/--target 与 TUNELY_TOKEN/TUNELY_TARGET），
 * 避免两种形态静默混用。单隧道解析行为与 0.3.x 完全一致。
 */

import { readFileSync } from 'node:fs';
import { parse as parseToml } from 'smol-toml';
import { resolveProxy } from './proxy.js';

export interface MultiTunnelEntry {
  name?: string;
  token?: string;
  target?: string;
}

export interface MultiTunnelFileConfig {
  server?: string;
  token?: string;
  target?: string;
  /** 出站 HTTP CONNECT 代理（客户端 → server 的 WS）；v1 仅 http://host:port */
  proxy?: string;
  tunnel?: MultiTunnelEntry[];
  reconnect_secs?: number;
  max_reconnect?: number;
  force?: boolean;
}

/** 读配置文件：.toml 走 TOML 解析（与 rust/python 同一份 client.toml），其余按 JSON */
export function loadConfigFile(path: string): MultiTunnelFileConfig {
  const text = readFileSync(path, 'utf-8');
  if (path.toLowerCase().endsWith('.toml')) {
    return parseToml(text) as MultiTunnelFileConfig;
  }
  return JSON.parse(text) as MultiTunnelFileConfig;
}

export interface ResolvedTunnel {
  /** 多隧道会话标签；单隧道为 null（日志前缀约定与 rust/python 一致） */
  name: string | null;
  serverUrl: string;
  token: string;
  targetUrl: string;
  /** 出站代理（null = 直连）；跨隧道共享（与 server 同级全局项） */
  proxy: string | null;
  /** 秒（TS 客户端内部换算毫秒） */
  reconnectSecs: number;
  maxReconnect: number;
  force: boolean;
}

export interface ResolveInputs {
  file?: MultiTunnelFileConfig | null;
  /** TUNELY_TUNNELS env 原文（JSON 数组），未设置为 null */
  envTunnels?: string | null;
  /** 代理 env 回退查找表（通常传 process.env；缺省视为无代理 env） */
  env?: Record<string, string | undefined>;
  /** CLI --server（未传为 undefined） */
  cliServer?: string;
  /** CLI --token 或 env TUNELY_TOKEN（未传为 undefined） */
  cliToken?: string;
  /** CLI --target 或 env TUNELY_TARGET（未传为 undefined） */
  cliTarget?: string;
  /** CLI --reconnect（秒，未传为 undefined） */
  cliReconnectSecs?: number;
  /** CLI --force */
  cliForce?: boolean;
}

const DEFAULT_SERVER = 'ws://localhost:8000/ws/tunnel';
const DEFAULT_TARGET = 'http://localhost:8080';

export function resolveTunnels(input: ResolveInputs): ResolvedTunnel[] {
  const file = input.file ?? {};
  // 代理在入口处统一解析（配置 > env > 无；非法值在此抛错），
  // 单/多隧道形态共享同一结果
  const proxy = resolveProxy({
    fileProxy: file.proxy ?? null,
    env: input.env ?? undefined,
  });
  let entries: MultiTunnelEntry[] = Array.isArray(file.tunnel) ? file.tunnel : [];

  // TUNELY_TUNNELS env 优先于文件内数组（env 是部署层注入，视同 CLI 级来源）
  if (input.envTunnels && input.envTunnels.trim()) {
    if (input.cliToken || input.cliTarget) {
      throw new Error('TUNELY_TUNNELS 多隧道时不能再指定单隧道参数 --token/--target');
    }
    let parsed: unknown;
    try {
      parsed = JSON.parse(input.envTunnels);
    } catch (e) {
      throw new Error(`TUNELY_TUNNELS 不是合法 JSON 数组: ${(e as Error).message}`);
    }
    if (!Array.isArray(parsed)) {
      throw new Error('TUNELY_TUNNELS 必须是 JSON 数组');
    }
    entries = parsed as MultiTunnelEntry[];
  }

  if (entries.length === 0) {
    // 单隧道形态：CLI > env(已在 cliToken/cliTarget 合并) > file > 默认
    const token = input.cliToken ?? file.token;
    if (!token) {
      throw new Error('缺少隧道令牌：--token / TUNELY_TOKEN / 配置文件 token 字段');
    }
    return [
      {
        name: null,
        serverUrl: input.cliServer ?? file.server ?? DEFAULT_SERVER,
        token,
        targetUrl: input.cliTarget ?? file.target ?? DEFAULT_TARGET,
        proxy,
        reconnectSecs: input.cliReconnectSecs ?? file.reconnect_secs ?? 5,
        maxReconnect: file.max_reconnect ?? 0,
        force: input.cliForce ?? file.force ?? false,
      },
    ];
  }

  // 多隧道形态：单隧道 CLI/env 来源一律拒绝
  if (input.cliToken || input.cliTarget) {
    throw new Error('配置了多隧道（[[tunnel]]/TUNELY_TUNNELS）时不能再指定单隧道参数 --token/--target');
  }

  const server = input.cliServer ?? file.server ?? DEFAULT_SERVER;
  const reconnectSecs = input.cliReconnectSecs ?? file.reconnect_secs ?? 5;
  const maxReconnect = file.max_reconnect ?? 0;
  const force = input.cliForce ?? file.force ?? false;

  return entries.map((entry, i) => {
    const token = (entry.token ?? '').trim();
    if (!token) {
      throw new Error(`多隧道第 ${i + 1} 条缺少 token`);
    }
    const name = entry.name ?? `tunnel-${i + 1}`;
    const targetUrl = entry.target ?? file.target ?? DEFAULT_TARGET;
    return { name, serverUrl: server, token, targetUrl, proxy, reconnectSecs, maxReconnect, force };
  });
}
