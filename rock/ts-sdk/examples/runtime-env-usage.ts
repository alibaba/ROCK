/**
 * 运行时环境管理示例
 *
 * 演示如何在沙箱内安装和管理 Python/Node.js 运行时环境
 */

import { Sandbox, RunMode, RuntimeEnv, PythonRuntimeEnvConfig, NodeRuntimeEnvConfig } from '../src';

async function runtimeEnvExample() {
  console.log('=== ROCK TypeScript SDK 运行时环境示例 ===\n');

  // 创建沙箱
  console.log('1. 创建并启动沙箱...');
  const sandbox = new Sandbox({
    image: 'ubuntu:22.04',
    baseUrl: process.env.ROCK_BASE_URL || 'http://localhost:8080',
    cluster: 'default',
    memory: '4g',
    cpus: 2,
  });

  try {
    await sandbox.start();
    console.log(`   沙箱已启动: ${sandbox.getSandboxId()}\n`);

    // ==================== Python 运行时环境 ====================
    console.log('2. 安装 Python 运行时环境...');
    const pythonConfig: PythonRuntimeEnvConfig = {
      type: 'python',
      version: '3.11',
      pipPackages: ['requests', 'numpy'],
      pipIndexUrl: 'https://mirrors.aliyun.com/pypi/simple/',
    };

    const pythonEnv = await RuntimeEnv.create(sandbox, pythonConfig);
    console.log(`   Python 运行时已安装`);
    console.log(`   工作目录: ${pythonEnv.workdir}`);
    console.log(`   Bin 目录: ${pythonEnv.binDir}\n`);

    // 验证 Python 安装
    console.log('3. 验证 Python 安装...');
    const pythonVersion = await pythonEnv.run('python --version');
    console.log(`   ${pythonVersion.output.trim()}`);

    // 验证 pip 包
    const pipList = await pythonEnv.run('pip list | grep -E "requests|numpy"');
    console.log(`   已安装的包:\n${pipList.output.split('\n').map(l => '     ' + l).join('\n')}\n`);

    // 运行 Python 代码
    console.log('4. 运行 Python 代码...');
    const pythonCode = await pythonEnv.run(
      'python -c "import numpy as np; print(f\'NumPy version: {np.__version__}\')"'
    );
    console.log(`   ${pythonCode.output.trim()}\n`);

    // ==================== Node.js 运行时环境 ====================
    console.log('5. 安装 Node.js 运行时环境...');
    const nodeConfig: NodeRuntimeEnvConfig = {
      type: 'node',
      version: '22.18.0',
      npmRegistry: 'https://registry.npmmirror.com',
    };

    const nodeEnv = await RuntimeEnv.create(sandbox, nodeConfig);
    console.log(`   Node.js 运行时已安装`);
    console.log(`   工作目录: ${nodeEnv.workdir}`);
    console.log(`   Bin 目录: ${nodeEnv.binDir}\n`);

    // 验证 Node.js 安装
    console.log('6. 验证 Node.js 安装...');
    const nodeVersion = await nodeEnv.run('node --version');
    console.log(`   Node.js 版本: ${nodeVersion.output.trim()}`);

    const npmVersion = await nodeEnv.run('npm --version');
    console.log(`   npm 版本: ${npmVersion.output.trim()}\n`);

    // ==================== 使用 wrappedCmd ====================
    console.log('7. 使用 wrappedCmd 自动添加 PATH...');
    // wrappedCmd 会自动在命令前添加 PATH 环境变量
    const wrappedCmd = pythonEnv.wrappedCmd('python -c "print(\'Hello from wrapped cmd!\')"');
    const wrappedResult = await sandbox.arun(wrappedCmd, { mode: RunMode.NORMAL });
    console.log(`   ${wrappedResult.output.trim()}\n`);

    // ==================== 清理 ====================
    console.log('8. 关闭沙箱...');
    await sandbox.close();
    console.log('   沙箱已关闭');

    console.log('\n✅ 运行时环境示例完成！');

  } catch (error) {
    console.error('\n❌ 示例失败:', error);
    await sandbox.close().catch(() => {});
    process.exit(1);
  }
}

// 运行示例
runtimeEnvExample().catch(console.error);
