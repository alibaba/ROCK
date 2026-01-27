# Deploy

沙箱资源部署管理器，用于本地目录部署和模板格式化。

## 使用示例

```python
sandbox = Sandbox(config)
deploy = sandbox.deploy

# 部署本地目录到沙箱
target = await deploy.deploy_working_dir(
    local_path="/local/path",           # 本地目录
    target_path="/remote/path",         # 可选，目标路径
)

# 使用 ${working_dir} 模板变量
cmd = deploy.format("mv ${working_dir}/config.json /root/.app/")
# 结果: mv /tmp/rock_workdir_xxx/config.json /root/.app/
```
