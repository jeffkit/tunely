"""
Tunely Server - 独立的隧道服务应用

支持通过子域名访问，实现真正的 HTTP 反向代理功能。

使用示例:
    # 启动服务器
    python -m tunely.app --port 8000 --domain tunely.woa.com
    
    # 或使用 CLI
    tunely serve --port 8000 --domain tunely.woa.com

访问方式:
    # 假设有一个名为 my-agent 的隧道
    curl https://my-agent.tunely.woa.com/api/chat -d '{"message": "hello"}'
    
    # 隧道管理 API
    curl https://tunely.woa.com/api/tunnels
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict

from .server import TunnelServer, StreamStartMessage, StreamChunkMessage, StreamEndMessage
from .config import TunnelServerConfig

logger = logging.getLogger(__name__)


class AppSettings(BaseSettings):
    """应用配置"""
    
    model_config = SettingsConfigDict(env_prefix="TUNELY_")
    
    # 服务配置
    host: str = "0.0.0.0"
    port: int = 8000
    
    # 顶级域名（用于子域名解析）
    # 例如: tunely.woa.com -> *.tunely.woa.com
    domain: str = "localhost"
    
    # 数据库配置
    database_url: str = "sqlite+aiosqlite:///./data/tunely.db"
    
    # WebSocket 路径
    ws_path: str = "/ws/tunnel"
    
    # 管理 API 密钥（可选）
    admin_api_key: str | None = None
    
    # 请求超时（秒）
    request_timeout: float = 300.0


# 全局配置实例
settings = AppSettings()

# 隧道服务器实例
tunnel_server: TunnelServer | None = None


def get_tunnel_server() -> TunnelServer:
    """获取隧道服务器实例"""
    if tunnel_server is None:
        raise RuntimeError("Tunnel server not initialized")
    return tunnel_server


def extract_subdomain(host: str, base_domain: str) -> str | None:
    """
    从 Host 头中提取子域名
    
    Args:
        host: Host 头的值，例如 "my-agent.tunely.woa.com" 或 "my-agent.tunely.woa.com:8000"
        base_domain: 顶级域名，例如 "tunely.woa.com"
        
    Returns:
        子域名，例如 "my-agent"，如果不是子域名则返回 None
    """
    # 去掉端口号
    host = host.split(":")[0]
    
    # 检查是否是子域名
    if host == base_domain:
        return None
    
    # 检查是否以 .{base_domain} 结尾
    suffix = f".{base_domain}"
    if host.endswith(suffix):
        subdomain = host[:-len(suffix)]
        # 确保子域名只有一级（不包含点）
        if "." not in subdomain:
            return subdomain
    
    return None


def create_lifespan(tunnel_srv: TunnelServer):
    """创建带有 TunnelServer 引用的 lifespan 函数"""
    
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """应用生命周期管理"""
        global tunnel_server
        tunnel_server = tunnel_srv
        
        logger.info(f"Tunely Server 启动")
        logger.info(f"  监听: {settings.host}:{settings.port}")
        logger.info(f"  域名: {settings.domain}")
        logger.info(f"  数据库: {settings.database_url}")
        
        # 初始化隧道服务器（路由已在创建时注册）
        await tunnel_server.initialize()
        logger.info("  隧道服务器已初始化")
        
        yield
        
        # 关闭隧道服务器
        if tunnel_server:
            await tunnel_server.close()
        logger.info("Tunely Server 已关闭")
    
    return lifespan


def create_full_app(
    domain: str = "localhost",
    database_url: str = "sqlite+aiosqlite:///./data/tunely.db",
    admin_api_key: str | None = None,
    ws_path: str = "/ws/tunnel",
) -> FastAPI:
    """
    创建完整的 Tunely Server 应用（包含 API 路由）
    
    Args:
        domain: 顶级域名
        database_url: 数据库连接 URL
        admin_api_key: 管理 API 密钥
        ws_path: WebSocket 路径
        
    Returns:
        FastAPI 应用实例
    """
    global settings
    settings = AppSettings(
        domain=domain,
        database_url=database_url,
        admin_api_key=admin_api_key,
        ws_path=ws_path,
    )
    
    # 创建 TunnelServer（路由在此时注册）
    tunnel_srv = TunnelServer(
        config=TunnelServerConfig(
            domain=domain,
            database_url=database_url,
            ws_path=ws_path,
            admin_api_key=admin_api_key,
        )
    )
    
    # 创建 FastAPI 应用
    new_app = FastAPI(
        title="Tunely Server",
        description="WebSocket 隧道服务 - 通过子域名访问内网服务",
        version="0.2.0",
        lifespan=create_lifespan(tunnel_srv),
    )
    
    # 包含 TunnelServer 的路由（API 和 WebSocket）
    new_app.include_router(tunnel_srv.router)
    
    # 添加基础路由
    @new_app.get("/")
    async def root(request: Request):
        """根路径"""
        host = request.headers.get("host", "")
        subdomain = extract_subdomain(host, settings.domain)
        
        if subdomain:
            return await forward_to_tunnel(request, subdomain, "/")
        
        return {
            "service": "Tunely Server",
            "version": "0.2.0",
            "domain": settings.domain,
            "status": "running",
        }
    
    @new_app.get("/health")
    async def health():
        """健康检查"""
        server = get_tunnel_server()
        connected_count = len(server.manager.list_connected_domains())
        return {"status": "healthy", "connected_tunnels": connected_count}
    
    # 注意：/api/info 接口由 TunnelServer 提供，不需要在这里重复定义
    
    @new_app.api_route(
        "/{path:path}",
        methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"],
    )
    async def catch_all(request: Request, path: str):
        """通用路由 - 子域名转发"""
        host = request.headers.get("host", "")
        subdomain = extract_subdomain(host, settings.domain)
        
        if subdomain:
            full_path = f"/{path}"
            if request.query_params:
                full_path += f"?{request.query_params}"
            return await forward_to_tunnel(request, subdomain, full_path)
        
        # 主域名访问且不是 API 路由 - 返回 404
        return Response(
            content='{"detail": "Not Found"}',
            status_code=404,
            media_type="application/json",
        )
    
    return new_app


# 默认应用实例（用于 uvicorn 直接启动）
# 注意：这个实例不包含完整功能，请使用 create_full_app() 或 run_app()
app = FastAPI(
    title="Tunely Server",
    description="WebSocket 隧道服务 - 通过子域名访问内网服务",
    version="0.2.0",
)


async def forward_to_tunnel(
    request: Request, domain: str, path: str
) -> Response | StreamingResponse:
    """
    转发请求到隧道
    
    Args:
        request: FastAPI 请求对象
        domain: 隧道域名（子域名）
        path: 请求路径（包含查询参数）
        
    Returns:
        响应对象
    """
    server = get_tunnel_server()
    
    # 检查隧道是否连接
    if not server.manager.is_connected(domain):
        return Response(
            content=f'{{"error": "Tunnel not connected: {domain}"}}',
            status_code=503,
            media_type="application/json",
        )
    
    # 提取请求信息
    method = request.method
    headers = dict(request.headers)
    
    # 移除一些不应该转发的头
    headers.pop("host", None)
    headers.pop("content-length", None)
    
    # 读取请求体
    body = None
    if method in ("POST", "PUT", "PATCH"):
        body_bytes = await request.body()
        if body_bytes:
            try:
                import json
                body = json.loads(body_bytes)
            except json.JSONDecodeError:
                # 非 JSON 请求体，转为字符串
                body = body_bytes.decode("utf-8", errors="replace")
    
    # 检查是否请求 SSE
    accept_header = headers.get("accept", "")
    is_sse = "text/event-stream" in accept_header
    
    if is_sse:
        # SSE 流式响应
        return StreamingResponse(
            stream_tunnel_response(server, domain, method, path, headers, body),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
    else:
        # 普通响应
        try:
            response = await server.forward(
                domain=domain,
                method=method,
                path=path,
                headers=headers,
                body=body,
                timeout=settings.request_timeout,
            )
            
            return Response(
                content=response.body if isinstance(response.body, (str, bytes)) else 
                    __import__("json").dumps(response.body),
                status_code=response.status,
                headers=response.headers,
                media_type=response.headers.get("content-type", "application/json"),
            )
        except Exception as e:
            logger.error(f"转发请求失败: {e}", exc_info=True)
            return Response(
                content=f'{{"error": "Forward failed: {str(e)}"}}',
                status_code=502,
                media_type="application/json",
            )


async def stream_tunnel_response(
    server: TunnelServer,
    domain: str,
    method: str,
    path: str,
    headers: dict,
    body: any,
) -> AsyncIterator[str]:
    """
    流式响应生成器（SSE 格式）
    
    Yields:
        SSE 格式的数据块
    """
    try:
        async for msg in server.forward_stream(
            domain=domain,
            method=method,
            path=path,
            headers=headers,
            body=body,
            timeout=settings.request_timeout,
        ):
            if isinstance(msg, StreamStartMessage):
                # 流开始，可以发送初始事件
                yield f"event: start\ndata: {{}}\n\n"
            
            elif isinstance(msg, StreamChunkMessage):
                # 数据块
                yield f"data: {msg.data}\n\n"
            
            elif isinstance(msg, StreamEndMessage):
                # 流结束
                if msg.error:
                    yield f"event: error\ndata: {msg.error}\n\n"
                else:
                    yield f"event: done\ndata: {{}}\n\n"
                break
    
    except Exception as e:
        logger.error(f"流式转发失败: {e}", exc_info=True)
        yield f"event: error\ndata: {str(e)}\n\n"


def run_app(
    host: str = "0.0.0.0",
    port: int = 8000,
    domain: str = "localhost",
    database_url: str = "sqlite+aiosqlite:///./data/tunely.db",
    admin_api_key: str | None = None,
    ws_path: str = "/ws/tunnel",
):
    """
    运行 Tunely Server
    
    Args:
        host: 监听地址
        port: 监听端口
        domain: 顶级域名
        database_url: 数据库连接 URL
        admin_api_key: 管理 API 密钥
        ws_path: WebSocket 路径
    """
    import uvicorn
    
    global settings, app
    settings = AppSettings(
        host=host,
        port=port,
        domain=domain,
        database_url=database_url,
        admin_api_key=admin_api_key,
        ws_path=ws_path,
    )
    
    # 创建完整的应用
    full_app = create_full_app(
        domain=domain,
        database_url=database_url,
        admin_api_key=admin_api_key,
        ws_path=ws_path,
    )
    
    uvicorn.run(
        full_app,
        host=host,
        port=port,
        ws_ping_interval=None,
        ws_ping_timeout=None,
    )


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    run_app()
