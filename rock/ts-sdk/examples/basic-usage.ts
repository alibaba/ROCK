/**
 * 基础沙箱使用示例
 * 
 * 演示如何创建、管理和使用沙箱环境
 */

import { Sandbox, RunMode } from '../src';

async function basicExample() {
  console.log('=== ROCK TypeScript SDK 基础示例 ===\n');

  // 1. 创建沙箱
  console.log('1. 创建沙箱...');
  const sandbox = new Sandbox({
    image: 'reg.docker.alibaba-inc.com/yanan/python:3.11',
    baseUrl: process.env.ROCK_BASE_URL || 'http://localhost:8080',
    cluster: 'default',
    memory: '4g',
    cpus: 2,
    autoClearSeconds: 1800, // 30分钟后自动清理
  });

  try {
    // 2. 启动沙箱
    console.log('2. 启动沙箱...');
    await sandbox.start();
    console.log(`   沙箱已启动: ${sandbox.getSandboxId()}`);

    // 3. 检查状态
    console.log('3. 检查沙箱状态...');
    const status = await sandbox.getStatus();
    console.log(`   状态: ${status.is_alive ? '运行中' : '已停止'}`);
    console.log(`   镜像: ${status.image}`);

    // 4. 执行简单命令
    console.log('4. 执行命令...');
    const result = await sandbox.arun('echo "Hello from ROCK SDK!" && pwd', {
      mode: RunMode.NORMAL,
    });
    console.log(`   输出: ${result.output.trim()}`);
    console.log(`   退出码: ${result.exit_code}`);

    // 5. 使用 Python
    console.log('5. 运行 Python 代码...');
    const pythonResult = await sandbox.arun(
      'python3 -c "import sys; print(f\'Python {sys.version}\')"',
      { mode: RunMode.NORMAL }
    );
    console.log(`   Python 版本: ${pythonResult.output.trim().split('\n')[0]}`);

    // 6. 文件操作示例
    console.log('6. 文件操作...');
    await sandbox.arun('echo "Hello ROCK" > /tmp/test.txt', { mode: RunMode.NORMAL });
    const catResult = await sandbox.arun('cat /tmp/test.txt', { mode: RunMode.NORMAL });
    console.log(`   文件内容: ${catResult.output.trim()}`);

  } finally {
    // 7. 清理
    console.log('\n7. 关闭沙箱...');
    await sandbox.close();
    console.log('   沙箱已关闭');
  }
}

// 运行示例
basicExample().catch(console.error);
