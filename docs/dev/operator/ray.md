# Ray Operator 开发文档

## Ray 临时目录配置 (`temp_dir`)

### 背景

Ray 默认使用 `/tmp/ray/` 作为临时目录，用于存储 session 数据、runtime environment 克隆（virtualenv 复制）等。当 `/tmp` 磁盘空间不足时，sandbox 创建会失败：

```
shutil.Error: [Errno 28] No space left on device
```

典型场景：Ray 在 `_clonevirtualenv.py` 中执行 `shutil.copytree` 将项目的 `.venv` 克隆到 `/tmp/ray/session_*/runtime_resources/pip/*/virtualenv/` 时，`/tmp` 空间耗尽。

### 设计方案

在 `RayConfig` 中新增 `temp_dir` 字段，通过 YAML 配置文件指定 Ray 临时目录，传递给 `ray.init(_temp_dir=...)` 参数。

#### 配置字段

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `temp_dir` | `str \| None` | `None` | Ray 临时数据目录，支持相对路径（自动解析为绝对路径）。`None` 时使用 Ray 默认值 `/tmp/ray` |

#### YAML 配置示例

```yaml
ray:
    runtime_env:
        working_dir: ./
        pip: ./requirements_sandbox_actor.txt
    namespace: "rock-sandbox-test"
    temp_dir: ./tmp/ray          # 相对路径，自动解析为项目绝对路径
```

### 实现

#### 涉及文件

| 文件 | 变更内容 |
|------|---------|
| `rock/config.py` | `RayConfig` 新增 `temp_dir` 字段，`__post_init__` 中自动将相对路径解析为绝对路径 |
| `rock/admin/core/ray_service.py` | `ray.init()` 和重连逻辑中传入 `_temp_dir=self._config.temp_dir` |
| `tests/unit/conftest.py` | 测试 `ray.init()` 中传入 `_temp_dir=ray_config.temp_dir` |
| `rock-conf/rock-test.yml` | 测试环境配置 `temp_dir: ./tmp/ray` |

#### 路径解析

`RayConfig.__post_init__` 使用 `Path.resolve()` 将相对路径转为绝对路径：

```python
def __post_init__(self):
    if self.temp_dir:
        self.temp_dir = str(Path(self.temp_dir).resolve())
```

这是因为 Ray 内部要求 `_temp_dir` 必须是绝对路径，否则抛出 `ValueError("temp_dir must be absolute path or None.")`。

#### ray.init 参数传递

`_temp_dir` 通过 `**kwargs` 传入 `ray.init()`，当值为 `None` 时等价于不传该参数，Ray 使用默认 `/tmp/ray`。

### 影响分析

#### 代码路径影响

| 代码路径 | 是否受影响 | 说明 |
|----------|-----------|------|
| `RayConfig` 数据类 | 是 | 新增字段，所有环境加载配置都经过此类 |
| `RayService.init()` | 是 | admin 服务启动时的 `ray.init()` |
| `RayService._reconnect_ray()` | 是 | Ray 重连时的 `ray.init()` |
| 测试 `conftest.py` | 是 | 单元/集成测试的 `ray.init()` |

#### 各环境实际影响

| 环境 | 配置文件 | 是否配置 `temp_dir` | 实际行为变化 |
|------|---------|-------------------|-------------|
| test | `rock-test.yml` | `./tmp/ray` | Ray 临时目录改为项目下 `tmp/ray/` |
| local | `rock-local.yml` | 未配置 | 无变化，`_temp_dir=None`，仍使用 `/tmp/ray` |
| dev | `rock-dev.yml` | 未配置 | 无变化，`_temp_dir=None`，仍使用 `/tmp/ray` |

#### 向后兼容性

- `temp_dir` 默认为 `None`，传给 `ray.init(_temp_dir=None)` 等价于不传该参数
- 未配置 `temp_dir` 的环境行为完全不变
- 无需修改已有的部署配置

### 运维指南

#### 启用 temp_dir

在对应环境的 YAML 配置文件中添加：

```yaml
ray:
    temp_dir: /data/ray/tmp    # 生产环境建议使用绝对路径，指向大容量磁盘
```

#### 清理旧 Ray 临时数据

```bash
# 查看 /tmp/ray 占用空间
du -sh /tmp/ray/

# 停止 Ray 后清理
ray stop
rm -rf /tmp/ray/session_*

# 重启 Ray
ray start --head
```

#### 注意事项

- `tmp/` 目录已在项目 `.gitignore` 中，不会被提交
- 相对路径基于进程工作目录解析，确保启动 admin 服务时工作目录为项目根目录
- 生产环境建议使用绝对路径，避免工作目录不一致导致的路径错误
