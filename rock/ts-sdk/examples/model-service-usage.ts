/**
 * 模型服务使用示例
 *
 * 演示如何在沙箱内安装和管理模型服务
 * 模型服务用于支持 Agent 与 LLM 的交互
 */

import { Sandbox, RuntimeEnv, PythonRuntimeEnvConfig, ModelService, ModelServiceConfig } from '../src';

async function modelServiceExample() {
  console.log('=== ROCK TypeScript SDK 模型服务示例 ===\n');

  // 创建沙箱
  console.log('1. 创建并启动沙箱...');
  const sandbox = new Sandbox({
    image: 'ubuntu:22.04',
    baseUrl: process.env.ROCK_BASE_URL || 'http://localhost:8080',
    cluster: 'default',
    memory: '8g',
    cpus: 4,
  });

  try {
    await sandbox.start();
    console.log(`   沙箱已启动: ${sandbox.getSandboxId()}\n`);

    // ==================== 安装 Python 运行时 ====================
    console.log('2. 安装 Python 运行时环境...');
    const pythonConfig: PythonRuntimeEnvConfig = {
      type: 'python',
      version: '3.11',
      pipIndexUrl: 'https://mirrors.aliyun.com/pypi/simple/',
    };

    const pythonEnv = await RuntimeEnv.create(sandbox, pythonConfig);
    console.log(`   Python 运行时已安装: ${pythonEnv.binDir}\n`);

    // ==================== 创建模型服务 ====================
    console.log('3. 创建模型服务...');
    const modelServiceConfig: ModelServiceConfig = {
      enabled: true,
      // 安装命令：使用 pip 安装 rl_rock 包（包含模型服务）
      installCmd: 'pip install rl_rock[model-service]',
      installTimeout: 600,
      // 模型服务配置
      configIniCmd: 'mkdir -p ~/.rock && touch ~/.rock/config.ini',
      // 日志配置
      loggingPath: '/tmp/rock-model-service/logs',
      loggingFileName: 'model-service.log',
    };

    const modelService = new ModelService(sandbox, modelServiceConfig);
    console.log('   模型服务已创建\n');

    // ==================== 安装模型服务 ====================
    console.log('4. 安装模型服务包...');
    await modelService.install();
    console.log('   模型服务包已安装\n');

    // ==================== 启动模型服务 ====================
    console.log('5. 启动模型服务...');
    await modelService.start();
    console.log('   模型服务已启动');
    console.log(`   运行状态: ${modelService.isStarted ? '运行中' : '已停止'}\n`);

    // ==================== 演示 watchAgent ====================
    console.log('6. 模型服务功能说明...');
    console.log('   - watchAgent(pid): 监控指定进程，保持模型服务活跃');
    console.log('   - antiCallLlm(index, responsePayload): 与 Agent 进行交互');
    console.log('   - start(): 启动模型服务');
    console.log('   - stop(): 停止模型服务\n');

    // ==================== 停止服务 ====================
    console.log('7. 停止模型服务...');
    await modelService.stop();
    console.log('   模型服务已停止\n');

    // ==================== 清理 ====================
    console.log('8. 关闭沙箱...');
    await sandbox.close();
    console.log('   沙箱已关闭');

    console.log('\n✅ 模型服务示例完成！');

  } catch (error) {
    console.error('\n❌ 示例失败:', error);
    await sandbox.close().catch(() => {});
    process.exit(1);
  }
}

// 运行示例
modelServiceExample().catch(console.error);
