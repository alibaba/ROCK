# Tracking Adapter 改进提案：多 Adapter 支持 + 灵活上报

**状态：** 部分实现（改动 1、2 已完成）  
**最后更新：** 2025-06-30

---

## 实施进度

### ✅ 改动 1：支持多 Adapter 并行（已实现）

**状态：** 已完成  
**影响范围：** 零侵入（纯新增 API）

- 新增 `resolve_tracking_adapters()` 函数，返回 `list[TrackingAdapter]`
- 保留原 `resolve_tracking_adapter()` 函数，向后兼容
- 添加完整测试覆盖（5 个测试用例）

### ✅ 改动 2：_report_tracking 支持 fan-out（已实现）

**状态：** 已完成  
**影响范围：** 私有方法内部实现，不影响外部 API

- `Job._report_tracking()` 现在调用所有注册的 adapters
- 每个 adapter 独立 try/except，一个失败不影响其他
- 向后兼容：如果没有 adapters，行为不变（静默跳过）
- 添加完整测试覆盖（3 个测试用例）：
  - 多 adapter 并行调用
  - 部分 adapter 失败时的容错
  - 无 adapter 时的正常流程

### ⏸️ 改动 3：让 Adapter 自己决定上报什么（暂缓）

**状态：** 待实施  
**暂缓原因：** 需要修改 `TrackingAdapter` ABC 的 `report()` 方法签名，属于 breaking change，影响所有现有 adapter 实现

---

## 背景

当前 `rock.sdk.job.adapter.TrackingAdapter` 实现存在两个设计局限：

### 1. 单 Adapter 限制
- `resolve_tracking_adapter()` 只返回第一个成功加载的 adapter
- 多个 adapter 注册时，无法并行工作
- 虽然失败会 fallback 到下一个，但成功的只会返回一个

### 2. 上报粒度由框架决定
- 框架在 `_report_tracking()` 中构建固定的 metrics dict
- 每个 adapter 被迫接收相同的数据：per-trial metrics + job-level summary
- 不同后端需求差异大（OTel 可能只要聚合，实验平台要 per-trial，日志系统只要 status）

## 改进方案

### 改动 1：支持多 Adapter 并行

**文件**：`rock/sdk/job/adapter.py`

**当前**：
```python
def resolve_tracking_adapter() -> TrackingAdapter | None:
    """Returns the first successfully loaded adapter, or None."""
    eps = entry_points(group=_ENTRY_POINT_GROUP)
    for ep in eps:
        try:
            cls = ep.load()
            adapter = cls()
            return adapter  # ← 第一个成功就返回
        except Exception:
            logger.warning(...)
    return None
```

**改进后**：
```python
def resolve_tracking_adapters() -> list[TrackingAdapter]:
    """Returns all successfully loaded adapters (may be empty)."""
    adapters: list[TrackingAdapter] = []
    eps = entry_points(group=_ENTRY_POINT_GROUP)
    for ep in eps:
        try:
            cls = ep.load()
            adapter = cls()
            adapters.append(adapter)  # ← 收集所有成功的
        except Exception:
            logger.warning(...)
    return adapters
```

### 改动 2：_report_tracking 支持 fan-out

**文件**：`rock/sdk/job/api.py`

**当前**：
```python
def _report_tracking(self, result: JobResult) -> None:
    adapter = resolve_tracking_adapter()
    if adapter is None:
        return
    
    try:
        adapter.init(...)
        for i, trial in enumerate(result.trial_results):
            adapter.report({...})  # 强制 per-trial
        adapter.report({...})       # 强制 job-level
    except Exception:
        ...
    finally:
        adapter.close()
```

**改进后**：
```python
def _report_tracking(self, result: JobResult) -> None:
    adapters = resolve_tracking_adapters()
    if not adapters:
        return
    
    config = self._config
    namespace = config.namespace or "rock-namespace"
    experiment_id = config.experiment_id or "rock-experiment"
    job_name = config.job_name or "default"
    
    # 构建 metrics 一次，复用给所有 adapter
    trial_metrics = [...]
    job_metrics = {...}
    
    # 每个 adapter 独立执行，互不干扰
    for adapter in adapters:
        try:
            adapter.init(...)
            for metrics in trial_metrics:
                adapter.report(metrics)
            adapter.report(job_metrics)
        except Exception:  # 一个失败不影响其他
            logger.warning("adapter %s failed", type(adapter).__name__)
        finally:
            adapter.close()
```

### 改动 3：让 Adapter 自己决定上报什么（核心改进）

**文件**：`rock/sdk/job/adapter.py`

**当前 API**：
```python
class TrackingAdapter(abc.ABC):
    @abc.abstractmethod
    def init(self, *, namespace: str, experiment_id: str, job_id: str, config: dict[str, Any]) -> None:
        """config 是扁平化的 metadata dict"""
    
    @abc.abstractmethod
    def report(self, metrics: dict[str, Any]) -> None:
        """metrics 是框架构建好的固定结构"""
```

**改进后 API**：
```python
class TrackingAdapter(abc.ABC):
    @abc.abstractmethod
    def init(self, *, namespace: str, experiment_id: str, job_id: str, config: JobConfig) -> None:
        """接收完整的 JobConfig 实例"""
    
    @abc.abstractmethod
    def report(self, *, job_result: JobResult, job_config: JobConfig) -> None:
        """接收完整的 JobResult + JobConfig，adapter 自己决定提取什么"""
```

**配套改动**（`rock/sdk/job/api.py`）：
```python
def _report_tracking(self, result: JobResult) -> None:
    adapters = resolve_tracking_adapters()
    if not adapters:
        return
    
    config = self._config
    namespace = config.namespace or "rock-namespace"
    experiment_id = config.experiment_id or "rock-experiment"
    job_name = config.job_name or "default"
    
    for adapter in adapters:
        try:
            adapter.init(namespace=namespace, experiment_id=experiment_id, job_id=job_name, config=config)
            adapter.report(job_result=result, job_config=config)  # ← 传递完整对象
        except Exception:
            logger.warning(...)
        finally:
            adapter.close()
```

## 收益

### 1. 多 Backend 并行
- OTel adapter 报聚合指标
- 实验平台 adapter 报 per-trial + metadata
- 日志 adapter 只记 status
- 三者可以同时工作，互不影响

### 2. Adapter 拥有上报决策权
- OTel adapter 实现示例：
  ```python
  def report(self, *, job_result, job_config):
      # 只报聚合指标
      self.meter.record("job.score", job_result.score)
      self.meter.record("job.success_rate", job_result.n_completed / len(job_result.trial_results))
  ```

- 实验平台 adapter 实现示例：
  ```python
  def report(self, *, job_result, job_config):
      # 报每个 trial 的详细信息
      for trial in job_result.trial_results:
          self.platform.log_trial(trial.task_name, trial.score, trial.duration_sec)
  ```

### 3. 容错性增强
- 一个 adapter 失败不影响其他 adapter
- 每个 adapter 有独立的 try/except/finally

### 4. 向后兼容
- 没有 adapter 注册时行为不变（空 list vs None）
- Adapter 接口变化是 breaking change，但这是 private API，影响范围可控

## 测试更新

**文件**：`tests/unit/sdk/job/test_adapter.py`

需要更新的测试：
1. 重命名 `TestResolveTrackingAdapter` → `TestResolveTrackingAdapters`
2. 新增 `test_loads_multiple_adapters()` — 验证多 adapter 并行加载
3. 新增 `test_loads_working_adapters_skips_broken()` — 验证混合场景（部分成功部分失败）
4. 更新 `test_adapter_lifecycle()` — 使用 `JobResult` 和 `JobConfig` 实例而非 dict

## 风险评估

### 低风险
- 改动范围小（3 个文件，约 20 行核心代码）
- 容错性增强（多 adapter 独立运行）
- Lint + format 检查通过

### 需要注意
- **Breaking change**：函数名和签名变化（`resolve_tracking_adapter` → `resolve_tracking_adapters`，`report(metrics)` → `report(job_result, job_config)`）
  - 需确认这是 private API，无外部依赖
- **执行顺序**：多 adapter 按 entry_points 注册顺序串行执行
  - 如需优先级控制，需额外设计
- **性能**：多 adapter 串行，但 tracking 本身应轻量

## 实施建议

1. **优先级**：中（非紧急，但设计更合理）
2. **工作量**：0.5-1 天（含测试更新 + 文档）
3. **验证**：
   - 单元测试全部通过
   - 手动验证多 adapter 场景（mock 2-3 个 adapter 并行工作）
   - 验证一个 adapter 失败时其他 adapter 仍正常上报

## 相关文件

- `rock/sdk/job/adapter.py` — TrackingAdapter ABC + resolve 函数
- `rock/sdk/job/api.py` — Job._report_tracking() 调用方
- `rock/sdk/job/result.py` — JobResult, TrialResult 定义
- `rock/sdk/job/config.py` — JobConfig 定义
- `tests/unit/sdk/job/test_adapter.py` — 单元测试

## 后续扩展（可选）

- **优先级控制**：在 entry_points 注册时指定优先级，按优先级顺序执行
- **并行执行**：用 `asyncio.gather()` 并行执行多个 adapter（需考虑线程安全）
- **Adapter 配置**：允许通过 `JobConfig` 传入 adapter 特定配置（如 OTel endpoint、实验平台 token）
