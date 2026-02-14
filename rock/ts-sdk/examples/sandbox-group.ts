/**
 * 沙箱组批量操作示例
 * 
 * 演示如何批量创建和管理多个沙箱
 */

import { SandboxGroup, RunMode } from '../src';

async function sandboxGroupExample() {
  console.log('=== ROCK TypeScript SDK 沙箱组示例 ===\n');

  // 1. 创建沙箱组
  console.log('1. 创建沙箱组 (5个沙箱)...');
  const group = new SandboxGroup({
    image: 'python:3.11-slim',
    baseUrl: process.env.ROCK_BASE_URL || 'http://localhost:8080',
    cluster: 'default',
    size: 5,               // 创建5个沙箱
    startConcurrency: 2,   // 同时启动2个
    startRetryTimes: 3,    // 失败重试3次
    memory: '2g',
    cpus: 1,
    autoClearSeconds: 1800,
  });

  try {
    // 2. 批量启动
    console.log('2. 批量启动沙箱 (并发数=2)...');
    const startBegin = Date.now();
    await group.start();
    const startElapsed = ((Date.now() - startBegin) / 1000).toFixed(1);
    console.log(`   所有沙箱已启动，耗时: ${startElapsed}秒\n`);

    const sandboxes = group.getSandboxList();
    console.log(`   沙箱列表:`);
    sandboxes.forEach((s, i) => {
      console.log(`   ${i + 1}. ${s.getSandboxId()}`);
    });

    // 3. 并行执行相同任务
    console.log('\n3. 并行执行相同任务...');
    const taskResults = await Promise.all(
      sandboxes.map(async (sandbox, index) => {
        const result = await sandbox.arun(
          `echo "Sandbox ${index + 1} reporting" && hostname && date`,
          { mode: RunMode.NORMAL }
        );
        return { index, id: sandbox.getSandboxId(), output: result.output.trim() };
      })
    );
    
    taskResults.forEach(({ index, id, output }) => {
      console.log(`   沙箱 ${index + 1} (${id?.substring(0, 8)}...):`);
      console.log(`     ${output.replace(/\n/g, '\n     ')}`);
    });

    // 4. 分配不同任务
    console.log('\n4. 分配不同任务给不同沙箱...');
    const tasks = [
      'python3 -c "print(sum(range(100)))"',
      'python3 -c "import math; print(math.pi)"',
      'python3 -c "print([x**2 for x in range(10)])"',
      'python3 -c "import json; print(json.dumps({\\"status\\": \\"ok\\"}))"',
      'python3 -c "print(len(\\"Hello, World!\\"))"',
    ];

    const diffResults = await Promise.all(
      sandboxes.map((sandbox, i) =>
        sandbox.arun(tasks[i] || 'echo "No task"', { mode: RunMode.NORMAL })
      )
    );

    diffResults.forEach((result, i) => {
      console.log(`   任务 ${i + 1}: ${result.output.trim()}`);
    });

    // 5. MapReduce 风格处理
    console.log('\n5. MapReduce 风格数据处理...');
    
    // 准备数据
    const data = Array.from({ length: 100 }, (_, i) => i + 1);
    const chunkSize = Math.ceil(data.length / sandboxes.length);
    
    // Map: 分配计算任务
    const mapResults = await Promise.all(
      sandboxes.map(async (sandbox, i) => {
        const chunk = data.slice(i * chunkSize, (i + 1) * chunkSize);
        const script = `
import json
data = json.loads('${JSON.stringify(chunk)}')
result = sum(x * x for x in data)
print(result)
`;
        await sandbox.write_file({
          content: script,
          path: `/tmp/map_task_${i}.py`,
        });
        
        const result = await sandbox.arun(`python3 /tmp/map_task_${i}.py`, {
          mode: RunMode.NORMAL,
        });
        
        return parseInt(result.output.trim(), 10);
      })
    );
    
    // Reduce: 合并结果
    const totalSum = mapResults.reduce((acc, val) => acc + val, 0);
    console.log(`   各分片结果: [${mapResults.join(', ')}]`);
    console.log(`   总和 (平方和): ${totalSum}`);
    
    // 验证
    const expected = data.reduce((acc, x) => acc + x * x, 0);
    console.log(`   验证结果: ${expected} (${totalSum === expected ? '正确' : '错误'})`);

    // 6. 健康检查
    console.log('\n6. 健康检查...');
    const healthChecks = await Promise.all(
      sandboxes.map(async (sandbox, i) => {
        try {
          const alive = await sandbox.isAlive();
          return { index: i, alive: alive.isAlive };
        } catch {
          return { index: i, alive: false };
        }
      })
    );
    
    healthChecks.forEach(({ index, alive }) => {
      console.log(`   沙箱 ${index + 1}: ${alive ? '健康' : '异常'}`);
    });

  } finally {
    // 7. 批量关闭
    console.log('\n7. 批量关闭沙箱...');
    await group.stop();
    console.log('   所有沙箱已关闭');
  }
}

// 运行示例
sandboxGroupExample().catch(console.error);
