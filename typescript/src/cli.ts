#!/usr/bin/env node

/**
 * tunely CLI
 *
 * 命令行工具，用于启动隧道客户端
 */

import { Command } from 'commander';
import { TunnelClient } from './client.js';
import {
  resolveTunnels,
  loadConfigFile,
  MultiTunnelFileConfig,
  ResolvedTunnel,
} from './multitunnel.js';
import { CLIENT_VERSION } from './version.js';

const program = new Command();

program
  .name('tunely')
  .description('WebSocket Tunnel Client - 让内网服务可被外网访问')
  .version(CLIENT_VERSION);

const DEFAULT_SERVER = 'ws://localhost:8000/ws/tunnel';
const DEFAULT_TARGET = 'http://localhost:8080';

program
  .command('connect')
  .description('连接到隧道服务器（--config 支持单进程多隧道）')
  .option('-t, --token <token>', '隧道令牌（也可通过环境变量 TUNELY_TOKEN 提供）')
  .option('-s, --server <url>', `服务端 WebSocket URL（也可通过环境变量 TUNELY_SERVER 提供，默认 ${DEFAULT_SERVER}）`)
  .option('-T, --target <url>', `本地目标服务 URL（也可通过环境变量 TUNELY_TARGET 提供，默认 ${DEFAULT_TARGET}）`)
  .option('-r, --reconnect <seconds>', '重连间隔（秒）', '5')
  .option('-f, --force', '强制抢占已有连接', false)
  .option('-c, --config <path>', '配置文件：.toml 直接用 rust/python 同款 client.toml（[[tunnel]] 多隧道），.json 为等价形态；也可用 TUNELY_TUNNELS env 传 JSON 数组')
  .action(async (options) => {
    // 取值优先级：命令行参数 > 环境变量 > 配置文件 > 默认值
    const cliToken = options.token ?? process.env.TUNELY_TOKEN;
    const cliTarget = options.target ?? process.env.TUNELY_TARGET;

    let file: MultiTunnelFileConfig | null = null;
    if (options.config) {
      try {
        file = loadConfigFile(options.config);
      } catch (e) {
        console.error(`错误: 读取配置文件失败: ${(e as Error).message}`);
        process.exit(1);
      }
    }

    let tunnels: ResolvedTunnel[];
    try {
      tunnels = resolveTunnels({
        file,
        envTunnels: process.env.TUNELY_TUNNELS ?? null,
        cliServer: options.server ?? process.env.TUNELY_SERVER,
        cliToken,
        cliTarget,
        cliReconnectSecs: parseFloat(options.reconnect),
        cliForce: options.force,
      });
    } catch (e) {
      console.error(`错误: ${(e as Error).message}`);
      process.exit(1);
    }

    const multi = tunnels.length > 1;
    console.log('tunely - WebSocket Tunnel Client');
    console.log(`  服务端: ${tunnels[0].serverUrl}`);
    if (multi) {
      console.log(`  隧道 (${tunnels.length}):`);
      for (const t of tunnels) {
        console.log(`    - ${t.name} -> ${t.targetUrl}`);
      }
    } else {
      console.log(`  目标: ${tunnels[0].targetUrl}`);
      if (tunnels[0].force) {
        console.log('  强制模式: 将抢占已有连接');
      }
    }
    console.log();

    const clients = tunnels.map((t) => {
      const prefix = multi && t.name ? `[${t.name}] ` : '';
      const client = new TunnelClient({
        serverUrl: t.serverUrl,
        token: t.token,
        targetUrl: t.targetUrl,
        reconnectInterval: t.reconnectSecs * 1000,
        maxReconnectAttempts: t.maxReconnect,
        force: t.force,
        name: t.name ?? undefined,
        // 普通响应体内存上限（字节）：env TUNELY_MAX_RESPONSE_BYTES，0 = 不限制，默认 100MB
        maxResponseBytes: process.env.TUNELY_MAX_RESPONSE_BYTES
          ? parseInt(process.env.TUNELY_MAX_RESPONSE_BYTES, 10)
          : undefined,
        // 非 SSE 大响应流式阈值（字节）：env TUNELY_STREAM_THRESHOLD_BYTES，
        // 0 = 从不流式，默认 1MB（协议 v2 chunked_http，仅 stream_ok 请求生效）
        streamThresholdBytes: process.env.TUNELY_STREAM_THRESHOLD_BYTES
          ? parseInt(process.env.TUNELY_STREAM_THRESHOLD_BYTES, 10)
          : undefined,
      });

      client.on('onConnect', (domain) => {
        console.log(`${prefix}✓ 已连接: domain=${domain}`);
      });
      client.on('onDisconnect', () => {
        console.log(`${prefix}! 连接断开`);
      });
      client.on('onError', (error) => {
        console.error(`${prefix}✗ 错误:`, error.message);
      });
      return client;
    });

    // 处理 Ctrl+C：停全部实例再退出
    process.on('SIGINT', () => {
      console.log('\n已停止');
      for (const c of clients) c.stop();
      process.exit(0);
    });

    // 每条隧道一个独立任务：重连互不影响，全部退出才结束
    await Promise.all(clients.map((c) => c.run()));
  });

program.parse();
