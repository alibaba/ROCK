# 方案 A：自定义容器部署（推荐）

使用 Docker 自定义容器部署 rocklet 到 FC3。

## 推荐度：⭐⭐⭐⭐⭐

## 优点

- 100% 代码复用，无需修改 rocklet
- 环境一致性最好
- 支持 GEM 游戏环境
- 镜像加速可优化冷启动

## 文件说明

- `Dockerfile` - Docker 镜像构建文件
- `s.yaml` - Serverless Devs 部署配置

## 部署步骤

### 1. 构建镜像

```bash
# 在项目根目录执行
cd /path/to/ROCK
docker build -t rock-rocklet:latest -f rock/deployments/fc3_rocklet/container/Dockerfile .
```

### 2. 推送到 ACR

```bash
# 替换为你的 ACR 地址
docker tag rock-rocklet:latest registry.cn-hangzhou.aliyuncs.com/your-namespace/rock-rocklet:latest
docker push registry.cn-hangzhou.aliyuncs.com/your-namespace/rock-rocklet:latest
```

### 3. 修改配置

编辑 `s.yaml`，修改 `containerImage` 为你的镜像地址。

### 4. 部署

```bash
cd rock/deployments/fc3_rocklet/container
s deploy
```

## 本地测试

```bash
# 构建并运行容器
docker build -t rock-rocklet:latest -f rock/deployments/fc3_rocklet/container/Dockerfile .
docker run -p 9000:9000 rock-rocklet:latest

# 测试
curl http://localhost:9000/is_alive
```
