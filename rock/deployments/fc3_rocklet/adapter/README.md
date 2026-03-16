# 方案 C：混合适配层部署

使用 FC3 Python3.10 运行时 + 适配层部署 rocklet。

## 推荐度：⭐⭐⭐

## 优点

- 使用 Python3.10 运行时，冷启动快
- 复用 rocklet 核心代码
- 无需构建 Docker 镜像

## 缺点

- 需要维护适配层代码
- 可能不支持部分 rocklet 功能

## 文件说明

- `server.py` - WSGI 适配层服务器
- `package.sh` - 打包脚本
- `s.yaml` - Serverless Devs 部署配置

## 部署步骤

### 1. 打包代码

```bash
cd rock/deployments/fc3_rocklet/adapter
./package.sh
```

### 2. 部署

```bash
s deploy
```

## 本地测试

```bash
# 启动本地服务器（需要安装 fastapi 和 uvicorn）
cd rock/deployments/fc3_rocklet/adapter
python server.py --port 9000

# 测试
curl http://localhost:9000/is_alive
```

## 适配层 API

适配层支持以下 API，与 rocklet 完全兼容：

| 端点 | 方法 | 说明 |
|------|------|------|
| `/is_alive` | GET | 健康检查 |
| `/create_session` | POST | 创建会话 |
| `/close_session` | POST | 关闭会话 |
| `/run_in_session` | POST | 在会话中执行命令 |
| `/read_file` | POST | 读取文件 |
| `/write_file` | POST | 写入文件 |
| `/execute` | POST | 执行命令（无会话） |
