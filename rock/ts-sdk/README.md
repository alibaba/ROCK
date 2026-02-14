# ROCK TypeScript SDK

[![npm version](https://img.shields.io/npm/v/rl-rock.svg)](https://www.npmjs.com/package/rl-rock)
[![License](https://img.shields.io/npm/l/rl-rock.svg)](https://github.com/Timandes/ROCK/blob/master/LICENSE)
[![TypeScript](https://img.shields.io/badge/TypeScript-5.x-blue.svg)](https://www.typescriptlang.org/)

ROCK (Remote Operation Container Kit) TypeScript SDK - 用于管理远程沙箱环境的客户端库。

## 特性

- 🚀 **沙箱管理** - 创建、启动、停止远程容器沙箱
- 📁 **文件系统** - 上传、下载、读取、写入文件
- 🖥️ **命令执行** - 同步/异步执行 Shell 命令
- 🔧 **运行时环境** - 支持 Python、Node.js 运行时环境管理
- 🤖 **Agent 框架** - 内置 Agent 支持自动化任务
- 📦 **EnvHub** - 环境注册与管理
- 🔄 **双模式构建** - 同时支持 ESM 和 CommonJS

## 安装

```bash
# 使用 pnpm
pnpm add rl-rock

# 使用 npm
npm install rl-rock

# 使用 yarn
yarn add rl-rock
```

## 快速开始

### 创建沙箱

```typescript
import { Sandbox, SandboxConfig } from 'rl-rock';

// 创建沙箱实例
const sandbox = new Sandbox({
  image: 'python:3.11',
  baseUrl: 'http://localhost:8080',
  cluster: 'default',
  memory: '8g',
  cpus: 2,
});

// 启动沙箱
await sandbox.start();

console.log(`Sandbox ID: ${sandbox.getSandboxId()}`);
```

### 执行命令

```typescript
// 同步执行命令
const result = await sandbox.arun('ls -la', {
  mode: 'normal',
});

console.log(result.output);

// 后台执行命令 (nohup 模式)
const bgResult = await sandbox.arun('python long_running_script.py', {
  mode: 'nohup',
  waitTimeout: 600,
});
```

### 文件操作

```typescript
// 写入文件
await sandbox.write_file({
  content: 'Hello, ROCK!',
  path: '/tmp/hello.txt',
});

// 读取文件
const fileContent = await sandbox.read_file({
  path: '/tmp/hello.txt',
});

// 上传本地文件
await sandbox.upload({
  sourcePath: './local-file.txt',
  targetPath: '/remote/path/file.txt',
});
```

### 使用 EnvHub

```typescript
import { EnvHubClient } from 'rl-rock';

const client = new EnvHubClient({
  baseUrl: 'http://localhost:8081',
});

// 注册环境
await client.register({
  envName: 'my-python-env',
  image: 'python:3.11',
  description: 'My Python environment',
  tags: ['python', 'ml'],
});

// 获取环境
const env = await client.getEnv('my-python-env');
```

### 使用 RockEnv (Gym 风格接口)

```typescript
import { make, RockEnv } from 'rl-rock';

// 创建环境
const env = make('my-env-id');

// 重置环境
const [observation, info] = await env.reset();

// 执行步骤
const [obs, reward, terminated, truncated, info] = await env.step('action');

// 关闭环境
await env.close();
```

## 配置

### 环境变量

| 变量名 | 说明 | 默认值 |
|--------|------|--------|
| `ROCK_BASE_URL` | ROCK 服务基础 URL | `http://localhost:8080` |
| `ROCK_ENVHUB_BASE_URL` | EnvHub 服务 URL | `http://localhost:8081` |
| `ROCK_SANDBOX_STARTUP_TIMEOUT_SECONDS` | 沙箱启动超时时间 | `180` |
| `ROCK_OSS_ENABLE` | 是否启用 OSS 上传 | `false` |
| `ROCK_OSS_BUCKET_ENDPOINT` | OSS Endpoint | - |
| `ROCK_OSS_BUCKET_NAME` | OSS Bucket 名称 | - |

### SandboxConfig 选项

```typescript
interface SandboxConfig {
  image: string;           // Docker 镜像
  baseUrl: string;         // 服务 URL
  cluster: string;         // 集群名称
  memory: string;          // 内存限制 (如 '8g')
  cpus: number;            // CPU 核心数
  autoClearSeconds: number; // 自动清理时间
  startupTimeout: number;  // 启动超时
  routeKey?: string;       // 路由键
  extraHeaders?: Record<string, string>; // 额外请求头
}
```

## API 文档

详细 API 文档请参阅 [开发者手册](./docs/DEVELOPER_GUIDE.md)。

## 示例

更多示例请参阅 [examples](./examples/) 目录。

### 示例列表

| 文件 | 说明 |
|------|------|
| `basic-usage.ts` | 基础沙箱使用示例 |
| `file-operations.ts` | 文件操作示例 |
| `background-tasks.ts` | 后台任务 (nohup) 示例 |
| `envhub-usage.ts` | EnvHub 环境管理示例 |
| `sandbox-group.ts` | 沙箱组批量操作示例 |
| `complete-workflow.ts` | 完整开发工作流示例 |

### 运行示例

```bash
# 进入 ts-sdk 目录
cd ts-sdk

# 安装依赖 (如未安装)
pnpm install

# 设置环境变量
export ROCK_BASE_URL=http://your-rock-server:8080
export ROCK_ENVHUB_BASE_URL=http://your-envhub-server:8081

# 运行示例 (使用 tsx)
npx tsx examples/basic-usage.ts

# 或使用 ts-node
npx ts-node examples/basic-usage.ts
```

## 开发

```bash
# 安装依赖
pnpm install

# 运行测试
pnpm test

# 构建
pnpm build

# 类型检查
pnpm exec tsc --noEmit
```

## 从 Python SDK 迁移

TypeScript SDK 与 Python SDK API 基本一致，主要差异：

| Python | TypeScript |
|--------|-----------|
| `sandbox.arun(cmd, mode="nohup")` | `sandbox.arun(cmd, { mode: 'nohup' })` |
| `await sandbox.fs.upload_dir(...)` | `await sandbox.getFs().uploadDir(...)` |
| `sandbox.process.execute_script(...)` | `sandbox.getProcess().executeScript(...)` |

### 响应字段命名

SDK 使用 **camelCase** 命名规范，符合 TypeScript 约定。HTTP 层自动处理 snake_case 到 camelCase 的转换：

```typescript
// 响应示例 - 使用 camelCase
const status = await sandbox.getStatus();
console.log(status.sandboxId);    // ✓ 正确
console.log(status.hostName);     // ✓ 正确
console.log(status.isAlive);      // ✓ 正确

const result = await sandbox.arun('ls');
console.log(result.exitCode);     // ✓ 正确
console.log(result.failureReason);// ✓ 正确
```

## License

Apache License 2.0
