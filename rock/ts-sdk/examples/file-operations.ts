/**
 * 文件操作示例
 * 
 * 演示文件上传、下载、读写等操作
 */

import { Sandbox, RunMode } from '../src';
import * as fs from 'fs';
import * as path from 'path';

async function fileOperationsExample() {
  console.log('=== ROCK TypeScript SDK 文件操作示例 ===\n');

  const sandbox = new Sandbox({
    image: 'python:3.11',
    baseUrl: process.env.ROCK_BASE_URL || 'http://localhost:8080',
    cluster: 'default',
  });

  try {
    await sandbox.start();
    console.log(`沙箱已启动: ${sandbox.getSandboxId()}\n`);

    // 1. 写入文件
    console.log('1. 写入文件...');
    await sandbox.write_file({
      content: 'Hello, this is a test file created by ROCK SDK!',
      path: '/tmp/test-file.txt',
    });
    console.log('   文件已写入: /tmp/test-file.txt');

    // 2. 读取文件
    console.log('2. 读取文件...');
    const readResult = await sandbox.read_file({
      path: '/tmp/test-file.txt',
    });
    console.log(`   内容: ${readResult.content}`);

    // 3. 创建本地测试文件并上传
    console.log('3. 上传本地文件...');
    const localFile = '/tmp/local-example.txt';
    fs.writeFileSync(localFile, 'This is a local file content.\nSecond line.');
    
    await sandbox.upload({
      sourcePath: localFile,
      targetPath: '/workspace/uploaded-file.txt',
    });
    console.log('   本地文件已上传: /workspace/uploaded-file.txt');

    // 验证上传
    const uploadedContent = await sandbox.read_file({
      path: '/workspace/uploaded-file.txt',
    });
    console.log(`   验证内容: ${uploadedContent.content.substring(0, 30)}...`);

    // 4. 使用命令操作文件
    console.log('4. 使用命令操作文件...');
    
    // 列出目录
    const lsResult = await sandbox.arun('ls -la /workspace/', {
      mode: RunMode.NORMAL,
    });
    console.log(`   目录内容:\n${lsResult.output}`);

    // 创建目录结构
    await sandbox.arun('mkdir -p /workspace/project/src', {
      mode: RunMode.NORMAL,
    });

    // 写入多个文件
    await sandbox.write_file({
      content: 'console.log("Hello from Node.js!");',
      path: '/workspace/project/src/index.js',
    });

    await sandbox.write_file({
      content: JSON.stringify({ name: 'test-project', version: '1.0.0' }, null, 2),
      path: '/workspace/project/package.json',
    });

    // 查看项目结构
    const treeResult = await sandbox.arun('find /workspace/project -type f', {
      mode: RunMode.NORMAL,
    });
    console.log(`   项目结构:\n${treeResult.output}`);

    // 5. 文件权限操作
    console.log('5. 文件权限操作...');
    
    const sandboxFs = sandbox.getFs();
    
    // 修改权限
    await sandboxFs.chmod({
      paths: ['/workspace/project/src/index.js'],
      mode: '755',
      recursive: false,
    });
    console.log('   已修改文件权限为 755');

    // 验证权限
    const permResult = await sandbox.arun('ls -la /workspace/project/src/index.js', {
      mode: RunMode.NORMAL,
    });
    console.log(`   权限: ${permResult.output.trim()}`);

    // 清理本地测试文件
    fs.unlinkSync(localFile);

  } finally {
    console.log('\n关闭沙箱...');
    await sandbox.close();
  }
}

// 运行示例
fileOperationsExample().catch(console.error);
