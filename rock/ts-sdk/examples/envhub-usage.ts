/**
 * EnvHub 使用示例
 * 
 * 演示环境注册和管理
 */

import { EnvHubClient, EnvHubClientConfig } from '../src';

async function envHubExample() {
  console.log('=== ROCK TypeScript SDK EnvHub 示例 ===\n');

  // 创建 EnvHub 客户端
  const config: EnvHubClientConfig = {
    baseUrl: process.env.ROCK_ENVHUB_BASE_URL || 'http://localhost:8081',
  };

  const client = new EnvHubClient(config);

  try {
    // 1. 健康检查
    console.log('1. 检查 EnvHub 服务状态...');
    const health = await client.healthCheck();
    console.log(`   服务状态: ${JSON.stringify(health)}`);

    // 2. 注册环境
    console.log('2. 注册新环境...');
    const envInfo = await client.register({
      envName: 'my-python-env',
      image: 'python:3.11-slim',
      owner: 'developer',
      description: 'My Python development environment',
      tags: ['python', 'development', 'ml'],
      extraSpec: {
        pythonVersion: '3.11',
        packages: ['numpy', 'pandas', 'scikit-learn'],
      },
    });
    console.log(`   环境已注册: ${envInfo.envName}`);
    console.log(`   镜像: ${envInfo.image}`);

    // 3. 获取环境信息
    console.log('3. 获取环境信息...');
    const fetchedEnv = await client.getEnv('my-python-env');
    console.log(`   名称: ${fetchedEnv.envName}`);
    console.log(`   描述: ${fetchedEnv.description}`);
    console.log(`   标签: ${fetchedEnv.tags?.join(', ') || '无'}`);

    // 4. 列出所有环境
    console.log('4. 列出所有环境...');
    const allEnvs = await client.listEnvs();
    console.log(`   共 ${allEnvs.length} 个环境:`);
    allEnvs.forEach((env, i) => {
      console.log(`   ${i + 1}. ${env.envName} (${env.image})`);
    });

    // 5. 按标签筛选
    console.log('5. 按标签筛选环境...');
    const pythonEnvs = await client.listEnvs({
      tags: ['python'],
    });
    console.log(`   带 'python' 标签的环境: ${pythonEnvs.length} 个`);

    // 6. 按所有者筛选
    console.log('6. 按所有者筛选环境...');
    const myEnvs = await client.listEnvs({
      owner: 'developer',
    });
    console.log(`   所有者为 'developer' 的环境: ${myEnvs.length} 个`);

    // 7. 删除环境
    console.log('7. 删除环境...');
    const deleted = await client.deleteEnv('my-python-env');
    console.log(`   删除结果: ${deleted ? '成功' : '环境不存在'}`);

    // 验证删除
    try {
      await client.getEnv('my-python-env');
      console.log('   警告: 环境仍然存在');
    } catch (e) {
      console.log('   确认: 环境已被删除');
    }

  } catch (error) {
    console.error('操作失败:', error);
    
    // 检查是否是服务不可用
    if (error instanceof Error && error.message.includes('Failed to')) {
      console.log('\n提示: 请确保 EnvHub 服务正在运行:');
      console.log('  - 检查 ROCK_ENVHUB_BASE_URL 环境变量');
      console.log('  - 确认服务端口是否正确');
    }
  }
}

// 运行示例
envHubExample().catch(console.error);
