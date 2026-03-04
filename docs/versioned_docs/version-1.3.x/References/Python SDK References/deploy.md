# Deploy

Sandbox resource deployment manager for local directory deployment and template formatting.

## deploy_working_dir - Deploy Local Directory

```python
sandbox = Sandbox(config)
deploy = sandbox.deploy

# Deploy local directory (auto-generated target path)
target = await deploy.deploy_working_dir(
    local_path="/path/to/local/project",
)
print(f"Deployed to: {target}")  # e.g., /tmp/rock_workdir_abc123

# Deploy to specific target path
target = await deploy.deploy_working_dir(
    local_path="/path/to/local/project",
    target_path="/root/workdir",
)
```

## format - Template Variable Substitution

The `format` method supports two template syntaxes:

- **`${variable}`** - Standard Python string template syntax
- **`<<variable>>`** - Alternative syntax (converted to `${variable}` internally)

```python
# After deploy_working_dir, use ${working_dir} placeholder
cmd = deploy.format("mv ${working_dir}/config.json /root/.app/")
# Result: mv /tmp/rock_workdir_abc123/config.json /root/.app/

# Alternative <<>> syntax
cmd = deploy.format("cat <<working_dir>>/file.txt")
# Result: cat /tmp/rock_workdir_abc123/file.txt

# Combine with custom variables
cmd = deploy.format(
    "cat ${working_dir}/${config_file}",
    config_file="settings.json"
)
# Result: cat /tmp/rock_workdir_abc123/settings.json

# Shell syntax is preserved
cmd = deploy.format("echo $((3 << 2 >> 1))")
# Result: echo $((3 << 2 >> 1))

# Access working_dir directly
if deploy.working_dir:
    print(f"Current working directory: {deploy.working_dir}")
```

## Multiple Deployments

Subsequent calls overwrite previous working directory paths:

```python
# First deployment
path1 = await deploy.deploy_working_dir(local_path="/project/v1")
print(deploy.working_dir)  # /tmp/rock_workdir_xxx1

# Second deployment (overwrites previous path)
path2 = await deploy.deploy_working_dir(local_path="/project/v2")
print(deploy.working_dir)  # /tmp/rock_workdir_xxx2
```
