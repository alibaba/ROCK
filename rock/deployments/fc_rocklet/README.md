# FC Rocklet 部署方案

本目录包含将 ROCK Sandbox 运行时部署到阿里云函数计算的两种方案。

## 目录结构

```
fc_rocklet/
├── README.md           # 本文件
├── container/          # 方案 A：自定义容器（推荐生产环境）
│   ├── Dockerfile
│   └── s.yaml
├── runtime/            # 方案 B：自定义运行时
│   ├── bootstrap
│   ├── requirements.txt
│   ├── package.sh
│   └── s.yaml
```

## 方案对比

| 方案 | 运行时 | 冷启动 | 维护成本 | 推荐场景 |
|------|--------|--------|----------|----------|
| A: 自定义容器 | custom-container | 较慢 | 需维护镜像 | 生产环境 |
| B: 自定义运行时 | custom.debian12 | 慢 | 需管理依赖 | 中等规模 |

> **备注**：方案 C（混合适配层）为概念方案，使用 Python 标准运行时 + WSGI 适配层快速验证，暂未实现。

## 统一架构

所有方案复用 `rock.rocklet.local_sandbox.LocalSandboxRuntime` 提供统一的 Sandbox 能力：

- 有状态 Bash 会话（cd、export、nohup 正常工作）
- 命令执行与流式输出
- 文件读写操作
- 健康检查

## 快速开始

### 方案 A：自定义容器（推荐）

```bash
# 1. 构建镜像
cd /path/to/ROCK
docker build -t rock-rocklet:latest -f rock/deployments/fc_rocklet/container/Dockerfile .

# 2. 推送到 ACR
docker tag rock-rocklet:latest registry.cn-hangzhou.aliyuncs.com/your-namespace/rock-rocklet:latest
docker push registry.cn-hangzhou.aliyuncs.com/your-namespace/rock-rocklet:latest

# 3. 修改 s.yaml 中的镜像地址，部署
cd rock/deployments/fc_rocklet/container
s deploy
```

### 方案 B：自定义运行时

```bash
cd rock/deployments/fc_rocklet/runtime
./package.sh
s deploy
```

## API 接口

所有方案部署后，都支持以下 HTTP API：

| 端点 | 方法 | 说明 |
|------|------|------|
| `/is_alive` | GET | 健康检查 |
| `/create_session` | POST | 创建会话 |
| `/close_session` | POST | 关闭会话 |
| `/run_in_session` | POST | 在会话中执行命令 |
| `/read_file` | POST | 读取文件 |
| `/write_file` | POST | 写入文件 |
| `/execute` | POST | 执行命令（无会话） |

## 会话亲和配置

所有方案都配置了 FC 会话隔离：

```yaml
instanceIsolationMode: SESSION_EXCLUSIVE
sessionAffinity: HEADER_FIELD
sessionAffinityConfig:
  affinityHeaderFieldName: x-rock-session-id
  sessionConcurrencyPerInstance: 1
  sessionIdleTimeoutInSeconds: 1800  # 30分钟
  sessionTTLInSeconds: 86400  # 24小时
```

客户端请求时需要携带 `x-rock-session-id` Header 以保证会话亲和。

## ROCK Admin 配置

部署完成后，更新 `rock-conf/rock-fc.yml`：

```yaml
fc:
    function_name: "rock-serverless-runtime-rocklet"  # 统一的函数名
```

启动 Admin 服务：

```bash
rock admin start --env fc
```

## 前提条件

1. 阿里云账号（已完成实名认证）
2. 开通函数计算服务
3. 安装 Serverless Devs：`npm install -g @serverless-devs/s`
4. 配置凭证：
   ```bash
   s config add  # 添加名为 `default` 的账号配置
   s config get  # 验证 default 账号已配置
   ```