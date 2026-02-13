/**
 * 后台任务示例
 * 
 * 演示使用 nohup 模式执行长时间运行的任务
 */

import { Sandbox, RunMode } from 'rl-rock';

async function backgroundTaskExample() {
  console.log('=== ROCK TypeScript SDK 后台任务示例 ===\n');

  const sandbox = new Sandbox({
    image: 'python:3.11',
    baseUrl: process.env.ROCK_BASE_URL || 'http://localhost:8080',
    cluster: 'default',
  });

  try {
    await sandbox.start();
    console.log(`沙箱已启动: ${sandbox.getSandboxId()}\n`);

    // 1. 创建一个模拟的长时间运行脚本
    console.log('1. 创建长时间运行脚本...');
    const scriptContent = `
import time
import sys

print("开始执行长时间任务...")
for i in range(10):
    print(f"进度: {i+1}/10")
    sys.stdout.flush()
    time.sleep(2)
print("任务完成!")
`;
    
    await sandbox.write_file({
      content: scriptContent,
      path: '/tmp/long_task.py',
    });
    console.log('   脚本已创建: /tmp/long_task.py');

    // 2. 使用 nohup 模式执行
    console.log('2. 使用 nohup 模式执行 (后台运行)...');
    const startTime = Date.now();
    
    const result = await sandbox.arun('python3 /tmp/long_task.py', {
      mode: RunMode.NOHUP,
      waitTimeout: 60,      // 最长等待60秒
      waitInterval: 5,      // 每5秒检查一次
      outputFile: '/tmp/task_output.log',
    });
    
    const elapsed = ((Date.now() - startTime) / 1000).toFixed(1);
    console.log(`   执行完成，耗时: ${elapsed}秒`);
    console.log(`   退出码: ${result.exitCode}`);
    console.log(`   输出:\n${result.output}`);

    // 3. 演示 ignoreOutput 模式
    console.log('3. 演示 ignoreOutput 模式 (不读取输出)...');
    
    await sandbox.write_file({
      content: 'import time; time.sleep(3); print("Done!")',
      path: '/tmp/quick_task.py',
    });
    
    const quickResult = await sandbox.arun('python3 /tmp/quick_task.py', {
      mode: RunMode.NOHUP,
      waitTimeout: 10,
      ignoreOutput: true,  // 不读取输出，适合超大输出
    });
    console.log(`   任务状态: ${quickResult.exitCode === 0 ? '成功' : '失败'}`);
    console.log(`   消息: ${quickResult.output}`);

    // 4. 并行执行多个任务
    console.log('4. 并行执行多个后台任务...');
    
    const tasks = [
      'echo "Task A" && sleep 2 && echo "A done"',
      'echo "Task B" && sleep 3 && echo "B done"',
      'echo "Task C" && sleep 1 && echo "C done"',
    ];
    
    const parallelStart = Date.now();
    
    const results = await Promise.all(
      tasks.map((cmd, i) => 
        sandbox.arun(cmd, {
          mode: RunMode.NOHUP,
          waitTimeout: 30,
          session: `task-${i}`,  // 使用独立会话
        })
      )
    );
    
    const parallelElapsed = ((Date.now() - parallelStart) / 1000).toFixed(1);
    console.log(`   并行任务完成，耗时: ${parallelElapsed}秒`);
    results.forEach((r, i) => {
      console.log(`   任务 ${i}: 退出码=${r.exitCode}`);
    });

  } finally {
    console.log('\n关闭沙箱...');
    await sandbox.close();
  }
}

// 运行示例
backgroundTaskExample().catch(console.error);
