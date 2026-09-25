//! tunely CLI — 与 typescript/src/cli.ts 的旗标保持一致

use std::time::Duration;

use clap::Parser;
use tunely::client::{TunnelClient, TunnelClientConfig};

#[derive(Parser)]
#[command(
    name = "tunely",
    version,
    about = "WebSocket Tunnel Client - 让内网服务可被外网访问（Rust 版）"
)]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

#[derive(clap::Subcommand)]
enum Command {
    /// 连接到隧道服务器
    Connect {
        /// 隧道令牌
        #[arg(short = 't', long = "token")]
        token: String,
        /// 服务端 WebSocket URL
        #[arg(
            short = 's',
            long = "server",
            default_value = "ws://localhost:8000/ws/tunnel"
        )]
        server: String,
        /// 本地目标服务 URL
        #[arg(short = 'T', long = "target", default_value = "http://localhost:8080")]
        target: String,
        /// 重连间隔（秒）
        #[arg(short = 'r', long = "reconnect", default_value_t = 5)]
        reconnect: u64,
        /// 最大重连次数（0 = 无限）
        #[arg(long = "max-reconnect", default_value_t = 0)]
        max_reconnect: u32,
        /// 请求默认超时（秒）
        #[arg(long = "request-timeout", default_value_t = 300)]
        request_timeout: u64,
        /// 强制抢占已有连接
        #[arg(short = 'f', long = "force", default_value_t = false)]
        force: bool,
    },
}

#[tokio::main]
async fn main() {
    let cli = Cli::parse();

    match cli.command {
        Command::Connect {
            token,
            server,
            target,
            reconnect,
            max_reconnect,
            request_timeout,
            force,
        } => {
            println!("tunely - WebSocket Tunnel Client (Rust)");
            println!("  服务端: {server}");
            println!("  目标: {target}");
            if force {
                println!("  强制模式: 将抢占已有连接");
            }
            println!();

            let client = TunnelClient::new(TunnelClientConfig {
                server_url: server,
                token,
                target_url: target,
                reconnect_interval: Duration::from_secs(reconnect),
                max_reconnect_attempts: max_reconnect,
                request_timeout: Duration::from_secs(request_timeout),
                force,
            });

            client.on_connect(|domain| println!("✓ 已连接: domain={domain}"));
            client.on_disconnect(|| println!("! 连接断开"));
            client.on_error(|msg| eprintln!("✗ 错误: {msg}"));

            let c = std::sync::Arc::new(client);
            let c2 = c.clone();
            tokio::spawn(async move {
                let _ = tokio::signal::ctrl_c().await;
                println!("\n已停止");
                c2.stop();
                std::process::exit(0);
            });

            c.run().await;
        }
    }
}
