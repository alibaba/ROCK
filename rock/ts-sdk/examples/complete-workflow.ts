/**
 * 完整工作流示例
 * 
 * 演示一个完整的开发工作流：
 * 1. 创建沙箱
 * 2. 配置环境
 * 3. 部署代码
 * 4. 运行测试
 * 5. 收集结果
 */

import { Sandbox, RunMode, SpeedupType } from '../src';
import * as fs from 'fs';
import * as path from 'path';

// 示例项目代码
const PROJECT_CODE = {
  'main.py': `
import json
from calculator import Calculator

def main():
    calc = Calculator()
    
    results = {
        'add': calc.add(10, 5),
        'subtract': calc.subtract(10, 5),
        'multiply': calc.multiply(10, 5),
        'divide': calc.divide(10, 5),
    }
    
    print(json.dumps(results, indent=2))
    
if __name__ == '__main__':
    main()
`,
  'calculator.py': `
class Calculator:
    """Simple calculator class."""
    
    def add(self, a: float, b: float) -> float:
        return a + b
    
    def subtract(self, a: float, b: float) -> float:
        return a - b
    
    def multiply(self, a: float, b: float) -> float:
        return a * b
    
    def divide(self, a: float, b: float) -> float:
        if b == 0:
            raise ValueError("Cannot divide by zero")
        return a / b
`,
  'test_calculator.py': `
import unittest
from calculator import Calculator

class TestCalculator(unittest.TestCase):
    def setUp(self):
        self.calc = Calculator()
    
    def test_add(self):
        self.assertEqual(self.calc.add(2, 3), 5)
        self.assertEqual(self.calc.add(-1, 1), 0)
    
    def test_subtract(self):
        self.assertEqual(self.calc.subtract(5, 3), 2)
        self.assertEqual(self.calc.subtract(1, 1), 0)
    
    def test_multiply(self):
        self.assertEqual(self.calc.multiply(3, 4), 12)
        self.assertEqual(self.calc.multiply(0, 5), 0)
    
    def test_divide(self):
        self.assertEqual(self.calc.divide(10, 2), 5)
        self.assertAlmostEqual(self.calc.divide(7, 2), 3.5)
    
    def test_divide_by_zero(self):
        with self.assertRaises(ValueError):
            self.calc.divide(1, 0)

if __name__ == '__main__':
    unittest.main(verbosity=2)
`,
  'requirements.txt': `
# No external dependencies for this simple project
`,
};

async function workflowExample() {
  console.log('=== ROCK TypeScript SDK 完整工作流示例 ===\n');
  console.log('演示：创建 Python 项目、部署、测试、运行\n');

  const sandbox = new Sandbox({
    image: 'reg.docker.alibaba-inc.com/yanan/python:3.11',
    baseUrl: process.env.ROCK_BASE_URL || 'http://localhost:8080',
    cluster: 'default',
    memory: '4g',
    cpus: 2,
  });

  try {
    // ==================== 阶段 1: 环境准备 ====================
    console.log('📁 阶段 1: 环境准备');
    console.log('-----------------------------------');

    console.log('  1.1 启动沙箱...');
    await sandbox.start();
    console.log(`      沙箱ID: ${sandbox.getSandboxId()}`);

    console.log('  1.2 配置网络加速...');
    const network = sandbox.getNetwork();
    try {
      await network.speedup(SpeedupType.PIP, 'https://mirrors.aliyun.com/pypi/simple/');
      console.log('      PIP 镜像已配置');
    } catch {
      console.log('      PIP 配置跳过 (可能已在镜像网络)');
    }

    // ==================== 阶段 2: 项目部署 ====================
    console.log('\n📦 阶段 2: 项目部署');
    console.log('-----------------------------------');

    console.log('  2.1 创建项目目录...');
    await sandbox.arun('mkdir -p /workspace/my-project', { mode: RunMode.NORMAL });

    console.log('  2.2 部署项目文件...');
    for (const [filename, content] of Object.entries(PROJECT_CODE)) {
      await sandbox.write_file({
        content: content.trim(),
        path: `/workspace/my-project/${filename}`,
      });
      console.log(`      已部署: ${filename}`);
    }

    console.log('  2.3 验证项目结构...');
    const treeResult = await sandbox.arun('ls -la /workspace/my-project/', {
      mode: RunMode.NORMAL,
    });
    console.log(`      项目文件:\n${treeResult.output.split('\n').map(l => '        ' + l).join('\n')}`);

    // ==================== 阶段 3: 测试运行 ====================
    console.log('\n🧪 阶段 3: 测试运行');
    console.log('-----------------------------------');

    console.log('  3.1 运行单元测试...');
    const testResult = await sandbox.arun(
      'cd /workspace/my-project && python3 test_calculator.py',
      { mode: RunMode.NORMAL, timeout: 60 }
    );
    console.log(`      测试输出:\n${testResult.output.split('\n').slice(0, 15).map(l => '        ' + l).join('\n')}`);
    console.log(`      退出码: ${testResult.exitCode}`);

    // ==================== 阶段 4: 执行应用 ====================
    console.log('\n🚀 阶段 4: 执行应用');
    console.log('-----------------------------------');

    console.log('  4.1 运行主程序...');
    const runResult = await sandbox.arun(
      'cd /workspace/my-project && python3 main.py',
      { mode: RunMode.NORMAL }
    );
    console.log(`      输出:\n${runResult.output.split('\n').map(l => '        ' + l).join('\n')}`);

    // ==================== 阶段 5: 结果收集 ====================
    console.log('\n📊 阶段 5: 结果收集');
    console.log('-----------------------------------');

    // 解析输出结果
    try {
      const outputJson = JSON.parse(runResult.output);
      console.log('  计算结果:');
      console.log(`    加法: 10 + 5 = ${outputJson.add}`);
      console.log(`    减法: 10 - 5 = ${outputJson.subtract}`);
      console.log(`    乘法: 10 * 5 = ${outputJson.multiply}`);
      console.log(`    除法: 10 / 5 = ${outputJson.divide}`);
    } catch {
      console.log('  结果解析失败');
    }

    // ==================== 阶段 6: 资源清理 ====================
    console.log('\n🧹 阶段 6: 资源清理');
    console.log('-----------------------------------');

    console.log('  6.1 清理临时文件...');
    await sandbox.arun('rm -rf /workspace/my-project', { mode: RunMode.NORMAL });
    console.log('      临时文件已清理');

    console.log('  6.2 关闭沙箱...');
    await sandbox.close();
    console.log('      沙箱已关闭');

    console.log('\n✅ 工作流完成！');

  } catch (error) {
    console.error('\n❌ 工作流失败:', error);
    await sandbox.close().catch(() => {});
    process.exit(1);
  }
}

// 运行示例
workflowExample().catch(console.error);
