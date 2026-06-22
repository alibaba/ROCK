# Deploy

沙箱资源部署管理器，用于本地目录部署和模板格式化。

## deploy_working_dir - 部署本地目录

```python
sandbox = Sandbox(config)
deploy = sandbox.deploy

# 部署本地目录到沙箱（自动生成目标路径）
target = await deploy.deploy_working_dir(
    local_path="/path/to/local/project",
)
print(f"部署到: {target}")  # 例如: /tmp/rock_workdir_abc123

# 部署到指定目标路径
target = await deploy.deploy_working_dir(
    local_path="/path/to/local/project",
    target_path="/root/workdir",
)
```

## format - 模板变量替换

`format` 方法支持两种模板语法：

- **`${variable}`** - 标准 Python 字符串模板语法
- **`<<variable>>`** - 替代语法（内部转换为 `${variable}`）

```python
# 使用 ${working_dir} 模板变量
cmd = deploy.format("mv ${working_dir}/config.json /root/.app/")
# 结果: mv /tmp/rock_workdir_abc123/config.json /root/.app/

# 使用 <<>> 替代语法
cmd = deploy.format("cat <<working_dir>>/file.txt")
# 结果: cat /tmp/rock_workdir_abc123/file.txt

# 结合自定义变量使用
cmd = deploy.format(
    "cat ${working_dir}/${config_file}",
    config_file="settings.json"
)
# 结果: cat /tmp/rock_workdir_abc123/settings.json

# Shell 语法保持不变
cmd = deploy.format("echo $((3 << 2 >> 1))")
# 结果: echo $((3 << 2 >> 1))

# 直接访问 working_dir
if deploy.working_dir:
    print(f"当前工作目录: {deploy.working_dir}")
```

## 多次部署

后续调用会覆盖之前的工作目录路径：

```python
# 第一次部署
path1 = await deploy.deploy_working_dir(local_path="/project/v1")
print(deploy.working_dir)  # /tmp/rock_workdir_xxx1

# 第二次部署（覆盖之前的路径）
path2 = await deploy.deploy_working_dir(local_path="/project/v2")
print(deploy.working_dir)  # /tmp/rock_workdir_xxx2
```
