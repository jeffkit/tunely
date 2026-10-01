//! 配置解析与合并：CLI 参数 > 环境变量（TUNELY_TOKEN / TUNELY_SERVER / TUNELY_TARGET）
//! > 配置文件（TOML）> 内置默认。
//!
//! 环境变量与 HOME 的读取以闭包/参数注入纯函数，单测不触碰进程全局状态。

use std::path::{Path, PathBuf};

use serde::Deserialize;

/// 内置默认（与 TS 客户端 CLI 默认一致）
pub const DEFAULT_SERVER: &str = "ws://localhost:8000/ws/tunnel";
pub const DEFAULT_TARGET: &str = "http://localhost:8080";

pub const ENV_TOKEN: &str = "TUNELY_TOKEN";
pub const ENV_SERVER: &str = "TUNELY_SERVER";
pub const ENV_TARGET: &str = "TUNELY_TARGET";

/// 代理出站的环境变量回退键（与 TS 客户端同序；wss:// 语义按 https 处理）
pub const PROXY_ENV_KEYS: [&str; 4] = ["HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"];

/// 配置文件（TOML）字段，全部可选；未知字段忽略
#[derive(Debug, Clone, Default, Deserialize)]
pub struct FileConfig {
    pub server: Option<String>,
    pub token: Option<String>,
    pub target: Option<String>,
    /// 出站 HTTP CONNECT 代理（客户端 → server 的 WS 经此转发）；
    /// v1 仅支持 http://host:port（SOCKS 不支持）。优先级：配置 > 代理 env > 无
    pub proxy: Option<String>,
    /// 多隧道形态（TOML `[[tunnel]]` 数组-of-tables，文档键为 `tunnel`）。
    /// 非空时进入多隧道模式：顶层 token/target 视为各条目的回退与单隧道形态互斥。
    #[serde(default, rename = "tunnel")]
    pub tunnels: Vec<TunnelEntry>,
    pub reconnect_secs: Option<u64>,
    pub max_reconnect: Option<u32>,
    pub request_timeout_secs: Option<u64>,
    pub force: Option<bool>,
    pub keepalive_interval_secs: Option<u64>,
    pub keepalive_timeout_secs: Option<u64>,
}

/// `[[tunnel]]` 单条目：token 必填（缺省在 resolve_all 按序号报错，比反序列化
/// 错误更可定位）；target 缺省回落顶层 target；name 缺省 tunnel-N
#[derive(Debug, Clone, Default, Deserialize)]
pub struct TunnelEntry {
    pub name: Option<String>,
    pub token: Option<String>,
    pub target: Option<String>,
}

/// CLI 侧可覆盖项（None = 未提供；数值旗标不再用 clap 默认值，才能区分「没传」与「传了默认值」）
#[derive(Debug, Clone, Default)]
pub struct CliOverrides {
    pub token: Option<String>,
    pub server: Option<String>,
    pub target: Option<String>,
    pub reconnect: Option<u64>,
    pub max_reconnect: Option<u32>,
    pub request_timeout: Option<u64>,
    pub force: Option<bool>,
    pub keepalive_interval: Option<u64>,
    pub keepalive_timeout: Option<u64>,
}

/// 合并后的最终配置
#[derive(Debug, Clone)]
pub struct Settings {
    pub token: String,
    pub server: String,
    pub target: String,
    /// 出站代理（None = 直连）；跨隧道共享（与 server 同级全局项）
    pub proxy: Option<String>,
    /// 多隧道模式下的会话标签（单隧道为 None）
    pub name: Option<String>,
    pub reconnect: u64,
    pub max_reconnect: u32,
    pub request_timeout: u64,
    pub force: bool,
    pub keepalive_interval: u64,
    pub keepalive_timeout: u64,
}

/// 跨隧道共享的全局配置（server / proxy 与重连/keepalive 参数）
#[derive(Debug, Clone)]
pub struct Globals {
    pub server: String,
    pub proxy: Option<String>,
    pub reconnect: u64,
    pub max_reconnect: u32,
    pub request_timeout: u64,
    pub force: bool,
    pub keepalive_interval: u64,
    pub keepalive_timeout: u64,
}

/// 代理 env 回退：HTTPS_PROXY > https_proxy > ALL_PROXY > all_proxy（首个非空生效）
fn env_proxy(env: &dyn Fn(&str) -> Option<String>) -> Option<String> {
    PROXY_ENV_KEYS
        .iter()
        .find_map(|key| env(key).filter(|v| !v.trim().is_empty()))
}

/// 代理解析：**配置 > env > 无**（注意与 TUNELY_* 的 CLI > env > file 顺序不同：
/// v1 不设 CLI 旗标，站点级配置文件覆盖部署环境注入的通用代理变量）。
/// 拿到非空值即校验（SOCKS 等不支持的形态在此直接报错，而非运行期重连循环里反复失败）。
fn resolve_proxy(
    file: &FileConfig,
    env: &dyn Fn(&str) -> Option<String>,
) -> Result<Option<String>, String> {
    match file.proxy.as_deref().map(str::trim).filter(|v| !v.is_empty()) {
        Some(v) => {
            crate::proxy::parse_proxy_url(v)?;
            Ok(Some(v.to_string()))
        }
        None => match env_proxy(env) {
            Some(v) => {
                crate::proxy::parse_proxy_url(&v)?;
                Ok(Some(v))
            }
            None => Ok(None),
        },
    }
}

/// 按 CLI > env > file 解析全局项（不含 token/target——它们按隧道区分）
fn resolve_globals(
    cli: &CliOverrides,
    file: &FileConfig,
    env: &dyn Fn(&str) -> Option<String>,
) -> Result<Globals, String> {
    Ok(Globals {
        server: cli
            .server
            .clone()
            .or_else(|| env(ENV_SERVER))
            .or_else(|| file.server.clone())
            .unwrap_or_else(|| DEFAULT_SERVER.to_string()),
        proxy: resolve_proxy(file, env)?,
        reconnect: cli.reconnect.or(file.reconnect_secs).unwrap_or(5),
        max_reconnect: cli.max_reconnect.or(file.max_reconnect).unwrap_or(0),
        request_timeout: cli
            .request_timeout
            .or(file.request_timeout_secs)
            .unwrap_or(300),
        force: cli.force.or(file.force).unwrap_or(false),
        keepalive_interval: cli
            .keepalive_interval
            .or(file.keepalive_interval_secs)
            .unwrap_or(25),
        keepalive_timeout: cli
            .keepalive_timeout
            .or(file.keepalive_timeout_secs)
            .unwrap_or(45),
    })
}

/// 按 CLI > env > file > 默认 的优先级合并出最终配置（单隧道形态）；
/// 三处都拿不到 token 时返回 Err。
pub fn resolve(
    cli: &CliOverrides,
    file: &FileConfig,
    env: &dyn Fn(&str) -> Option<String>,
) -> Result<Settings, String> {
    let token = cli
        .token
        .clone()
        .or_else(|| env(ENV_TOKEN))
        .or_else(|| file.token.clone())
        .ok_or_else(|| {
            format!("缺少 token：请通过 --token、环境变量 {ENV_TOKEN} 或配置文件提供")
        })?;
    let target = cli
        .target
        .clone()
        .or_else(|| env(ENV_TARGET))
        .or_else(|| file.target.clone())
        .unwrap_or_else(|| DEFAULT_TARGET.to_string());
    let g = resolve_globals(cli, file, env)?;
    Ok(Settings {
        token,
        server: g.server,
        target,
        proxy: g.proxy,
        name: None,
        reconnect: g.reconnect,
        max_reconnect: g.max_reconnect,
        request_timeout: g.request_timeout,
        force: g.force,
        keepalive_interval: g.keepalive_interval,
        keepalive_timeout: g.keepalive_timeout,
    })
}

/// 解析全部隧道：FileConfig 带 `[[tunnel]]` 数组时按条目展开（多隧道形态，
/// 顶层 target 作为各条目的回退）；否则退化为单隧道（resolve）。
///
/// 多隧道形态下禁止单隧道来源（--token/--target 与 TUNELY_TOKEN/TUNELY_TARGET），
/// 避免两种形态静默混用。
pub fn resolve_all(
    cli: &CliOverrides,
    file: &FileConfig,
    env: &dyn Fn(&str) -> Option<String>,
) -> Result<Vec<Settings>, String> {
    if file.tunnels.is_empty() {
        return Ok(vec![resolve(cli, file, env)?]);
    }
    if cli.token.is_some() || cli.target.is_some() {
        return Err(
            "配置了 [[tunnel]] 多隧道数组时不能再指定单隧道参数 --token/--target".into(),
        );
    }
    if env(ENV_TOKEN).is_some() || env(ENV_TARGET).is_some() {
        return Err(format!(
            "配置了 [[tunnel]] 多隧道数组时不能再设置 {ENV_TOKEN}/{ENV_TARGET}"
        ));
    }
    let g = resolve_globals(cli, file, env)?;
    let mut out = Vec::with_capacity(file.tunnels.len());
    for (i, entry) in file.tunnels.iter().enumerate() {
        let token = entry.token.as_deref().map(str::trim).unwrap_or("");
        if token.is_empty() {
            return Err(format!("[[tunnel]] 第 {} 条缺少 token", i + 1));
        }
        let target = entry
            .target
            .clone()
            .or_else(|| file.target.clone())
            .unwrap_or_else(|| DEFAULT_TARGET.to_string());
        let name = entry
            .name
            .clone()
            .unwrap_or_else(|| format!("tunnel-{}", i + 1));
        out.push(Settings {
            token: token.to_string(),
            server: g.server.clone(),
            target,
            proxy: g.proxy.clone(),
            name: Some(name),
            reconnect: g.reconnect,
            max_reconnect: g.max_reconnect,
            request_timeout: g.request_timeout,
            force: g.force,
            keepalive_interval: g.keepalive_interval,
            keepalive_timeout: g.keepalive_timeout,
        });
    }
    Ok(out)
}

/// 真实环境变量查找：读取后去除首尾空白，空白串视为未设置
pub fn real_env(key: &str) -> Option<String> {
    std::env::var(key)
        .ok()
        .map(|v| v.trim().to_string())
        .filter(|v| !v.is_empty())
}

/// 当前用户 HOME 目录（$HOME，空白视为未设置）
pub fn home_dir() -> Option<String> {
    std::env::var("HOME")
        .ok()
        .map(|h| h.trim().to_string())
        .filter(|h| !h.is_empty())
}

/// 配置文件查找路径（按序）：./tunely-client.toml、$HOME/.config/tunely/client.toml
pub fn config_paths_with(home: Option<&str>) -> Vec<PathBuf> {
    let mut paths = vec![PathBuf::from("./tunely-client.toml")];
    if let Some(h) = home.filter(|h| !h.is_empty()) {
        paths.push(
            PathBuf::from(h)
                .join(".config")
                .join("tunely")
                .join("client.toml"),
        );
    }
    paths
}

/// 默认配置文件查找路径
pub fn default_config_paths() -> Vec<PathBuf> {
    config_paths_with(home_dir().as_deref())
}

/// 读取单个配置文件：不存在 → Ok(None)；存在但读取/解析失败 → Err（带文件路径上下文）
pub fn load_config_file(path: &Path) -> Result<Option<FileConfig>, String> {
    if !path.exists() {
        return Ok(None);
    }
    let text = std::fs::read_to_string(path)
        .map_err(|e| format!("读取配置文件 {} 失败: {e}", path.display()))?;
    let cfg: FileConfig =
        toml::from_str(&text).map_err(|e| format!("解析配置文件 {} 失败: {e}", path.display()))?;
    Ok(Some(cfg))
}

/// 依次尝试给定路径，返回第一个存在的配置；都不存在返回默认空配置。
/// 任一存在的文件解析失败则整体报错。
pub fn load_first_config(paths: &[PathBuf]) -> Result<(FileConfig, Option<PathBuf>), String> {
    for path in paths {
        if let Some(cfg) = load_config_file(path)? {
            return Ok((cfg, Some(path.clone())));
        }
    }
    Ok((FileConfig::default(), None))
}

#[cfg(test)]
mod tests {
    use super::*;

    /// 构造注入式环境变量查找闭包
    fn env<'a>(pairs: &'a [(&'a str, &'a str)]) -> impl Fn(&str) -> Option<String> + 'a {
        move |key: &str| {
            pairs
                .iter()
                .find(|(name, _)| *name == key)
                .map(|(_, v)| v.to_string())
        }
    }

    #[test]
    fn resolve_cli_wins_over_env_and_file() {
        let cli = CliOverrides {
            token: Some("t-cli".into()),
            server: Some("ws://cli".into()),
            target: Some("http://cli".into()),
            ..Default::default()
        };
        let file = FileConfig {
            token: Some("t-file".into()),
            server: Some("ws://file".into()),
            target: Some("http://file".into()),
            ..Default::default()
        };
        let e = env(&[
            (ENV_TOKEN, "t-env"),
            (ENV_SERVER, "ws://env"),
            (ENV_TARGET, "http://env"),
        ]);
        let s = resolve(&cli, &file, &e).unwrap();
        assert_eq!(s.token, "t-cli");
        assert_eq!(s.server, "ws://cli");
        assert_eq!(s.target, "http://cli");
    }

    #[test]
    fn resolve_env_beats_file_and_fills_gaps() {
        let cli = CliOverrides {
            token: Some("t-cli".into()),
            ..Default::default()
        };
        let file = FileConfig {
            token: Some("t-file".into()),
            server: Some("ws://file".into()),
            reconnect_secs: Some(9),
            ..Default::default()
        };
        let e = env(&[(ENV_TOKEN, "t-env"), (ENV_SERVER, "ws://env")]);
        let s = resolve(&cli, &file, &e).unwrap();
        assert_eq!(s.token, "t-cli");
        assert_eq!(s.server, "ws://env");
        assert_eq!(s.target, DEFAULT_TARGET);
        assert_eq!(s.reconnect, 9); // 数值旗标未传时回落配置文件
    }

    #[test]
    fn resolve_env_used_when_cli_absent() {
        let file = FileConfig {
            token: Some("t-file".into()),
            server: Some("ws://file".into()),
            ..Default::default()
        };
        let e = env(&[(ENV_TOKEN, "t-env")]);
        let s = resolve(&CliOverrides::default(), &file, &e).unwrap();
        assert_eq!(s.token, "t-env");
        assert_eq!(s.server, "ws://file"); // env 未提供 server → 配置文件
    }

    #[test]
    fn resolve_file_used_when_no_cli_or_env() {
        let file = FileConfig {
            token: Some("t-file".into()),
            target: Some("http://file".into()),
            reconnect_secs: Some(9),
            max_reconnect: Some(3),
            request_timeout_secs: Some(60),
            force: Some(true),
            ..Default::default()
        };
        let s = resolve(&CliOverrides::default(), &file, &env(&[])).unwrap();
        assert_eq!(s.token, "t-file");
        assert_eq!(s.target, "http://file");
        assert_eq!(s.reconnect, 9);
        assert_eq!(s.max_reconnect, 3);
        assert_eq!(s.request_timeout, 60);
        assert!(s.force);
    }

    #[test]
    fn resolve_builtin_defaults_when_nothing_set() {
        let e = env(&[(ENV_TOKEN, "t-env")]); // 仅补 token，其余走内置默认
        let s = resolve(&CliOverrides::default(), &FileConfig::default(), &e).unwrap();
        assert_eq!(s.token, "t-env");
        assert_eq!(s.server, DEFAULT_SERVER);
        assert_eq!(s.target, DEFAULT_TARGET);
        assert_eq!(s.reconnect, 5);
        assert_eq!(s.max_reconnect, 0);
        assert_eq!(s.request_timeout, 300);
        assert!(!s.force);
    }

    #[test]
    fn resolve_missing_token_is_error() {
        let err = resolve(&CliOverrides::default(), &FileConfig::default(), &env(&[])).unwrap_err();
        assert!(err.contains("token"), "{err}");
        // CLI 缺、env 有 → 不报错
        let e = env(&[(ENV_TOKEN, "t-env")]);
        assert!(resolve(&CliOverrides::default(), &FileConfig::default(), &e).is_ok());
    }

    #[test]
    fn toml_parses_all_fields() {
        let text = r#"
server = "wss://s.example/ws/tunnel"
token = "tok-1"
target = "http://127.0.0.1:3080"
proxy = "http://127.0.0.1:7890"
reconnect_secs = 7
max_reconnect = 2
request_timeout_secs = 30
force = true
"#;
        let c: FileConfig = toml::from_str(text).unwrap();
        assert_eq!(c.server.as_deref(), Some("wss://s.example/ws/tunnel"));
        assert_eq!(c.token.as_deref(), Some("tok-1"));
        assert_eq!(c.target.as_deref(), Some("http://127.0.0.1:3080"));
        assert_eq!(c.proxy.as_deref(), Some("http://127.0.0.1:7890"));
        assert_eq!(c.reconnect_secs, Some(7));
        assert_eq!(c.max_reconnect, Some(2));
        assert_eq!(c.request_timeout_secs, Some(30));
        assert_eq!(c.force, Some(true));
    }

    #[test]
    fn toml_missing_fields_are_none() {
        let c: FileConfig = toml::from_str("token = \"x\"\n").unwrap();
        assert_eq!(c.token.as_deref(), Some("x"));
        assert_eq!(c.server, None);
        assert_eq!(c.target, None);
        assert_eq!(c.proxy, None);
        assert_eq!(c.reconnect_secs, None);
        assert_eq!(c.max_reconnect, None);
        assert_eq!(c.request_timeout_secs, None);
        assert_eq!(c.force, None);
    }

    #[test]
    fn toml_malformed_is_rejected() {
        assert!(toml::from_str::<FileConfig>("token = ").is_err());
        assert!(toml::from_str::<FileConfig>("???").is_err());
        assert!(toml::from_str::<FileConfig>("reconnect_secs = \"abc\"").is_err());
    }

    #[test]
    fn load_config_file_missing_present_and_malformed() {
        let dir = tempfile::tempdir().unwrap();
        assert!(load_config_file(&dir.path().join("nope.toml"))
            .unwrap()
            .is_none());

        let ok = dir.path().join("client.toml");
        std::fs::write(&ok, "token = \"from-file\"\n").unwrap();
        let c = load_config_file(&ok).unwrap().unwrap();
        assert_eq!(c.token.as_deref(), Some("from-file"));

        let bad = dir.path().join("bad.toml");
        std::fs::write(&bad, "???").unwrap();
        let err = load_config_file(&bad).unwrap_err();
        assert!(err.contains("bad.toml"), "{err}");
    }

    #[test]
    fn load_first_config_takes_first_existing() {
        let dir = tempfile::tempdir().unwrap();
        let a = dir.path().join("a.toml");
        let b = dir.path().join("b.toml");
        std::fs::write(&b, "token = \"b\"\n").unwrap();
        let (cfg, used) = load_first_config(&[a, b.clone()]).unwrap();
        assert_eq!(cfg.token.as_deref(), Some("b"));
        assert_eq!(used.as_deref(), Some(b.as_path()));

        let (cfg2, used2) = load_first_config(&[dir.path().join("none.toml")]).unwrap();
        assert_eq!(cfg2.token, None);
        assert!(used2.is_none());
    }

    #[test]
    fn config_paths_order_and_home_fallback() {
        let paths = config_paths_with(Some("/home/u"));
        assert_eq!(paths.len(), 2);
        assert_eq!(paths[0], PathBuf::from("./tunely-client.toml"));
        assert_eq!(
            paths[1],
            PathBuf::from("/home/u/.config/tunely/client.toml")
        );
        assert_eq!(config_paths_with(None).len(), 1);
        assert_eq!(config_paths_with(Some("")).len(), 1);
    }

    // ============== 多隧道形态（[[tunnel]]） ==============

    #[test]
    fn toml_tunnel_array_parses_and_flat_form_still_works() {
        let multi: FileConfig = toml::from_str(
            r#"
server = "wss://srv/ws/tunnel"

[[tunnel]]
name = "dsh"
token = "t-dsh"
target = "http://127.0.0.1:3098"

[[tunnel]]
token = "t-p2"
target = "http://127.0.0.1:8123"
"#,
        )
        .unwrap();
        assert_eq!(multi.tunnels.len(), 2);
        assert_eq!(multi.tunnels[0].name.as_deref(), Some("dsh"));
        assert_eq!(multi.tunnels[0].token.as_deref(), Some("t-dsh"));
        assert_eq!(multi.tunnels[1].name, None);
        assert_eq!(multi.tunnels[1].token.as_deref(), Some("t-p2"));

        // 单隧道平铺形态不受影响
        let flat: FileConfig = toml::from_str("token = \"x\"\n").unwrap();
        assert!(flat.tunnels.is_empty());
        assert_eq!(flat.token.as_deref(), Some("x"));
    }

    #[test]
    fn resolve_all_expands_tunnels_with_fallbacks() {
        let file: FileConfig = toml::from_str(
            r#"
server = "wss://srv/ws/tunnel"
target = "http://fallback:1"

[[tunnel]]
name = "dsh"
token = " t-dsh "
target = "http://127.0.0.1:3098"

[[tunnel]]
token = "t-p2"
"#,
        )
        .unwrap();
        let list = resolve_all(&CliOverrides::default(), &file, &env(&[])).unwrap();
        assert_eq!(list.len(), 2);
        assert_eq!(list[0].name.as_deref(), Some("dsh"));
        assert_eq!(list[0].target, "http://127.0.0.1:3098");
        // 第 2 条无 name/target：name 缺省 tunnel-2，target 回落顶层
        assert_eq!(list[1].name.as_deref(), Some("tunnel-2"));
        assert_eq!(list[1].target, "http://fallback:1");
        // token 去首尾空白；全局项共享
        assert_eq!(list[0].token, "t-dsh");
        assert_eq!(list[1].server, "wss://srv/ws/tunnel");
        assert_eq!(list[1].reconnect, 5);
    }

    #[test]
    fn resolve_all_rejects_single_tunnel_sources_in_multi_mode() {
        let file: FileConfig = toml::from_str("[[tunnel]]\ntoken = \"t\"\n").unwrap();
        // CLI 单隧道参数冲突
        let cli = CliOverrides {
            token: Some("t-cli".into()),
            ..Default::default()
        };
        let err = resolve_all(&cli, &file, &env(&[])).unwrap_err();
        assert!(err.contains("--token"), "{err}");
        // env 单隧道变量冲突
        let err = resolve_all(
            &CliOverrides::default(),
            &file,
            &env(&[(ENV_TARGET, "http://x")]),
        )
        .unwrap_err();
        assert!(err.contains(ENV_TARGET), "{err}");
    }

    #[test]
    fn resolve_all_reports_missing_token_with_index() {
        let file: FileConfig = toml::from_str(
            "[[tunnel]]\ntoken = \"a\"\n\n[[tunnel]]\ntarget = \"http://x\"\n",
        )
        .unwrap();
        let err = resolve_all(&CliOverrides::default(), &file, &env(&[])).unwrap_err();
        assert!(err.contains("第 2 条"), "{err}");
    }

    #[test]
    fn resolve_all_delegates_to_resolve_when_no_array() {
        let file = FileConfig {
            token: Some("t-file".into()),
            ..Default::default()
        };
        let list = resolve_all(&CliOverrides::default(), &file, &env(&[])).unwrap();
        assert_eq!(list.len(), 1);
        assert_eq!(list[0].token, "t-file");
        assert_eq!(list[0].name, None);
    }

    // ============== 代理出站（proxy 配置 > env > 无；SOCKS 拒绝） ==============

    #[test]
    fn proxy_from_file_used_when_no_env() {
        let file = FileConfig {
            token: Some("t".into()),
            proxy: Some("http://file-proxy:7890".into()),
            ..Default::default()
        };
        let s = resolve(&CliOverrides::default(), &file, &env(&[])).unwrap();
        assert_eq!(s.proxy.as_deref(), Some("http://file-proxy:7890"));
    }

    #[test]
    fn proxy_config_beats_env() {
        // 与 TUNELY_* 不同：proxy 明确「配置 > env」——站点级配置覆盖部署环境通用变量
        let file = FileConfig {
            token: Some("t".into()),
            proxy: Some("http://file-proxy:7890".into()),
            ..Default::default()
        };
        let e = env(&[(PROXY_ENV_KEYS[0], "http://env-proxy:3128")]);
        let s = resolve(&CliOverrides::default(), &file, &e).unwrap();
        assert_eq!(s.proxy.as_deref(), Some("http://file-proxy:7890"));
    }

    #[test]
    fn proxy_env_fallback_order() {
        let file = FileConfig {
            token: Some("t".into()),
            ..Default::default()
        };
        // HTTPS_PROXY（大写优先）
        let e = env(&[
            (PROXY_ENV_KEYS[0], "http://upper:1"),
            (PROXY_ENV_KEYS[1], "http://lower:2"),
            (PROXY_ENV_KEYS[2], "http://all:3"),
        ]);
        assert_eq!(
            resolve(&CliOverrides::default(), &file, &e).unwrap().proxy.as_deref(),
            Some("http://upper:1")
        );
        // 小写 https_proxy
        let e = env(&[
            (PROXY_ENV_KEYS[1], "http://lower:2"),
            (PROXY_ENV_KEYS[2], "http://all:3"),
            (PROXY_ENV_KEYS[3], "http://all-lower:4"),
        ]);
        assert_eq!(
            resolve(&CliOverrides::default(), &file, &e).unwrap().proxy.as_deref(),
            Some("http://lower:2")
        );
        // ALL_PROXY
        let e = env(&[(PROXY_ENV_KEYS[2], "http://all:3"), (PROXY_ENV_KEYS[3], "http://all-lower:4")]);
        assert_eq!(
            resolve(&CliOverrides::default(), &file, &e).unwrap().proxy.as_deref(),
            Some("http://all:3")
        );
        // all_proxy
        let e = env(&[(PROXY_ENV_KEYS[3], "http://all-lower:4")]);
        assert_eq!(
            resolve(&CliOverrides::default(), &file, &e).unwrap().proxy.as_deref(),
            Some("http://all-lower:4")
        );
    }

    #[test]
    fn proxy_env_blank_value_is_ignored() {
        let file = FileConfig {
            token: Some("t".into()),
            ..Default::default()
        };
        let e = env(&[(PROXY_ENV_KEYS[0], "   "), (PROXY_ENV_KEYS[2], "http://all:3")]);
        assert_eq!(
            resolve(&CliOverrides::default(), &file, &e).unwrap().proxy.as_deref(),
            Some("http://all:3")
        );
    }

    #[test]
    fn proxy_none_when_neither_config_nor_env() {
        let file = FileConfig {
            token: Some("t".into()),
            ..Default::default()
        };
        let s = resolve(&CliOverrides::default(), &file, &env(&[])).unwrap();
        assert_eq!(s.proxy, None);
    }

    #[test]
    fn proxy_socks_config_rejected_at_startup() {
        // SOCKS 在配置解析期即报错（而非运行期重连循环反复失败）
        let file = FileConfig {
            token: Some("t".into()),
            proxy: Some("socks5://127.0.0.1:1080".into()),
            ..Default::default()
        };
        let err = resolve(&CliOverrides::default(), &file, &env(&[])).unwrap_err();
        assert!(err.contains("SOCKS"), "{err}");
        // env 注入的 SOCKS 同样拒绝
        let file = FileConfig {
            token: Some("t".into()),
            ..Default::default()
        };
        let e = env(&[(PROXY_ENV_KEYS[0], "socks5://127.0.0.1:1080")]);
        let err = resolve(&CliOverrides::default(), &file, &e).unwrap_err();
        assert!(err.contains("SOCKS"), "{err}");
    }

    #[test]
    fn proxy_shared_across_multi_tunnels() {
        let file: FileConfig = toml::from_str(
            r#"
proxy = "http://shared-proxy:7890"

[[tunnel]]
name = "a"
token = "t-a"

[[tunnel]]
token = "t-b"
"#,
        )
        .unwrap();
        let list = resolve_all(&CliOverrides::default(), &file, &env(&[])).unwrap();
        assert_eq!(list.len(), 2);
        assert!(list.iter().all(|s| s.proxy.as_deref() == Some("http://shared-proxy:7890")));
    }
}
