//! tunely CLI — 与 typescript/src/cli.ts 的旗标保持一致
//!
//! connect 的 --token/--server/--target 可省略，取值优先级：
//! CLI 参数 > 环境变量（TUNELY_TOKEN / TUNELY_SERVER / TUNELY_TARGET）> 配置文件 > 内置默认。
//! 运行期间把连接状态写入状态文件，供 `tunely status` 查询。

use std::path::PathBuf;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use clap::Parser;
use tunely::client::{TunnelClient, TunnelClientConfig};
use tunely::config::{self, CliOverrides};
use tunely::status::{self, RunState, StatusData};

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
        /// 隧道令牌（也可用环境变量 TUNELY_TOKEN 或配置文件提供）
        #[arg(short = 't', long = "token")]
        token: Option<String>,
        /// 服务端 WebSocket URL（默认 ws://localhost:8000/ws/tunnel）
        #[arg(short = 's', long = "server")]
        server: Option<String>,
        /// 本地目标服务 URL（默认 http://localhost:8080）
        #[arg(short = 'T', long = "target")]
        target: Option<String>,
        /// 重连间隔（秒）
        #[arg(short = 'r', long = "reconnect")]
        reconnect: Option<u64>,
        /// 最大重连次数（0 = 无限）
        #[arg(long = "max-reconnect")]
        max_reconnect: Option<u32>,
        /// 请求默认超时（秒）
        #[arg(long = "request-timeout")]
        request_timeout: Option<u64>,
        /// 强制抢占已有连接
        #[arg(short = 'f', long = "force", action = clap::ArgAction::SetTrue)]
        force: Option<bool>,
        /// 配置文件路径（省略时依次尝试 ./tunely-client.toml 与 ~/.config/tunely/client.toml）
        #[arg(long = "config", value_name = "PATH")]
        config: Option<PathBuf>,
    },
    /// 查看运行中的 tunely 客户端状态
    Status,
}

/// 打印错误到 stderr 并以退出码 1 退出
fn fail(msg: &str) -> ! {
    eprintln!("tunely: {msg}");
    std::process::exit(1);
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
            config: config_path,
        } => {
            run_connect(
                CliOverrides {
                    token,
                    server,
                    target,
                    reconnect,
                    max_reconnect,
                    request_timeout,
                    force,
                },
                config_path,
            )
            .await;
        }
        Command::Status => run_status(),
    }
}

async fn run_connect(cli: CliOverrides, config_path: Option<PathBuf>) {
    // 配置文件：显式指定必须存在；默认路径存在才读取，全部缺失则静默跳过
    let (file_cfg, used_path) = match &config_path {
        Some(path) => match config::load_config_file(path) {
            Ok(Some(cfg)) => (cfg, Some(path.clone())),
            Ok(None) => fail(&format!("配置文件不存在: {}", path.display())),
            Err(e) => fail(&e),
        },
        None => match config::load_first_config(&config::default_config_paths()) {
            Ok(found) => found,
            Err(e) => fail(&e),
        },
    };
    if let Some(path) = &used_path {
        println!("已加载配置文件: {}", path.display());
    }

    let settings = match config::resolve(&cli, &file_cfg, &config::real_env) {
        Ok(s) => s,
        Err(e) => fail(&e),
    };

    println!("tunely - WebSocket Tunnel Client (Rust)");
    println!("  服务端: {}", settings.server);
    println!("  目标: {}", settings.target);
    if settings.force {
        println!("  强制模式: 将抢占已有连接");
    }
    println!();

    let client = TunnelClient::new(TunnelClientConfig {
        server_url: settings.server,
        token: settings.token,
        target_url: settings.target,
        reconnect_interval: Duration::from_secs(settings.reconnect),
        max_reconnect_attempts: settings.max_reconnect,
        request_timeout: Duration::from_secs(settings.request_timeout),
        force: settings.force,
    });

    // 状态文件：在连接成功 / 断开进入重连 / 报错 / 停止 时更新
    let tracker = Arc::new(StatusTracker::new(status::default_state_path()));
    tracker.mark_starting();

    {
        let tracker = tracker.clone();
        client.on_connect(move |domain| {
            println!("✓ 已连接: domain={domain}");
            tracker.set_connected(domain);
        });
    }
    {
        let tracker = tracker.clone();
        client.on_disconnect(move || {
            println!("! 连接断开");
            tracker.set_reconnecting();
        });
    }
    {
        let tracker = tracker.clone();
        client.on_error(move |msg| {
            eprintln!("✗ 错误: {msg}");
            tracker.set_error(msg);
        });
    }

    let client = Arc::new(client);
    let c2 = client.clone();
    tokio::spawn(async move {
        let _ = tokio::signal::ctrl_c().await;
        println!("\n收到 Ctrl-C，正在停止…");
        c2.stop();
        // 不在此处 process::exit：让 run() 自然返回后统一写最终状态
    });

    client.run().await;
    tracker.set_stopped();
}

fn run_status() {
    let Some(path) = status::default_state_path() else {
        fail("not running");
    };
    match status::read_status(&path) {
        Ok(data) => print_status(&data),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => fail("not running"),
        Err(e) => fail(&format!("读取状态文件 {} 失败: {e}", path.display())),
    }
}

fn print_status(data: &StatusData) {
    println!("tunely 状态");
    println!("  pid:             {}", data.pid);
    println!("  state:           {}", data.state.as_str());
    println!(
        "  domain:          {}",
        data.domain.as_deref().unwrap_or("-")
    );
    println!("  reconnect_count: {}", data.reconnect_count);
    println!(
        "  last_error:      {}",
        data.last_error.as_deref().unwrap_or("-")
    );
    println!("  updated_at:      {}", data.updated_at);
}

/// 状态文件写入器：聚合当前连接状态，在状态变化点落盘
struct StatusTracker {
    path: Option<PathBuf>,
    pid: u32,
    inner: Mutex<Inner>,
}

struct Inner {
    state: RunState,
    domain: Option<String>,
    reconnect_count: u32,
    last_error: Option<String>,
}

impl StatusTracker {
    fn new(path: Option<PathBuf>) -> Self {
        if path.is_none() {
            eprintln!("警告: 未设置 TUNELY_STATE_FILE 且无法确定 HOME，状态文件功能不可用");
        }
        Self {
            path,
            pid: std::process::id(),
            inner: Mutex::new(Inner {
                state: RunState::Reconnecting,
                domain: None,
                reconnect_count: 0,
                last_error: None,
            }),
        }
    }

    /// 启动即写一次（state=reconnecting），让首次连接尝试期间 `status` 也有内容
    fn mark_starting(&self) {
        self.flush();
    }

    fn set_connected(&self, domain: &str) {
        let mut g = self.inner.lock().unwrap();
        g.state = RunState::Connected;
        g.domain = Some(domain.to_string());
        g.reconnect_count = 0;
        g.last_error = None;
        drop(g);
        self.flush();
    }

    fn set_reconnecting(&self) {
        let mut g = self.inner.lock().unwrap();
        g.state = RunState::Reconnecting;
        drop(g);
        self.flush();
    }

    fn set_error(&self, msg: &str) {
        let mut g = self.inner.lock().unwrap();
        g.state = RunState::Reconnecting;
        g.last_error = Some(msg.to_string());
        g.reconnect_count += 1;
        drop(g);
        self.flush();
    }

    fn set_stopped(&self) {
        let mut g = self.inner.lock().unwrap();
        g.state = RunState::Disconnected;
        drop(g);
        self.flush();
    }

    fn flush(&self) {
        let Some(path) = &self.path else { return };
        let data = {
            let g = self.inner.lock().unwrap();
            StatusData {
                pid: self.pid,
                state: g.state,
                domain: g.domain.clone(),
                reconnect_count: g.reconnect_count,
                last_error: g.last_error.clone(),
                updated_at: status::now_rfc3339(),
            }
        };
        if let Err(e) = status::write_status(path, &data) {
            eprintln!("警告: 写状态文件失败: {e}");
        }
    }
}
