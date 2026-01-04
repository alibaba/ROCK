# 沙箱代理 (Sandbox Agent)

沙箱代理是Self-ROCK框架中用于在隔离环境中运行AI智能体任务的组件，提供安全且可控制的智能体执行环境。代理可以与模型服务(Model Service)集成，实现AI模型调用的处理。

## 架构概述

沙箱代理通过在隔离的沙箱环境中初始化和执行智能体任务，包括安装必要的依赖、配置运行环境等步骤。代理系统支持多种不同的智能体类型，如SWE-agent和IFlow CLI等，每种类型都有其特定的配置和实现方式。

代理可以与模型服务集成，在任务执行期间处理AI模型调用。当代理需要查询或调用AI模型时，模型服务会在后台处理模型通信，使代理专注于任务执行。

## Model Service 集成能力

沙箱代理支持与模型服务的无缝集成，为AI模型调用提供通信桥梁。这种集成允许代理通过文件系统通信协议与模型服务交互，实现异步的请求-响应机制。

### 集成方式
- **配置驱动**: 通过`model_service_config`参数在代理配置中指定模型服务
- **并行初始化**: 模型服务的安装过程与代理安装并行执行
- **动态启动**: 任务执行期间可根据需要启动模型服务
- **进程监控**: 通过`watch-agent`命令监控代理进程

### ModelServiceConfig 属性
- `workdir`: 模型服务工作目录
- `python_install_cmd`: Python环境安装命令
- `model_service_install_cmd`: 模型服务安装命令
- `python_install_timeout`: Python安装超时时间
- `model_service_install_timeout`: 模型服务安装超时时间
- `model_service_type`: 模型服务类型 (local/proxy)
- `model_service_session`: 服务会话名称
- `start_cmd`, `stop_cmd`, `watch_agent_cmd`: 控制命令模板
- `anti_call_llm_cmd`: 反调用LLM命令
- `logging_path`: 日志路径

## 核心组件

### 1. Agent 抽象基类
位于`rock/sdk/sandbox/agent/base.py`，定义了所有沙箱代理的抽象基类:
- `__init__()`: 初始化代理，传入沙箱实例和可选的模型服务
- `init()`: 异步初始化代理环境
- `run()`: 异步执行代理任务

### 2. AgentConfig 抽象配置类
位于`rock/sdk/sandbox/agent/config.py`，定义了代理的基本配置:
- `agent_type`: 智能体类型标识
- `version`: 版本信息

## 具体代理实现

### SWE-agent
基于`Agent`基类实现，专门处理软件工程任务。

#### SweAgentConfig 配置
- `agent_type`: 固定为"swe-agent"
- `default_run_single_config`: 默认运行配置
- `agent_session`: 代理执行的bash会话名称
- `pre_startup_bash_cmd_list`: 启动前执行的命令列表
- `post_startup_bash_cmd_list`: 启动后执行的命令列表
- `swe_agent_workdir`: 代理工作目录
- `python_install_cmd`: Python环境安装命令
- `swe_agent_install_cmd`: SWE-agent安装命令
- `python_install_timeout`: Python安装超时时间
- `swe_agent_install_timeout`: SWE-agent安装超时时间
- `agent_run_timeout`: 代理运行超时时间
- `agent_run_check_interval`: 代理运行检查间隔
- `model_service_config`: 可选的模型服务配置

#### SweAgent 实现
- `init()`: 初始化SWE-agent环境，包括安装Python环境、SWE-agent依赖和可选的模型服务
- `run()`: 执行指定的软件工程任务
- `start_model_service()`: 启动关联的模型服务
- `_install_swe_agent()`: 安装SWE-agent环境
- `_init_model_service()`: 初始化模型服务
- `_agent_run()`: 执行代理命令

### IFlow CLI Agent
基于`Agent`基类实现，处理基于命令行的智能体任务。

#### IFlowCliConfig 配置
- `agent_type`: 固定为"iflow-cli"
- `agent_session`: 代理执行的bash会话名称
- `pre_startup_bash_cmd_list`: 启动前执行的命令列表
- `npm_install_cmd`: NPM安装命令
- `npm_install_timeout`: NPM安装超时时间
- `iflow_cli_install_cmd`: IFlow CLI安装命令
- `iflow_settings`: IFlow配置设置
- `iflow_run_cmd`: 运行命令模板
- `iflow_log_file`: IFlow日志文件路径
- `session_envs`: 会话环境变量
- `model_service_config`: 可选的模型服务配置

#### IFlowCli 实现
- `init()`: 初始化IFlow CLI环境，包括安装Node.js、IFlow CLI和可选的模型服务
- `run()`: 以指定的问题陈述运行IFlow CLI
- `start_model_service()`: 启动关联的模型服务
- `_install_iflow_cli()`: 安装IFlow CLI环境
- `_init_model_service()`: 初始化模型服务
- `_extract_session_id_from_log()`: 从日志中提取会话ID
- `_get_session_id_from_sandbox()`: 从沙箱中获取会话ID
- `_agent_run()`: 执行代理命令

## 初始化流程

1. 创建沙箱环境和代理实例
2. 调用`init()`方法初始化代理
3. 在沙箱中创建专用的bash会话
4. 执行预启动命令（如环境配置）
5. 安装必要的依赖（Python、Node.js等）
6. 安装特定的智能体工具（如SWE-agent或IFlow CLI）
7. 如果配置了模型服务，将同步初始化模型服务

## 执行流程

1. 准备任务运行参数（问题陈述、项目路径等）
2. 调用`run()`方法执行任务
3. 准备运行所需的配置文件
4. 将配置文件上传到沙箱环境
5. 执行代理命令
6. 如果配置了模型服务，监控代理进程
7. 等待任务完成并返回结果

## Model Service 交互流程

当代理与模型服务集成时的工作流程:

### 任务开始前
1. 调用`start_model_service()`启动模型服务
2. 调用`watch_agent()`设置对代理进程的监控

### 代理调用LLM时
1. 代理发起模型调用请求
2. 请求通过文件通信协议写入日志文件
3. 模型服务监听器捕获请求
4. 实际的AI模型返回响应
5. 响应通过文件通信协议写回
6. 代理读取响应并继续执行

### 任务完成后
1. 如果需要，关闭模型服务进程
2. 清理相关资源

## 工作流程

### 初始化阶段
1. 创建沙箱专用会话
2. 执行环境预配置命令
3. 创建工作目录
4. 安装运行时环境（Python/Node.js）
5. 安装特定代理工具
6. 可选：初始化模型服务

### 任务执行阶段
1. 接收任务参数：问题描述、代码库路径等
2. 生成并上传特定任务配置
3. 执行智能体命令
4. 可选：启动代理监控（如果配置了模型服务）
5. 等待处理完成
6. 收集和返回执行结果

## 配置选项

### 通用代理配置
- `agent_session`: 代理运行的bash会话名
- `pre_startup_bash_cmd_list`: 启动前执行命令列表
- `session_envs`: 会话环境变量配置
- `model_service_config`: 模型服务配置（可选）

### SWE-agent 特定配置
- `swe_agent_workdir`: SWE-agent工作目录
- `python_install_cmd`: Python安装命令
- `swe_agent_install_cmd`: SWE-agent安装命令
- `default_run_single_config`: 默认运行配置

### IFlow CLI 特定配置
- `npm_install_cmd`: NPM安装命令
- `iflow_cli_install_cmd`: IFlow CLI安装命令
- `iflow_settings`: IFlow设置
- `iflow_run_cmd`: 运行命令模板
- `iflow_log_file`: 日志文件路径