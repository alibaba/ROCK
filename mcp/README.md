# ROCK MCP Tools

ROCK MCP Tools - 为OpenClaw Agent提供的阿里ROCK环境管理工具包

## 🎯 功能特性

### 核心工具
- `rock_create_agent` - 创建新的agent实例
- `rock_create_sandbox` - 创建隔离的沙箱环境
- `rock_list_agents` - 列出所有活跃的agents
- `rock_clone_agent` - agent自我复制功能
- `rock_execute_action` - 执行ROCK环境操作
- `rock_get_status` - 获取系统状态
- `rock_cleanup` - 清理资源

### 高级功能
- 🔄 **自我复制** - agent可以创建自己的副本
- 🏗️ **环境隔离** - 每个agent运行在独立的沙箱中
- 📊 **资源管理** - 自动管理CPU、内存等资源
- 🔒 **安全隔离** - 多层隔离机制确保安全
- ⚡ **分布式部署** - 支持跨机器部署

## 🚀 快速开始

### 安装依赖
```bash
pip install mcp asyncio httpx
pip install rl-rock  # ROCK SDK
```

### 启动MCP服务器
```bash
python rock_mcp_server.py
```

### 配置OpenClaw
在你的OpenClaw配置中添加ROCK MCP工具：

```json
{
  "mcpServers": {
    "rock": {
      "command": "python",
      "args": ["/path/to/rock_mcp_server.py"],
      "env": {
        "ROCK_ADMIN_URL": "http://127.0.0.1:8080"
      }
    }
  }
}
```

## 📋 工具使用示例

### 1. 创建Agent副本
```typescript
// 创建一个新的agent实例
const newAgent = await rock_create_agent({
  name: "rock-agent-clone",
  image: "python:3.11",
  memory: "4g",
  cpus: 2.0,
  capabilities: ["coding", "analysis", "automation"]
});

console.log("Agent created:", newAgent.agent_id);
```

### 2. 自我复制
```typescript
// agent可以创建自己的副本
const myClone = await rock_clone_agent({
  source_agent_id: "current-agent-id",
  clone_name: "my-clone-1",
  transfer_memory: true,
  transfer_context: true
});
```

### 3. 创建沙箱环境
```typescript
// 创建隔离的沙箱环境
const sandbox = await rock_create_sandbox({
  image: "python:3.11",
  memory: "8g",
  cpus: 4.0,
  network_mode: "isolated"
});

console.log("Sandbox created:", sandbox.sandbox_id);
```

### 4. 执行环境操作
```typescript
// 在ROCK环境中执行操作
const result = await rock_execute_action({
  sandbox_id: "sandbox-123",
  action: "python",
  command: "print('Hello from ROCK!')"
});
```

## 🏗️ 系统架构

```
┌─────────────────┐
│  OpenClaw Agent │
│   (Main Agent)  │
└────────┬────────┘
         │
         ├── MCP Protocol ───┐
         │                    │
┌────────▼────────┐   ┌──────▼────────┐
│  ROCK MCP Tool  │──▶│  ROCK Admin   │
│   Interface     │   │  (Scheduler)  │
└─────────────────┘   └──────┬────────┘
                              │
                              ├── Worker Nodes
                              ├── Sandbox Runtime
                              └── Agent Instances
```

## 🔧 配置选项

### 环境变量
- `ROCK_ADMIN_URL` - ROCK管理服务地址 (默认: http://127.0.0.1:8080)
- `ROCK_API_KEY` - API认证密钥
- `ROCK_TIMEOUT` - 请求超时时间 (默认: 30秒)

### 工具配置
```json
{
  "default_image": "python:3.11",
  "default_memory": "4g",
  "default_cpus": 2.0,
  "max_agents": 10,
  "auto_cleanup": true
}
```

## 📊 监控和管理

### 查看Agent状态
```typescript
const agents = await rock_list_agents();
agents.forEach(agent => {
  console.log(`${agent.name}: ${agent.status}`);
});
```

### 获取系统状态
```typescript
const status = await rock_get_status();
console.log("Active agents:", status.active_agents);
console.log("Total sandboxes:", status.total_sandboxes);
```

## 🔒 安全特性

- **多层隔离** - 容器级隔离 + 网络隔离
- **资源限制** - CPU、内存、磁盘使用限制
- **访问控制** - 基于API密钥的认证
- **审计日志** - 所有操作记录日志
- **自动清理** - 异常终止自动清理资源

## 🚀 高级用例

### 1. 分布式Agent集群
```typescript
// 在不同机器上创建agent副本
const machines = ["machine1", "machine2", "machine3"];
for (const machine of machines) {
  await rock_clone_agent({
    target_machine: machine,
    clone_name: `agent-${machine}`
  });
}
```

### 2. 弹性扩展
```typescript
// 根据负载自动扩展
const load = await rock_get_status();
if (load.cpu_usage > 80) {
  await rock_clone_agent({ auto_scale: true });
}
```

### 3. 任务分发
```typescript
// 创建专门处理不同任务的agents
const tasks = ["coding", "analysis", "testing"];
for (const task of tasks) {
  await rock_create_agent({
    name: `${task}-agent`,
    specialization: task
  });
}
```

## 📝 开发指南

### 添加新工具
1. 在`rock_mcp_server.py`中添加新的工具函数
2. 使用`@mcp.tool()`装饰器注册工具
3. 实现必要的错误处理和日志记录

### 测试
```bash
# 运行测试
python -m pytest tests/

# 启动开发服务器
python rock_mcp_server.py --debug
```

## 🤝 贡献

欢迎提交Issue和Pull Request！

## 📄 许可证

MIT License

## 🔗 相关资源

- [ROCK官方文档](https://alibaba.github.io/ROCK/)
- [ROCK GitHub仓库](https://github.com/alibaba/ROCK)
- [MCP协议文档](https://modelcontextprotocol.io/)

---

**ROCK MCP Tools - 让OpenClaw Agent拥有ROCK的强大环境管理能力！** 🚀