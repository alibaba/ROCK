# ROCK TypeScript SDK 开发者手册

## 目录

1. [架构概览](#架构概览)
2. [核心模块](#核心模块)
3. [API 参考](#api-参考)
4. [配置指南](#配置指南)
5. [错误处理](#错误处理)
6. [日志系统](#日志系统)
7. [高级用法](#高级用法)
8. [最佳实践](#最佳实践)

---

## 架构概览

```
┌─────────────────────────────────────────────────────────────┐
│                      ROCK TypeScript SDK                      │
├─────────────────────────────────────────────────────────────┤
│                                                               │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────┐   │
│  │   Sandbox   │  │   EnvHub    │  │       Envs          │   │
│  │   沙箱管理   │  │  环境注册   │  │  Gym风格接口        │   │
│  └──────┬──────┘  └──────┬──────┘  └──────────┬──────────┘   │
│         │                │                     │              │
│  ┌──────┴──────┐         │              ┌──────┴──────┐      │
│  │   Sub-modules│        │              │   RockEnv   │      │
│  │ - FileSystem│         │              └─────────────┘      │
│  │ - Network   │         │                                   │
│  │ - Process   │         │                                   │
│  │ - Deploy    │         │                                   │
│  │ - RuntimeEnv│         │                                   │
│  └─────────────┘         │                                   │
│                          │                                   │
│  ┌───────────────────────┴───────────────────────────────┐   │
│  │                      Model Module                      │   │
│  │              ModelClient / ModelService               │   │
│  └───────────────────────────────────────────────────────┘   │
│                                                               │
├─────────────────────────────────────────────────────────────┤
│                      Foundation Layer                         │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌────────────────┐   │
│  │  Types   │ │  Utils   │ │  Logger  │ │    Common      │   │
│  │ (Zod)    │ │ (HTTP)   │ │ (Winston)│ │ (Exceptions)   │   │
│  └──────────┘ └──────────┘ └──────────┘ └────────────────┘   │
└─────────────────────────────────────────────────────────────┘
```

---

## 核心模块

### Sandbox 模块

沙箱管理是 SDK 的核心功能，提供远程容器环境的完整生命周期管理。

#### Sandbox 类

```typescript
import { Sandbox } from 'rl-rock';

const sandbox = new Sandbox({
  image: 'python:3.11',
  baseUrl: process.env.ROCK_BASE_URL || 'http://localhost:8080',
  cluster: 'default',
  memory: '8g',
  cpus: 2,
  autoClearSeconds: 3600, // 1小时后自动清理
});
```

#### 生命周期方法

| 方法 | 说明 |
|------|------|
| `start()` | 启动沙箱 |
| `stop()` | 停止沙箱 |
| `close()` | 关闭并清理资源 |
| `isAlive()` | 检查沙箱是否存活 |
| `getStatus()` | 获取沙箱状态 |

#### 命令执行

```typescript
// 同步执行 (等待命令完成)
const result = await sandbox.arun('echo "Hello"', {
  mode: 'normal',
  session: 'default',
});

// 后台执行 (nohup 模式)
const bgResult = await sandbox.arun('python train.py', {
  mode: 'nohup',
  waitTimeout: 3600,      // 最长等待1小时
  waitInterval: 30,       // 每30秒检查一次
  ignoreOutput: false,    // 是否忽略输出
  outputFile: '/tmp/train.log', // 输出文件路径
});

// 结果结构
interface Observation {
  output: string;
  exitCode: number;
  failureReason: string;
  expectString: string;
}
```

#### 会话管理

```typescript
// 创建会话
await sandbox.createSession({
  session: 'my-session',
  startupSource: [],
  envEnable: true,
  env: { MY_VAR: 'value' },
});

// 在会话中执行命令
await sandbox.arun('source venv/bin/activate', {
  session: 'my-session',
  mode: 'normal',
});

// 关闭会话
await sandbox.closeSession({ session: 'my-session' });
```

### SandboxGroup 类

批量管理多个沙箱实例：

```typescript
import { SandboxGroup } from 'rl-rock';

const group = new SandboxGroup({
  image: 'python:3.11',
  size: 10,              // 创建10个沙箱
  startConcurrency: 3,   // 同时启动3个
  startRetryTimes: 3,    // 失败重试3次
});

// 启动所有沙箱
await group.start();

// 获取沙箱列表
const sandboxes = group.getSandboxList();

// 并行执行任务
await Promise.all(
  sandboxes.map((sandbox, i) => 
    sandbox.arun(`python task_${i}.py`)
  )
);

// 停止所有沙箱
await group.stop();
```

---

## API 参考

### FileSystem

```typescript
const fs = sandbox.getFs();

// 修改文件所有者
await fs.chown({
  remoteUser: 'appuser',
  paths: ['/data/app'],
  recursive: true,
});

// 修改文件权限
await fs.chmod({
  paths: ['/data/app/scripts.sh'],
  mode: '755',
  recursive: false,
});

// 上传目录
const result = await fs.uploadDir(
  '/local/project',
  '/remote/project',
  600 // 提取超时
);
```

### Network

```typescript
const network = sandbox.getNetwork();

// 配置 APT 镜像加速
await network.speedup(
  SpeedupType.APT,
  'http://mirrors.aliyun.com'
);

// 配置 PIP 镜像加速
await network.speedup(
  SpeedupType.PIP,
  'https://mirrors.aliyun.com/pypi/simple/'
);

// 配置 GitHub 加速
await network.speedup(
  SpeedupType.GITHUB,
  '11.11.11.11' // GitHub IP
);
```

### Process

```typescript
const process = sandbox.getProcess();

// 执行脚本
const result = await process.executeScript(
  `#!/bin/bash
echo "Starting deployment..."
npm install
npm run build
echo "Deployment complete!"`,
  {
    scriptName: 'deploy.sh',
    waitTimeout: 600,
    cleanup: true, // 执行后删除脚本
  }
);
```

### Deploy

```typescript
const deploy = sandbox.getDeploy();

// 部署工作目录
await deploy.deployWorkingDir(
  './my-project',
  '/workspace/my-project'
);

// 使用模板命令
const cmd = deploy.format('cd ${working_dir} && npm test');
await sandbox.arun(cmd);
```

---

## 配置指南

### 环境变量配置

创建 `.env` 文件或在系统中设置：

```bash
# 基础配置
ROCK_BASE_URL=http://your-rock-server:8080
ROCK_ENVHUB_BASE_URL=http://your-envhub-server:8081

# 沙箱配置
ROCK_SANDBOX_STARTUP_TIMEOUT_SECONDS=300

# OSS 配置 (大文件上传)
ROCK_OSS_ENABLE=true
ROCK_OSS_BUCKET_ENDPOINT=https://oss-cn-hangzhou.aliyuncs.com
ROCK_OSS_BUCKET_NAME=your-bucket
ROCK_OSS_BUCKET_REGION=oss-cn-hangzhou

# 日志配置
ROCK_LOGGING_PATH=/var/log/rock
ROCK_LOGGING_LEVEL=debug
ROCK_TIME_ZONE=Asia/Shanghai
```

### 代码中配置

```typescript
import { setEnvVars } from 'rl-rock';

// 或通过环境变量
process.env.ROCK_BASE_URL = 'http://custom-server:8080';
```

---

## 错误处理

### 异常类型

```typescript
import {
  RockException,
  BadRequestRockError,
  InternalServerRockError,
  CommandRockError,
  raiseForCode,
} from 'rl-rock';

try {
  await sandbox.start();
} catch (e) {
  if (e instanceof BadRequestRockError) {
    // 客户端错误 (4xxx)
    console.error('Bad request:', e.message);
  } else if (e instanceof InternalServerRockError) {
    // 服务端错误 (5xxx)
    console.error('Server error:', e.message);
  } else if (e instanceof CommandRockError) {
    // 命令执行错误 (6xxx) - 不抛出异常，返回在结果中
    console.error('Command failed:', e.message);
  } else {
    throw e;
  }
}
```

### 错误码

| 范围 | 类型 | 说明 |
|------|------|------|
| 2000-2999 | Success | 成功 |
| 4000-4999 | Client Error | 客户端错误 |
| 5000-5999 | Server Error | 服务端错误 |
| 6000-6999 | Command Error | 命令执行错误 |

---

## 日志系统

### 基本使用

```typescript
import { initLogger } from 'rl-rock';

const logger = initLogger('my-module');

logger.debug('Debug message');
logger.info('Info message');
logger.warn('Warning message');
logger.error('Error message');
```

### 日志格式

```
2025-02-12T10:30:45.123+08:00 INFO:client.ts:100 [rock.sandbox] [sandbox-id] [trace-id] -- Sandbox started successfully
```

### 创建带沙箱上下文的日志

```typescript
import { createSandboxLogger } from 'rl-rock';

const logger = createSandboxLogger('my-module', sandbox.getSandboxId());
logger.info('This message includes sandbox context');
```

---

## 高级用法

### 自定义 HTTP 客户端

```typescript
import { HttpUtils } from 'rl-rock';

// 创建自定义 HTTP 客户端
const client = new HttpUtils({
  timeout: 60000,
  baseURL: 'https://custom-api.example.com',
});

const response = await client.get('/endpoint');
```

### 重试机制

```typescript
import { withRetry, retryAsync } from 'rl-rock';

// 使用装饰器
class MyService {
  @withRetry({ maxAttempts: 3, delaySeconds: 1, backoff: 2 })
  async fetchData(): Promise<Data> {
    // 自动重试
  }
}

// 使用函数包装
const result = await retryAsync(
  () => sandbox.arun('flaky-command'),
  { maxAttempts: 5, delaySeconds: 2 }
);
```

### 并发控制

```typescript
import { SandboxGroup } from 'rl-rock';

// 使用 Semaphore 控制并发
import { Semaphore } from 'async-mutex';

const semaphore = new Semaphore(5);

const tasks = sandboxes.map(async (sandbox) => {
  const [, release] = await semaphore.acquire();
  try {
    return await sandbox.arun('heavy-task');
  } finally {
    release();
  }
});
```

---

## 最佳实践

### 1. 资源清理

```typescript
// 始终在 finally 中清理资源
let sandbox: Sandbox | null = null;
try {
  sandbox = new Sandbox(config);
  await sandbox.start();
  // ... 工作
} finally {
  if (sandbox) {
    await sandbox.close();
  }
}

// 或使用 async context (推荐)
async function withSandbox<T>(
  config: SandboxConfig,
  fn: (sandbox: Sandbox) => Promise<T>
): Promise<T> {
  const sandbox = new Sandbox(config);
  try {
    await sandbox.start();
    return await fn(sandbox);
  } finally {
    await sandbox.close();
  }
}
```

### 2. 错误处理

```typescript
// 区分可恢复和不可恢复错误
async function executeTask(sandbox: Sandbox) {
  const result = await sandbox.arun('task-command');
  
  if (result.exitCode !== 0) {
    // 命令执行失败，可重试
    logger.warn(`Task failed: ${result.failureReason}`);
    return { success: false, error: result.failureReason };
  }
  
  return { success: true, output: result.output };
}
```

### 3. 超时设置

```typescript
// 为长时间操作设置合理超时
const result = await sandbox.arun('long-running-task', {
  mode: 'nohup',
  waitTimeout: 3600,    // 1小时
  waitInterval: 60,     // 每分钟检查
});
```

### 4. 会话隔离

```typescript
// 为不同任务使用独立会话
const sessionId = `task-${Date.now()}`;

await sandbox.createSession({
  session: sessionId,
  envEnable: true,
  env: taskSpecificEnv,
});

try {
  await sandbox.arun('task', { session: sessionId });
} finally {
  await sandbox.closeSession({ session: sessionId });
}
```

---

## 类型定义

完整类型定义请参考 `dist/index.d.ts` 或源码中的 Zod schemas。

### 主要类型

```typescript
// 配置类型
interface SandboxConfig { ... }
interface SandboxGroupConfig { ... }

// 请求类型
interface Command { ... }
interface CreateBashSessionRequest { ... }
interface WriteFileRequest { ... }
interface ReadFileRequest { ... }

// 响应类型
interface Observation { ... }
interface CommandResponse { ... }
interface SandboxStatusResponse { ... }

// 枚举
enum SpeedupType { APT, PIP, GITHUB }
enum RunMode { NORMAL, NOHUP }
```

---

## 常见问题

### Q: 如何调试沙箱问题？

```typescript
// 启用详细日志
process.env.ROCK_LOGGING_LEVEL = 'debug';

// 获取沙箱状态
const status = await sandbox.getStatus();
console.log(status);
```

### Q: 如何处理大文件上传？

```typescript
// 启用 OSS 上传 (> 1MB 自动使用 OSS)
process.env.ROCK_OSS_ENABLE = 'true';
process.env.ROCK_OSS_BUCKET_NAME = 'your-bucket';
// ... 其他 OSS 配置

await sandbox.upload({
  sourcePath: './large-file.zip',
  targetPath: '/remote/large-file.zip',
});
```

### Q: 如何实现并行任务？

```typescript
const group = new SandboxGroup({
  image: 'python:3.11',
  size: 10,
  startConcurrency: 5,
});

await group.start();

const results = await Promise.allSettled(
  group.getSandboxList().map((s, i) => 
    s.arun(`python task_${i}.py`)
  )
);
```
