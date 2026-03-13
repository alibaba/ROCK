/**
 * Agent 自动化任务示例
 *
 * 演示如何使用 Agent 进行自动化任务编排
 * Agent 会自动管理运行时环境、模型服务和命令执行
 */

import { Sandbox, DefaultAgent, RockAgentConfig, RunMode } from '../src';
import * as path from 'path';

// 示例 Agent 代码（将被上传到沙箱）
const AGENT_CODE = `
import sys
import json

def main():
    prompt = sys.argv[1] if len(sys.argv) > 1 else "Hello"

    result = {
        "status": "success",
        "prompt_received": prompt,
        "message": f"Agent processed: {prompt}"
    }

    print(json.dumps(result, indent=2))

if __name__ == "__main__":
    main()
`;

async function agentExample() {
  console.log('=== ROCK TypeScript SDK Agent 示例 ===\n');

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

    // ==================== 准备工作目录 ====================
    console.log('2. 准备 Agent 工作目录...');
    const workDir = path.join(process.cwd(), 'examples', 'temp-agent-workdir');

    // 创建临时工作目录并写入 Agent 代码
    const fs = await import('fs');
    if (!fs.existsSync(workDir)) {
      fs.mkdirSync(workDir, { recursive: true });
    }
    fs.writeFileSync(path.join(workDir, 'agent.py'), AGENT_CODE);
    console.log(`   工作目录: ${workDir}\n`);

    // ==================== 配置 Agent ====================
    console.log('3. 配置 Agent...');
    const agentConfig: RockAgentConfig = {
      // Agent 标识
      agentType: 'custom-agent',
      agentSession: 'my-agent-session',

      // 工作目录（本地目录会被上传到沙箱）
      workingDir: workDir,

      // 项目路径（沙箱内的路径）
      projectPath: '/workspace/agent-project',
      useDeployWorkingDirAsFallback: true,

      // 运行时环境配置
      runtimeEnvConfig: {
        type: 'python',
        version: '3.11',
        pipPackages: [],
      },

      // 运行命令（{prompt} 会被替换为实际提示）
      runCmd: 'python agent.py {prompt}',
      skipWrapRunCmd: false,

      // 超时配置
      agentInstallTimeout: 600,
      agentRunTimeout: 1800,
      agentRunCheckInterval: 30,

      // 环境变量
      env: {
        AGENT_MODE: 'demo',
      },

      // 预初始化命令
      preInitCmds: [
        { command: 'echo "Pre-init: Setting up environment"', timeoutSeconds: 60 },
      ],

      // 后初始化命令
      postInitCmds: [
        { command: 'echo "Post-init: Environment ready"', timeoutSeconds: 60 },
      ],

      // 模型服务配置（可选）
      modelServiceConfig: null,
    };
    console.log('   Agent 配置完成\n');

    // ==================== 创建并安装 Agent ====================
    console.log('4. 创建 Agent 实例...');
    const agent = new DefaultAgent(sandbox);
    console.log('   Agent 实例已创建\n');

    console.log('5. 安装 Agent（这会安装运行时环境、上传工作目录）...');
    await agent.install(agentConfig);
    console.log('   Agent 安装完成\n');

    // ==================== 运行 Agent ====================
    console.log('6. 运行 Agent...');
    const result = await agent.run('Please analyze the codebase');
    console.log('   Agent 执行结果:');
    console.log(`   退出码: ${result.exitCode}`);
    console.log(`   输出: ${result.output.trim()}\n`);

    // ==================== 再次运行 ====================
    console.log('7. 再次运行 Agent（复用已安装的环境）...');
    const result2 = await agent.run('Generate a summary');
    console.log('   Agent 执行结果:');
    console.log(`   退出码: ${result2.exitCode}`);
    console.log(`   输出: ${result2.output.trim()}\n`);

    // ==================== 清理 ====================
    console.log('8. 清理资源...');
    await sandbox.close();

    // 清理临时目录
    fs.rmSync(workDir, { recursive: true, force: true });
    console.log('   临时目录已清理');
    console.log('   沙箱已关闭');

    console.log('\n✅ Agent 示例完成！');

  } catch (error) {
    console.error('\n❌ 示例失败:', error);
    await sandbox.close().catch(() => {});
    process.exit(1);
  }
}

// 运行示例
agentExample().catch(console.error);
