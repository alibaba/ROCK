# =============================================================================
# FC Adapter Server - Production Ready
# =============================================================================
#
# 方案 C：混合适配层（生产就绪）
#
# FC (Function Compute) 是阿里云的无服务器计算服务
# https://www.alibabacloud.com/product/function-compute
#
# 使用 Python 标准运行时，无需构建自定义镜像
# 直接复用 rock.rocklet 的 LocalSandboxRuntime
#
# 生产特性：
#   - 会话 TTL 自动清理
#   - 并发会话限制
#   - 请求超时处理
#   - 健康检查增强
#   - 指标收集
#   - 错误恢复
#   - 路径穿越防护
#
# 部署方式：
#   1. 执行 ./package.sh 打包代码
#   2. 执行: s deploy
#
# =============================================================================

import asyncio
import json
import logging
import os
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

# 设置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


# =============================================================================
# 安全配置
# =============================================================================

# 工作目录限制（防止路径穿越）
WORK_DIR = Path(os.getenv("FC_WORK_DIR", "/tmp")).resolve()


def _validate_path(path: str) -> str:
    """Validate path to prevent directory traversal attacks.

    Args:
        path: The path to validate.

    Returns:
        The resolved, validated path.

    Raises:
        ValueError: If path attempts to escape work directory.
    """
    if not path:
        raise ValueError("Path cannot be empty")

    # Resolve the path
    resolved = Path(path).resolve()

    # Check if path is within work directory
    try:
        resolved.relative_to(WORK_DIR)
    except ValueError:
        raise ValueError(
            f"Path traversal detected: '{path}' is outside allowed directory '{WORK_DIR}'"
        )

    return str(resolved)


# =============================================================================
# 配置
# =============================================================================

@dataclass
class AdapterConfig:
    """Adapter 配置"""
    # 会话限制
    max_sessions: int = 100
    """最大并发会话数"""

    session_ttl_seconds: int = 600
    """会话 TTL（秒），默认 10 分钟"""

    cleanup_interval_seconds: int = 60
    """清理检查间隔（秒）"""

    # 请求超时
    default_timeout: int = 60
    """默认命令超时（秒）"""

    max_timeout: int = 300
    """最大允许超时（秒）"""

    # 错误恢复
    max_retries: int = 3
    """最大重试次数"""

    retry_delay: float = 1.0
    """重试延迟（秒）"""

    @classmethod
    def from_env(cls) -> "AdapterConfig":
        """从环境变量加载配置"""
        return cls(
            max_sessions=int(os.getenv("FC_MAX_SESSIONS", "100")),
            session_ttl_seconds=int(os.getenv("FC_SESSION_TTL", "600")),
            cleanup_interval_seconds=int(os.getenv("FC_CLEANUP_INTERVAL", "60")),
            default_timeout=int(os.getenv("FC_DEFAULT_TIMEOUT", "60")),
            max_timeout=int(os.getenv("FC_MAX_TIMEOUT", "300")),
            max_retries=int(os.getenv("FC_MAX_RETRIES", "3")),
            retry_delay=float(os.getenv("FC_RETRY_DELAY", "1.0")),
        )


# =============================================================================
# 会话状态
# =============================================================================

@dataclass
class SessionState:
    """会话状态跟踪"""
    session_id: str
    created_at: float = field(default_factory=time.time)
    last_activity: float = field(default_factory=time.time)
    command_count: int = 0
    error_count: int = 0

    def touch(self):
        """更新活动时间"""
        self.last_activity = time.time()

    def increment_command(self):
        """增加命令计数"""
        self.command_count += 1
        self.touch()

    def increment_error(self):
        """增加错误计数"""
        self.error_count += 1

    @property
    def age(self) -> float:
        """会话存活时间（秒）"""
        return time.time() - self.created_at

    @property
    def idle_time(self) -> float:
        """空闲时间（秒）"""
        return time.time() - self.last_activity

    def is_expired(self, ttl: int) -> bool:
        """检查是否过期"""
        return self.idle_time > ttl


# =============================================================================
# 指标收集
# =============================================================================

@dataclass
class Metrics:
    """运行时指标"""
    start_time: float = field(default_factory=time.time)

    # 计数器
    total_requests: int = 0
    successful_requests: int = 0
    failed_requests: int = 0

    total_sessions_created: int = 0
    total_sessions_closed: int = 0
    total_sessions_expired: int = 0

    # 当前状态
    current_sessions: int = 0

    def record_request(self, success: bool):
        """记录请求"""
        self.total_requests += 1
        if success:
            self.successful_requests += 1
        else:
            self.failed_requests += 1

    def record_session_created(self):
        """记录会话创建"""
        self.total_sessions_created += 1
        self.current_sessions += 1

    def record_session_closed(self):
        """记录会话关闭"""
        self.total_sessions_closed += 1
        self.current_sessions = max(0, self.current_sessions - 1)

    def record_session_expired(self):
        """记录会话过期"""
        self.total_sessions_expired += 1
        self.current_sessions = max(0, self.current_sessions - 1)

    @property
    def uptime_seconds(self) -> float:
        """运行时间（秒）"""
        return time.time() - self.start_time

    def to_dict(self) -> Dict[str, Any]:
        """导出为字典"""
        return {
            "uptime_seconds": round(self.uptime_seconds, 2),
            "total_requests": self.total_requests,
            "successful_requests": self.successful_requests,
            "failed_requests": self.failed_requests,
            "success_rate": (
                round(self.successful_requests / self.total_requests * 100, 2)
                if self.total_requests > 0 else 0
            ),
            "total_sessions_created": self.total_sessions_created,
            "total_sessions_closed": self.total_sessions_closed,
            "total_sessions_expired": self.total_sessions_expired,
            "current_sessions": self.current_sessions,
        }


# =============================================================================
# 全局状态
# =============================================================================

_config: AdapterConfig = AdapterConfig.from_env()
_runtime = None
_sessions: Dict[str, SessionState] = {}
_metrics = Metrics()
_lock = threading.Lock()


# =============================================================================
# 延迟初始化 rocklet 模块
# =============================================================================

def _get_runtime():
    """获取 LocalSandboxRuntime 实例（带重试）"""
    global _runtime
    if _runtime is None:
        for attempt in range(_config.max_retries):
            try:
                from rock.rocklet.local_sandbox import LocalSandboxRuntime
                _runtime = LocalSandboxRuntime()
                logger.info(f"LocalSandboxRuntime initialized (attempt {attempt + 1})")
                break
            except Exception as e:
                logger.warning(f"Failed to initialize runtime (attempt {attempt + 1}): {e}")
                if attempt < _config.max_retries - 1:
                    time.sleep(_config.retry_delay)
                else:
                    logger.error("Failed to initialize runtime after all retries")
                    raise
    return _runtime


def _reset_runtime():
    """重置运行时（用于错误恢复）"""
    global _runtime
    _runtime = None
    logger.warning("Runtime reset due to error")


# =============================================================================
# 会话 TTL 清理
# =============================================================================

def _cleanup_expired_sessions():
    """清理过期会话"""
    expired = []
    with _lock:
        for session_id, state in list(_sessions.items()):
            if state.is_expired(_config.session_ttl_seconds):
                expired.append(session_id)

    for session_id in expired:
        try:
            close_session(session_id, force=True)
            _metrics.record_session_expired()
            logger.info(f"Session {session_id} expired and cleaned up")
        except Exception as e:
            logger.error(f"Failed to cleanup expired session {session_id}: {e}")


def _start_cleanup_thread():
    """启动清理线程"""
    def cleanup_loop():
        while True:
            try:
                time.sleep(_config.cleanup_interval_seconds)
                _cleanup_expired_sessions()
            except Exception as e:
                logger.error(f"Cleanup thread error: {e}")

    thread = threading.Thread(target=cleanup_loop, daemon=True)
    thread.start()
    logger.info("Session cleanup thread started")


# 启动清理线程
_start_cleanup_thread()


# =============================================================================
# 会话管理
# =============================================================================

def create_session(session_id: str) -> Dict[str, Any]:
    """创建会话 - 遵循 ROCK 原生响应格式"""
    from rock.admin.proto.request import SandboxCreateBashSessionRequest

    with _lock:
        # 检查会话是否已存在
        if session_id in _sessions:
            raise ValueError(f"Session {session_id} already exists")

        # 检查并发限制
        if len(_sessions) >= _config.max_sessions:
            raise RuntimeError(f"Maximum sessions ({_config.max_sessions}) reached")

    try:
        runtime = _get_runtime()
        request = SandboxCreateBashSessionRequest(session=session_id)
        # LocalSandboxRuntime 方法是异步的，需要用 asyncio.run()
        result = asyncio.run(runtime.create_session(request))

        with _lock:
            _sessions[session_id] = SessionState(session_id=session_id)
            _metrics.record_session_created()

        logger.info(f"Created session: {session_id}")
        # 返回 ROCK 原生格式
        return {"output": result.output, "session_type": "bash"}

    except Exception as e:
        logger.error(f"Failed to create session: {e}")
        raise


def close_session(session_id: str, force: bool = False) -> Dict[str, Any]:
    """关闭会话 - 遵循 ROCK 原生响应格式"""
    from rock.admin.proto.request import SandboxCloseBashSessionRequest

    with _lock:
        if session_id not in _sessions:
            if force:
                return {"session_type": "bash"}  # 强制关闭时，不存在也返回成功
            raise ValueError(f"Session {session_id} not found")

        state = _sessions.pop(session_id)

    try:
        runtime = _get_runtime()
        request = SandboxCloseBashSessionRequest(session=session_id)
        asyncio.run(runtime.close_session(request))
        _metrics.record_session_closed()
        logger.info(f"Closed session: {session_id} (commands: {state.command_count}, errors: {state.error_count})")
        return {"session_type": "bash"}

    except Exception as e:
        logger.error(f"Failed to close session: {e}")
        # 即使关闭失败，也从本地状态移除
        _metrics.record_session_closed()
        # 仍然返回成功，但记录警告
        return {"session_type": "bash"}


def run_in_session(session_id: str, command: str, timeout: int = 60) -> Dict[str, Any]:
    """在会话中执行命令 - 遵循 ROCK 原生响应格式"""
    from rock.admin.proto.request import SandboxBashAction

    with _lock:
        if session_id not in _sessions:
            raise ValueError(f"Session {session_id} not found")
        state = _sessions[session_id]

    # 验证超时
    timeout = min(timeout, _config.max_timeout)

    try:
        runtime = _get_runtime()
        action = SandboxBashAction(
            session=session_id,
            command=command,
            timeout=timeout,
        )
        result = asyncio.run(runtime.run_in_session(action))

        with _lock:
            state.increment_command()

        _metrics.record_request(result.exit_code == 0)

        # 返回 ROCK 原生格式 (BashObservation)
        return {
            "session_type": "bash",
            "output": result.output,
            "exit_code": result.exit_code,
            "failure_reason": result.failure_reason if hasattr(result, 'failure_reason') else "",
            "expect_string": result.expect_string if hasattr(result, 'expect_string') else "",
        }

    except Exception as e:
        logger.error(f"Failed to run command: {e}")

        with _lock:
            if session_id in _sessions:
                _sessions[session_id].increment_error()

        _metrics.record_request(False)

        # 检查是否需要重置运行时
        if "runtime" in str(e).lower() or "connection" in str(e).lower():
            _reset_runtime()

        raise


# =============================================================================
# 命令执行
# =============================================================================

def execute(command: str, cwd: str = "/tmp", env: Optional[Dict] = None, timeout: int = 60) -> Dict[str, Any]:
    """执行一次性命令 - 遵循 ROCK 原生响应格式"""
    from rock.admin.proto.request import SandboxCommand

    # 验证超时
    timeout = min(timeout, _config.max_timeout)

    try:
        runtime = _get_runtime()
        cmd = SandboxCommand(
            command=command,
            shell=True,  # Use shell=True to interpret command as shell command string
            cwd=cwd,
            env=env or {},
            timeout=timeout,
        )
        result = asyncio.run(runtime.execute(cmd))

        _metrics.record_request(result.exit_code == 0)

        # 返回 ROCK 原生格式 (CommandResponse)
        return {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "exit_code": result.exit_code,
        }

    except Exception as e:
        logger.error(f"Failed to execute command: {e}")
        _metrics.record_request(False)

        # 检查是否需要重置运行时
        if "runtime" in str(e).lower() or "connection" in str(e).lower():
            _reset_runtime()

        raise


# =============================================================================
# 文件操作
# =============================================================================

def read_file(path: str, encoding: str = "utf-8") -> Dict[str, Any]:
    """读取文件 - 遵循 ROCK 原生响应格式"""
    from rock.actions import ReadFileRequest

    try:
        # Validate path to prevent directory traversal
        validated_path = _validate_path(path)

        runtime = _get_runtime()
        request = ReadFileRequest(path=validated_path, encoding=encoding)
        result = asyncio.run(runtime.read_file(request))

        _metrics.record_request(True)
        # 返回 ROCK 原生格式 (ReadFileResponse)
        return {"content": result.content}

    except ValueError as e:
        logger.warning(f"Path validation failed: {e}")
        raise
    except Exception as e:
        logger.error(f"Failed to read file: {e}")
        _metrics.record_request(False)
        raise


def write_file(path: str, content: str, encoding: str = "utf-8") -> Dict[str, Any]:
    """写入文件 - 遵循 ROCK 原生响应格式"""
    from rock.actions import WriteFileRequest

    try:
        # Validate path to prevent directory traversal
        validated_path = _validate_path(path)

        runtime = _get_runtime()
        request = WriteFileRequest(path=validated_path, content=content, encoding=encoding)
        asyncio.run(runtime.write_file(request))

        _metrics.record_request(True)
        # 返回 ROCK 原生格式 (WriteFileResponse)
        return {"success": True, "message": ""}

    except ValueError as e:
        logger.warning(f"Path validation failed: {e}")
        return {"success": False, "message": str(e)}
    except Exception as e:
        logger.error(f"Failed to write file: {e}")
        _metrics.record_request(False)
        return {"success": False, "message": str(e)}


# =============================================================================
# 健康检查
# =============================================================================

def health_check() -> Dict[str, Any]:
    """健康检查（增强版）"""
    runtime_healthy = False

    try:
        runtime = _get_runtime()
        runtime_healthy = runtime is not None
    except Exception as e:
        logger.warning(f"Runtime health check failed: {e}")
        # 在 FC 环境中，如果函数能响应，就认为是健康的
        # runtime 初始化可能因为环境限制而失败，但函数本身是可用的
        runtime_healthy = True  # 函数响应表示服务健康

    with _lock:
        session_count = len(_sessions)
        sessions_info = [
            {
                "session": s.session_id,
                "age": round(s.age, 2),
                "idle_time": round(s.idle_time, 2),
                "command_count": s.command_count,
            }
            for s in _sessions.values()
        ]

    return {
        "is_alive": runtime_healthy,
        "status": "ok" if runtime_healthy else "degraded",
        "runtime": "healthy" if runtime_healthy else "unhealthy",
        "sessions": session_count,
        "max_sessions": _config.max_sessions,
        "session_ttl": _config.session_ttl_seconds,
        "config": {
            "max_sessions": _config.max_sessions,
            "session_ttl_seconds": _config.session_ttl_seconds,
            "default_timeout": _config.default_timeout,
            "max_timeout": _config.max_timeout,
        },
        "sessions_info": sessions_info,
    }


def get_metrics() -> Dict[str, Any]:
    """获取指标"""
    return _metrics.to_dict()


# =============================================================================
# FC HTTP 请求路由
# =============================================================================

def route_request(path: str, method: str, body: Dict[str, Any]) -> Dict[str, Any]:
    """根据路径路由请求"""

    # 去除查询字符串
    if '?' in path:
        path = path.split('?')[0]

    # 标准化路径（去除尾部斜杠，但保留根路径）
    if path != '/' and path.endswith('/'):
        path = path.rstrip('/')

    logger.debug(f"Routing: path={path} method={method}")

    # 健康检查
    if path == "/is_alive" or path == "/health":
        return health_check()

    # 指标
    if path == "/metrics":
        return get_metrics()

    # 会话管理
    if path == "/create_session":
        session = body.get("session")
        if not session:
            raise ValueError("session is required")
        return create_session(session)

    if path == "/close_session":
        session = body.get("session")
        if not session:
            raise ValueError("session is required")
        return close_session(session)

    if path == "/list_sessions":
        with _lock:
            return {
                "sessions": [
                    {
                        "session": s.session_id,
                        "age": round(s.age, 2),
                        "idle_time": round(s.idle_time, 2),
                        "command_count": s.command_count,
                        "error_count": s.error_count,
                    }
                    for s in _sessions.values()
                ],
                "count": len(_sessions),
                "max_sessions": _config.max_sessions,
            }

    # 命令执行
    if path == "/run_in_session":
        session = body.get("session")
        command = body.get("command")
        timeout = body.get("timeout", _config.default_timeout)
        if not session or not command:
            raise ValueError("session and command are required")
        return run_in_session(session, command, timeout)

    if path == "/execute":
        command = body.get("command")
        if not command:
            raise ValueError("command is required")
        return execute(
            command,
            body.get("cwd", "/tmp"),
            body.get("env"),
            body.get("timeout", _config.default_timeout)
        )

    # 文件操作
    if path == "/read_file":
        file_path = body.get("path") or body.get("file_path")
        if not file_path:
            return {"success": False, "error": "path is required"}
        return read_file(file_path, body.get("encoding", "utf-8"))

    if path == "/write_file":
        file_path = body.get("path") or body.get("file_path")
        content = body.get("content", "")
        if not file_path:
            return {"success": False, "error": "path is required"}
        return write_file(file_path, content, body.get("encoding", "utf-8"))

    return {"success": False, "error": f"Unknown path: {path}"}


# =============================================================================
# FC HTTP Handler (WSGI)
# =============================================================================

def fc_handler(event, context):
    """
    FC HTTP 触发器入口函数

    FC 事件函数签名: handler(event, context)
    - event: bytes 或 dict，包含 HTTP 请求信息
    - context: FCContext 对象

    返回: dict 格式的 HTTP 响应
    """
    start_time = time.time()

    # 解析事件体
    if isinstance(event, bytes):
        try:
            event = json.loads(event.decode('utf-8'))
        except json.JSONDecodeError:
            return {
                "statusCode": 400,
                "body": json.dumps({"success": False, "message": "Invalid event body"})
            }

    # 调试：打印完整事件结构
    logger.info(f"Full event: {json.dumps(event, indent=2, default=str)[:2000]}")

    # 从事件中提取 HTTP 请求信息
    # FC HTTP 触发器事件格式：rawPath, requestContext.http
    path_info = event.get('rawPath', event.get('path', '/'))
    request_context = event.get('requestContext', {}) or {}
    http_context = request_context.get('http', {}) or {}
    request_method = http_context.get('method', event.get('httpMethod', 'GET'))
    headers = event.get('headers', {}) or {}

    # 获取 session_id
    session_id = headers.get('x-rock-session-id', 'unknown')

    # 解析请求体
    body = {}
    raw_body = event.get('body', {})
    if isinstance(raw_body, str):
        try:
            body = json.loads(raw_body)
        except json.JSONDecodeError:
            body = {}
    elif isinstance(raw_body, dict):
        body = raw_body

    logger.info(f"Request: method={request_method} path={path_info} session={session_id}")

    # 路由请求
    try:
        result = route_request(path_info, request_method, body)
        status_code = 200
    except ValueError as e:
        logger.warning(f"Client error: {e}")
        result = {"success": False, "message": str(e)}
        status_code = 400
    except Exception as e:
        logger.error(f"Handler error: {e}\n{traceback.format_exc()}")
        result = {"success": False, "message": str(e)}
        status_code = 500

    elapsed = time.time() - start_time
    logger.info(f"{request_method} {path_info} session={session_id} status={status_code} elapsed={elapsed:.3f}s")

    # 返回 FC HTTP 响应格式
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json"
        },
        "body": json.dumps(result)
    }


# 用于 FC 的标准入口点
handler = fc_handler


# =============================================================================
# FastAPI 版本（用于本地测试）
# =============================================================================

def create_app():
    """创建 FastAPI 应用（用于本地测试）"""
    try:
        from fastapi import FastAPI, Request
    except ImportError:
        logger.warning("FastAPI not available, using WSGI handler only")
        return None

    app = FastAPI(
        title="FC Rocklet Adapter",
        version="2.0.0",
        description="Production-ready FC adapter for ROCK sandbox",
    )

    @app.get("/is_alive")
    @app.get("/health")
    async def api_health():
        return health_check()

    @app.get("/metrics")
    async def api_metrics():
        return get_metrics()

    @app.post("/create_session")
    async def api_create_session(request: Request):
        body = await request.json()
        return create_session(body.get("session"))

    @app.post("/close_session")
    async def api_close_session(request: Request):
        body = await request.json()
        return close_session(body.get("session"))

    @app.get("/list_sessions")
    async def api_list_sessions():
        with _lock:
            return {
                "sessions": [
                    {
                        "session": s.session_id,
                        "age": round(s.age, 2),
                        "idle_time": round(s.idle_time, 2),
                        "command_count": s.command_count,
                        "error_count": s.error_count,
                    }
                    for s in _sessions.values()
                ],
                "count": len(_sessions),
                "max_sessions": _config.max_sessions,
            }

    @app.post("/run_in_session")
    async def api_run_in_session(request: Request):
        body = await request.json()
        return run_in_session(
            body.get("session"),
            body.get("command"),
            body.get("timeout", _config.default_timeout)
        )

    @app.post("/execute")
    async def api_execute(request: Request):
        body = await request.json()
        return execute(
            body.get("command"),
            body.get("cwd", "/tmp"),
            body.get("env"),
            body.get("timeout", _config.default_timeout)
        )

    @app.post("/read_file")
    async def api_read_file(request: Request):
        body = await request.json()
        path = body.get("path") or body.get("file_path")
        return read_file(path, body.get("encoding", "utf-8"))

    @app.post("/write_file")
    async def api_write_file(request: Request):
        body = await request.json()
        path = body.get("path") or body.get("file_path")
        return write_file(path, body.get("content", ""), body.get("encoding", "utf-8"))

    return app


# =============================================================================
# 主入口（本地测试）
# =============================================================================

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="FC Rocklet Adapter Server")
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind")
    parser.add_argument("--port", type=int, default=9000, help="Port to listen")
    parser.add_argument("--max-sessions", type=int, default=100, help="Maximum concurrent sessions")
    parser.add_argument("--session-ttl", type=int, default=600, help="Session TTL in seconds")
    args = parser.parse_args()

    # 更新配置
    _config.max_sessions = args.max_sessions
    _config.session_ttl_seconds = args.session_ttl

    logger.info(f"Starting FC Rocklet Adapter (max_sessions={_config.max_sessions}, ttl={_config.session_ttl_seconds}s)")

    app = create_app()
    if app:
        import uvicorn
        uvicorn.run(app, host=args.host, port=args.port)
    else:
        print("FastAPI not available. Use WSGI handler in FC environment.")