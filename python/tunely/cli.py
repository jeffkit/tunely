"""
WS-Tunnel 命令行工具

使用示例:
    # 启动客户端
    tunely connect --token tun_xxx --target http://localhost:8080

    # 使用配置文件
    tunely connect --config tunnel.yaml

    # 管理隧道
    ws-tunnel tunnel create my-agent
    ws-tunnel tunnel list
    ws-tunnel tunnel delete my-agent
"""

import asyncio
import logging
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

from .client import TunnelClient
from .config import TunnelClientConfig

console = Console()


def setup_logging(verbose: bool = False) -> None:
    """配置日志"""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(rich_tracebacks=True)],
    )


def _pkg_version() -> str:
    try:
        from importlib.metadata import version
        return version("tunely")
    except Exception:
        try:
            from . import __version__ as _v
            return _v
        except Exception:
            return "unknown"


@click.group()
@click.version_option(version=_pkg_version())
def main():
    """WS-Tunnel - WebSocket 透明反向代理隧道"""
    pass


@main.command()
@click.option("--host", "-h", default="0.0.0.0", help="监听地址")
@click.option("--port", "-p", default=8000, help="监听端口")
@click.option("--domain", "-d", default="localhost", help="顶级域名（用于子域名解析）")
@click.option(
    "--database",
    "-D",
    default="sqlite+aiosqlite:///./data/tunely.db",
    help="数据库连接 URL",
)
@click.option("--api-key", "-k", help="管理 API 密钥（未提供时回退读环境变量 WS_TUNNEL_ADMIN_API_KEY）")
@click.option("--ws-path", default="/ws/tunnel", help="WebSocket 路径")
@click.option("--cors-origins", default="", help="CORS 允许的来源（逗号分隔；* 表示全部；默认空 = 仅同源）")
@click.option("--ssl-certfile", default=None, help="控制面 TLS 证书（PEM）；与 --ssl-keyfile 成对提供后以 HTTPS/WSS 终止（缺省回退读 TUNELY_SSL_CERT_FILE）")
@click.option("--ssl-keyfile", default=None, help="控制面 TLS 私钥（PEM）（缺省回退读 TUNELY_SSL_KEY_FILE）")
@click.option("--verbose", "-v", is_flag=True, help="详细日志")
def serve(
    host: str,
    port: int,
    domain: str,
    database: str,
    api_key: str,
    ws_path: str,
    cors_origins: str,
    ssl_certfile: str | None,
    ssl_keyfile: str | None,
    verbose: bool,
):
    """启动 Tunely Server（独立隧道服务）"""
    import os
    setup_logging(verbose)

    # --api-key 未显式提供时回退到环境变量（避免密钥只能经命令行传入、被 ps 看到）
    if api_key is None:
        api_key = os.environ.get("WS_TUNNEL_ADMIN_API_KEY")
    
    console.print(f"[bold blue]Tunely Server v{_pkg_version()}[/bold blue]")
    console.print(f"  监听: {host}:{port}")
    console.print(f"  域名: {domain}")
    console.print(f"  数据库: {database}")
    console.print(f"  WebSocket: {ws_path}")
    console.print(f"  CORS: {cors_origins}")
    console.print()
    console.print(f"[dim]访问方式:[/dim]")
    console.print(f"  管理 API:    http://{domain}/api/tunnels")
    console.print(f"  子域名模式:  http://{{subdomain}}.{domain}/")
    console.print(f"  路径前缀模式: http://{domain}/t/{{tunnel-name}}/")
    console.print()
    
    # 设置 CORS 环境变量（供 AppSettings 读取）
    os.environ["TUNELY_CORS_ORIGINS"] = cors_origins

    if ssl_certfile or ssl_keyfile:
        console.print(f"  TLS(控制面): {'已启用（HTTPS/WSS）' if ssl_certfile and ssl_keyfile else '配置不完整！cert/key 必须成对'}")
    console.print(f"[dim]提示:[/dim] TCP/UDP 监听 TLS 见 WS_TUNNEL_LISTENER_TLS_CERT_FILE/KEY_FILE")

    from .app import run_app

    run_app(
        host=host,
        port=port,
        domain=domain,
        database_url=database,
        admin_api_key=api_key,
        ws_path=ws_path,
        ssl_certfile=ssl_certfile,
        ssl_keyfile=ssl_keyfile,
    )


@main.command()
@click.option("--server", "-s", default=None, help="服务端 WebSocket URL")
@click.option("--token", "-t", default=None, help="隧道令牌（--config 多隧道时省略）")
@click.option("--target", "-T", default=None, help="本地目标服务 URL")
@click.option("--reconnect", "-r", default=None, type=float, help="重连间隔（秒）")
@click.option("--force", "-f", is_flag=True, default=None, help="强制抢占已有连接")
@click.option(
    "--config",
    "-c",
    "config_path",
    default=None,
    type=click.Path(exists=True, dir_okay=False),
    help="TOML 配置文件（支持 [[tunnel]] 多隧道，键名与 rust 客户端 client.toml 一致）",
)
@click.option("--verbose", "-v", is_flag=True, help="详细日志")
def connect(
    server: str | None,
    token: str | None,
    target: str | None,
    reconnect: float | None,
    force: bool | None,
    config_path: str | None,
    verbose: bool,
):
    """连接到隧道服务器（--config 支持单进程多隧道）"""
    setup_logging(verbose)

    if config_path:
        from .config import load_client_settings_from_toml

        try:
            settings_list = load_client_settings_from_toml(
                config_path,
                cli_server=server,
                cli_token=token,
                cli_target=target,
                cli_reconnect=reconnect,
                cli_force=force,
            )
        except ValueError as e:
            console.print(f"[red]配置错误: {e}[/red]")
            sys.exit(1)
    else:
        if not token:
            console.print("[red]错误: 缺少 --token（或用 --config 指定配置文件）[/red]")
            sys.exit(1)
        settings_list = [
            {
                "server_url": server or "ws://localhost:8000/ws/tunnel",
                "token": token,
                "target_url": target or "http://localhost:8080",
                "reconnect_interval": reconnect if reconnect is not None else 5.0,
                "force": bool(force),
                "name": None,
            }
        ]

    multi = len(settings_list) > 1
    console.print("[bold blue]WS-Tunnel Client[/bold blue]")
    console.print(f"  服务端: {settings_list[0]['server_url']}")
    if multi:
        console.print(f"  隧道 ({len(settings_list)}):")
        for s in settings_list:
            console.print(f"    - {s['name']} -> {s['target_url']}")
    else:
        console.print(f"  目标: {settings_list[0]['target_url']}")
        if settings_list[0]["force"]:
            console.print("  [yellow]强制模式: 将抢占已有连接[/yellow]")
    console.print()

    async def run_all() -> None:
        clients = []
        for s in settings_list:
            prefix = f"[{s['name']}] " if multi else ""
            client = TunnelClient(config=TunnelClientConfig(**s))

            def make_callbacks(c=client, p=prefix):
                def on_connect():
                    console.print(f"{p}[green]✓[/green] 已连接: domain={c.domain}")

                def on_disconnect():
                    console.print(f"{p}[yellow]![/yellow] 连接断开")

                return on_connect, on_disconnect

            on_conn, on_disc = make_callbacks(c=client, p=prefix)
            client.on_connect(on_conn)
            client.on_disconnect(on_disc)
            clients.append(client)

        # 每条隧道一个独立任务：重连互不影响，全部退出才结束
        tasks = [asyncio.create_task(c.run()) for c in clients]
        try:
            await asyncio.gather(*tasks)
        finally:
            await asyncio.gather(*(c.stop() for c in clients), return_exceptions=True)

    try:
        asyncio.run(run_all())
    except KeyboardInterrupt:
        console.print("\n[dim]已停止[/dim]")
        sys.exit(0)


@main.group()
def tunnel():
    """管理隧道"""
    pass


@tunnel.command("create")
@click.argument("domain")
@click.option("--name", "-n", help="隧道名称")
@click.option("--description", "-d", help="隧道描述")
@click.option(
    "--mode",
    "-m",
    type=click.Choice(["http", "tcp"]),
    default="http",
    help="隧道模式: http（应用层转发）/ tcp（传输层原始转发）",
)
@click.option("--server", "-s", default="http://localhost:8000", help="服务端 URL")
@click.option("--api-key", "-k", help="管理 API 密钥")
def tunnel_create(
    domain: str, name: str, description: str, mode: str, server: str, api_key: str
):
    """创建隧道"""
    import httpx

    headers = {}
    if api_key:
        headers["x-api-key"] = api_key

    try:
        response = httpx.post(
            f"{server}/api/tunnels",
            json={
                "domain": domain,
                "name": name,
                "description": description,
                "mode": mode,
            },
            headers=headers,
        )

        if response.status_code == 201 or response.status_code == 200:
            data = response.json()
            console.print(f"[green]✓[/green] 隧道已创建")
            console.print(f"  域名: {data['domain']}")
            console.print(f"  令牌: [bold]{data['token']}[/bold]")
            console.print(f"  模式: {data.get('mode', 'http')}")
            console.print()
            console.print("[dim]使用以下命令连接:[/dim]")
            console.print(f"  tunely connect --token {data['token']} --target http://localhost:8080")
        else:
            console.print(f"[red]✗[/red] 创建失败: {response.text}")
            sys.exit(1)

    except Exception as e:
        console.print(f"[red]✗[/red] 请求失败: {e}")
        sys.exit(1)


@tunnel.command("list")
@click.option("--server", "-s", default="http://localhost:8000", help="服务端 URL")
@click.option("--api-key", "-k", help="管理 API 密钥")
def tunnel_list(server: str, api_key: str):
    """列出所有隧道"""
    import httpx

    headers = {}
    if api_key:
        headers["x-api-key"] = api_key

    try:
        response = httpx.get(f"{server}/api/tunnels", headers=headers)

        if response.status_code == 200:
            tunnels = response.json()

            if not tunnels:
                console.print("[dim]没有隧道[/dim]")
                return

            table = Table(title="隧道列表")
            table.add_column("域名", style="cyan")
            table.add_column("名称")
            table.add_column("状态")
            table.add_column("连接")
            table.add_column("请求数", justify="right")

            for t in tunnels:
                status = "[green]启用[/green]" if t["enabled"] else "[red]禁用[/red]"
                connected = "[green]●[/green]" if t["connected"] else "[dim]○[/dim]"
                table.add_row(
                    t["domain"],
                    t.get("name") or "-",
                    status,
                    connected,
                    str(t.get("total_requests", 0)),
                )

            console.print(table)
        else:
            console.print(f"[red]✗[/red] 请求失败: {response.text}")
            sys.exit(1)

    except Exception as e:
        console.print(f"[red]✗[/red] 请求失败: {e}")
        sys.exit(1)


@tunnel.command("delete")
@click.argument("domain")
@click.option("--server", "-s", default="http://localhost:8000", help="服务端 URL")
@click.option("--api-key", "-k", help="管理 API 密钥")
@click.option("--yes", "-y", is_flag=True, help="跳过确认")
def tunnel_delete(domain: str, server: str, api_key: str, yes: bool):
    """删除隧道"""
    import httpx

    if not yes:
        if not click.confirm(f"确定删除隧道 {domain}?"):
            return

    headers = {}
    if api_key:
        headers["x-api-key"] = api_key

    try:
        response = httpx.delete(f"{server}/api/tunnels/{domain}", headers=headers)

        if response.status_code == 200:
            console.print(f"[green]✓[/green] 隧道已删除: {domain}")
        else:
            console.print(f"[red]✗[/red] 删除失败: {response.text}")
            sys.exit(1)

    except Exception as e:
        console.print(f"[red]✗[/red] 请求失败: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
