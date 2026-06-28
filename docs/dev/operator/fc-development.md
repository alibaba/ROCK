# FC Operator 开发文档

> **架构设计文档**：详见 [fc-architecture.md](./fc-architecture.md)，包含完整的架构设计、组件说明、SDK 集成、E2E 验证结果。

## 概述

FCOperator 是 ROCK Operator 模式的 Alibaba Cloud Function Compute (FC) 实现，用于在无服务器环境中管理 sandbox 生命周期。

### 架构位置

```
SandboxManager → FCOperator → FCRuntime → FCSessionManager → FC SDK InvokeFunction
```

FCOperator 作为 AbstractOperator 的实现，与 RayOperator、K8sOperator 并列：

| Operator | Backend | 通信方式 | 特点 |
|----------|---------|----------|------|
| RayOperator | Ray Cluster | Ray remote calls | 适合分布式计算 |
| K8sOperator | Kubernetes | HTTP to Pod | 适合容器编排 |
| FCOperator | Alibaba FC | SDK InvokeFunction | 适合无服务器 |

## 两层架构

FCOperator 采用 **模板 + 实例** 的两层架构：

- **Layer 1 - 沙箱模板（FC Function）**：按配置哈希复用 FC 函数，引用计数管理生命周期
- **Layer 2 - 沙箱实例（FC Session）**：通过 `x-rock-session-id` header 实现会话亲和

详见架构设计文档 [fc-architecture.md](./fc-architecture.md) 第 2 节。

## 配置

### FCConfig

FCConfig 在 RockConfig 中定义，包含 FC 函数的默认配置：

```python
@dataclass
class FCConfig:
    function_name: str = "rock-serverless-runtime-rocklet"
    region: str = "cn-hangzhou"
    account_id: str = ""
    access_key_id: str = ""
    access_key_secret: str = ""
    security_token: str | None = None
    default_memory: int = 4096
    default_cpus: float = 2.0
    default_session_ttl: int = 86400      # 24小时
    default_session_idle_timeout: int = 1800  # 30分钟
    default_function_timeout: int = 3600   # 1小时
```

### YAML 配置示例

```yaml
# rock-conf/rock-fc.yml
fc:
  function_name: rock-serverless-runtime-rocklet
  region: cn-hangzhou
  account_id: "1273734601317349"
  access_key_id: "${FC_ACCESS_KEY_ID}"
  access_key_secret: "${FC_ACCESS_KEY_SECRET}"

runtime:
  operator_type: fc  # 使用 FC Operator
```

## 核心方法

### submit(config, user_info)

启动 FC sandbox，通过 SDK InvokeFunction 创建会话：

1. 合并 FCConfig 默认值
2. 计算模板哈希，复用或创建 FC 函数
3. 创建 FCRuntime 实例
4. 通过 InvokeFunction 创建 Bash 会话
5. 引用计数 +1，返回 SandboxInfo

### get_status(sandbox_id)

获取 FC sandbox 状态：本地查找 → is_alive 检查 → 返回状态

### stop(sandbox_id)

停止 FC sandbox：关闭会话 → 引用计数 -1 → 按需删除函数

## Session Affinity

FC sandbox 使用 `x-rock-session-id` HTTP header 实现会话亲和性。FC 平台根据此 header 将请求路由到同一个实例，保证 session 内命令的连续性（`cd`、`export`、`nohup` 等都能正确工作）。

## SDK InvokeFunction

FC 3.0 SDK 端点格式：`UID.{region-id}.fc.aliyuncs.com`

通过 `invoke_function_with_options` 调用，payload 中 `action` 字段路由到对应操作。

## 部署

### 方案 B：自定义运行时（推荐）

```bash
cd rock/sandbox/operator/fc/runtime_example/runtime
./package.sh
s deploy --use-local -y
```

详见 [runtime_example/README.md](../../rock/sandbox/operator/fc/runtime_example/README.md)。

## 测试

| 层级 | 文件 | 内容 |
|------|------|------|
| 单元测试 | `tests/unit/sandbox/operator/fc/` | Mock SDK（56 passed, 3 xfailed） |
| 集成测试 | `tests/integration/deployments/test_fc_deployment.py` | Mock client |
| E2E 测试 | `tests/integration/deployments/test_fc_e2e.py` | 真实 FC 调用 |

E2E 测试需要设置环境变量：`FC_ACCOUNT_ID`, `FC_ACCESS_KEY_ID`, `FC_ACCESS_KEY_SECRET`

## 参考

- [fc-architecture.md](./fc-architecture.md) - 完整架构设计文档
- `rock/sandbox/operator/fc/operator.py` - FCOperator 实现
- `rock/sandbox/operator/fc/runtime.py` - FCRuntime + FCSessionManager
- `rock/sandbox/operator/fc/config.py` - FCOperatorConfig
- `rock/rocklet/local_api.py` - Rocklet API 路由
- `openspec/fc_operator_refactoring_proposal.md` - 重构方案文档
