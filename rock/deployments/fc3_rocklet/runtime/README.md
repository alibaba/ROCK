# 方案 B：自定义运行时部署

使用 FC3 自定义运行时（custom.debian12）部署 rocklet。

## 推荐度：⭐⭐⭐⭐

## ✅ 测试通过

| 测试项 | 结果 | 说明 |
|--------|------|------|
| 健康检查 | ✅ | `/is_alive` 返回 200，耗时 2.4s |
| 创建会话 | ✅ | `create_session` 成功 |
| 执行命令 | ✅ | `run_in_session` 返回正确输出 |
| Python 版本 | ✅ | Python 3.11.2（Debian 12 自带） |

## 优点

- 无需构建 Docker 镜像
- 部署流程简单

## 缺点

- 首次冷启动慢（需要安装依赖）
- 代码包大小受限
- 依赖安装可能失败

## 文件说明

- `bootstrap` - FC3 启动脚本
- `requirements.txt` - Python 依赖
- `package.sh` - 打包脚本
- `s.yaml` - Serverless Devs 部署配置

## 部署步骤

### 1. 打包代码

```bash
cd rock/deployments/fc3_rocklet/runtime
./package.sh
```

### 2. 部署

```bash
s deploy
```

## 注意事项

- 首次冷启动需要安装依赖，耗时较长
- 建议配合 Provisioned Concurrency 使用
- 如果依赖安装失败，请检查网络和磁盘空间
