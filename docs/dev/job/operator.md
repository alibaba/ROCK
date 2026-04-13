# 分布式算子参考

对比 torch.distributed、Ray Data、Flink 三个框架的算子抽象，分析 Rock Job Operator 的设计参考。

## 1. 算子总览

### 1.1 torch.distributed — 集合通信算子

torch.distributed 定义了三类通信原语，操作对象是 **Tensor**：

| 类别 | 算子 | 输入 | 输出 | 语义 |
|------|------|------|------|------|
| **点对点** | `send(tensor, dst)` | 1 Tensor on src | dst rank 收到 | 发送给指定 rank |
| | `recv(tensor, src)` | 空 Tensor on dst | dst 填充数据 | 从指定 rank 接收 |
| | `isend` / `irecv` | 同上 | 返回 Work (异步) | 异步版本 |
| **一对多** | `broadcast(tensor, src)` | 1 Tensor on src | 所有 rank 拿到相同 Tensor | 广播 |
| | `scatter(output, scatter_list, src)` | src 持有 list[Tensor] | 每个 rank 拿到 1 片 | 拆分分发 |
| **多对一** | `reduce(tensor, dst, op)` | 每个 rank 1 Tensor | dst 拿到聚合结果 | 聚合到 dst |
| | `gather(gather_list, tensor, dst)` | 每个 rank 1 Tensor | dst 拿到 list[Tensor] | 收集到 dst |
| **多对多** | `all_reduce(tensor, op)` | 每个 rank 1 Tensor | 每个 rank 拿到聚合结果 | 聚合 + 广播 |
| | `all_gather(tensor_list, tensor)` | 每个 rank 1 Tensor | 每个 rank 拿到完整 list | 收集 + 广播 |
| | `reduce_scatter(output, input_list, op)` | 每个 rank list[Tensor] | 每个 rank 拿到 1 片聚合 | 聚合 + 拆分 |
| | `all_to_all(output, input)` | 每个 rank N 片 | 每个 rank 从所有 rank 各收 1 片 | 全交换 |
| **同步** | `barrier()` | 无 | 无 | 等所有 rank 到达 |
| **聚合操作** | `ReduceOp` | — | — | SUM, PRODUCT, MIN, MAX, AVG, BAND, BOR, BXOR |

### 1.2 数据流图

```
4 个 Rank, 每个持有一个 Tensor

broadcast (src=0):              scatter (src=0):
  R0:[1,2] ──→ R0:[1,2]          R0:[A,B,C,D] ──→ R0:[A]
  R1:[0,0] ──→ R1:[1,2]                        ──→ R1:[B]
  R2:[0,0] ──→ R2:[1,2]                        ──→ R2:[C]
  R3:[0,0] ──→ R3:[1,2]                        ──→ R3:[D]

reduce (dst=0, SUM):            gather (dst=0):
  R0:[1,0] ──┐                   R0:[A] ──┐
  R1:[0,1] ──┼→ R0:[1,2]         R1:[B] ──┼→ R0:[A,B,C,D]
  R2:[0,1] ──┤                   R2:[C] ──┤
  R3:[0,0] ──┘                   R3:[D] ──┘

all_reduce (SUM):               all_gather:
  R0:[1,0] ──┐   ┌→ R0:[1,2]    R0:[A] ──┐   ┌→ R0:[A,B,C,D]
  R1:[0,1] ──┼───┼→ R1:[1,2]    R1:[B] ──┼───┼→ R1:[A,B,C,D]
  R2:[0,1] ──┤   ├→ R2:[1,2]    R2:[C] ──┤   ├→ R2:[A,B,C,D]
  R3:[0,0] ──┘   └→ R3:[1,2]    R3:[D] ──┘   └→ R3:[A,B,C,D]

reduce_scatter (SUM):            all_to_all:
  R0:[1,2,3,4] ──┐               R0:[a0,a1,a2,a3]     R0:[a0,b0,c0,d0]
  R1:[5,6,7,8] ──┼→ SUM →拆分    R1:[b0,b1,b2,b3]  →  R1:[a1,b1,c1,d1]
  R2:[1,1,1,1] ──┤  [8,10,12,14] R2:[c0,c1,c2,c3]     R2:[a2,b2,c2,d2]
  R3:[1,1,1,1] ──┘  R0:[8,10]    R3:[d0,d1,d2,d3]     R3:[a3,b3,c3,d3]
                     R1:[12,14]
```

## 2. scatter 跨框架对比

scatter 是最核心的"数据分发"算子，三个框架实现差异显著：

### 2.1 输入输出对比

| | 输入 | 拆分方式 | 输出 | 粒度 |
|---|------|---------|------|------|
| **torch** | src rank 持有 `list[Tensor]` (len=world_size) | 按 index 1:1 分发 | 每个 rank 拿到 1 个 Tensor | Tensor 切片 |
| **Ray Data** | 1 个 `Dataset` (内部 N blocks) | `split(K)` 按 block 边界拆 | K 个 `MaterializedDataset` | Block (Arrow Table) |
| **Flink** | 1 个 `DataStream<T>` | `rebalance()` round-robin / `keyBy()` hash | 每个 subtask 收到部分 Record | Record (StreamRecord) |

### 2.2 代码示例

**torch.distributed — 6 个核心算子完整示例 (4 ranks)**

```python
import os
import torch
import torch.distributed as dist

def init_process(rank, size, fn, backend="gloo"):
    os.environ["MASTER_ADDR"] = "127.0.0.1"
    os.environ["MASTER_PORT"] = "29500"
    dist.init_process_group(backend, rank=rank, world_size=size)
    fn(rank, size)
```

**scatter — 一对多拆分**

```python
def do_scatter(rank, size):
    # 签名: dist.scatter(tensor, scatter_list=None, src=0, group=None, async_op=False)
    #   tensor:       接收缓冲区 (每个 rank 上)
    #   scatter_list: 待分发的 tensor 列表 (仅 src rank 提供, len=world_size)
    #   src:          源 rank

    tensor = torch.empty(1)                # 每个 rank 准备空接收缓冲区
    if rank == 0:
        scatter_list = [torch.tensor([float(i + 1)]) for i in range(size)]
        # scatter_list = [tensor([1.]), tensor([2.]), tensor([3.]), tensor([4.])]
        dist.scatter(tensor, scatter_list=scatter_list, src=0)
    else:
        dist.scatter(tensor, scatter_list=[], src=0)

    print(f"[rank {rank}] received: {tensor[0]}")
    # [rank 0] received: 1.0    ← scatter_list[0]
    # [rank 1] received: 2.0    ← scatter_list[1]
    # [rank 2] received: 3.0    ← scatter_list[2]
    # [rank 3] received: 4.0    ← scatter_list[3]
```

**gather — 多对一收集 (scatter 的逆操作)**

```python
def do_gather(rank, size):
    # 签名: dist.gather(tensor, gather_list=None, dst=0, group=None, async_op=False)
    #   tensor:       每个 rank 提供的数据
    #   gather_list:  收集结果 (仅 dst rank 提供, len=world_size)
    #   dst:          目标 rank

    tensor = torch.tensor([float(rank)])    # 每个 rank 持有自己的数据
    if rank == 0:
        gather_list = [torch.empty(1) for _ in range(size)]
        dist.gather(tensor, gather_list=gather_list, dst=0)
        print(f"[rank 0] gathered: {gather_list}")
        # [rank 0] gathered: [tensor([0.]), tensor([1.]), tensor([2.]), tensor([3.])]
    else:
        dist.gather(tensor, gather_list=[], dst=0)
```

**broadcast — 一对多复制**

```python
def do_broadcast(rank, size):
    # 签名: dist.broadcast(tensor, src=0, group=None, async_op=False)
    if rank == 0:
        tensor = torch.tensor([42.0])
    else:
        tensor = torch.empty(1)

    dist.broadcast(tensor, src=0)
    print(f"[rank {rank}] received: {tensor[0]}")
    # 所有 rank 输出: 42.0
```

**reduce — 多对一聚合**

```python
def do_reduce(rank, size):
    # 签名: dist.reduce(tensor, dst=0, op=ReduceOp.SUM, group=None, async_op=False)
    tensor = torch.ones(1)                  # 每个 rank 持有 tensor([1.0])

    dist.reduce(tensor, dst=0, op=dist.ReduceOp.SUM)
    print(f"[rank {rank}] result: {tensor[0]}")
    # [rank 0] result: 4.0    ← 1+1+1+1, 仅 rank 0 有聚合值
    # [rank 1] result: 1.0    ← 未改变
```

**all_reduce — 多对多聚合 (reduce + broadcast)**

```python
def do_all_reduce(rank, size):
    # 签名: dist.all_reduce(tensor, op=ReduceOp.SUM, group=None, async_op=False)
    tensor = torch.ones(1)

    dist.all_reduce(tensor, op=dist.ReduceOp.SUM)
    print(f"[rank {rank}] result: {tensor[0]}")
    # 所有 rank 输出: 4.0    ← 每个 rank 都拿到聚合结果
```

**all_gather — 多对多收集 (gather + broadcast)**

```python
def do_all_gather(rank, size):
    # 签名: dist.all_gather(tensor_list, tensor, group=None, async_op=False)
    tensor = torch.tensor([float(rank)])
    tensor_list = [torch.empty(1) for _ in range(size)]

    dist.all_gather(tensor_list, tensor)
    print(f"[rank {rank}] gathered: {tensor_list}")
    # 所有 rank 输出: [tensor([0.]), tensor([1.]), tensor([2.]), tensor([3.])]
```

**Ray Data split (scatter 等价)**

```python
ds = ray.data.read_csv("big_data.csv")  # 1 Dataset → N blocks

shards = ds.split(n=4, equal=True)
# shards[0]: MaterializedDataset (25% blocks)
# shards[1]: MaterializedDataset (25% blocks)
# ...

workers = [Worker.remote() for _ in range(4)]
ray.get([w.train.remote(s) for w, s in zip(workers, shards)])
```

**Flink rebalance (scatter 等价)**

```java
dataStream
    .rebalance()     // round-robin 逐条分发到所有 subtask
    .map(new MyMapFunction());

dataStream
    .keyBy(row -> row.userId)  // 按 key hash 分发 (相同 key → 同一 subtask)
    .process(new MyProcessFunction());
```

### 2.3 核心差异

| 维度 | torch | Ray Data | Flink |
|------|-------|----------|-------|
| **数据模型** | Tensor (连续内存) | Block (Arrow Table) | Record (流记录) |
| **拆分时机** | 调用时一次性同步 | materialize 时 | 运行时逐条流式 |
| **需要预知总量?** | 是 (world_size) | 否 (按 block 拆) | 否 (流式) |
| **拆分粒度** | Tensor 维度切片 | Block 级别 | Record 级别 |
| **通信方式** | NCCL/Gloo 进程间通信 | Object Store 引用传递 | Network Shuffle |
| **适用场景** | GPU 并行计算 | 数据并行处理 | 流/批处理 |

## 3. map 跨框架对比

| | 用户函数签名 | 输入 | 输出 | 粒度 |
|---|------------|------|------|------|
| **torch** | 无内置 map (手动循环) | Tensor | Tensor | — |
| **Ray Data** | `lambda row: Dict` 或 `Callable(batch) -> batch` | `Dict[str, Any]` (一行) 或 `Dict[str, np.ndarray]` (一批) | 同类型 | 逐行 / 逐批 |
| **Flink** | `MapFunction<T, O>.map(T) -> O` | `T` (一条记录) | `O` (一条记录) | 逐条 |
| **Rock** | `AbstractTask.setup/build/collect` | `JobConfig` (整个任务配置) | `TaskResult` | 逐 sandbox |

### Flink MapFunction 层次

```
MapFunction<T, O>                    ← 用户接口: T → O
    ↓ 包装为
StreamMap(OneInputStreamOperator)    ← Operator: StreamRecord<T> → StreamRecord<O>
    ↓ chain 为
Task (OperatorChain)                 ← 运行时: 多个 Operator 融合成一个 Task
    ↓ 调度到
TaskManager                          ← 执行引擎: 分配 slot 执行
```

### Ray Data map 层次

```
lambda row: dict                     ← 用户函数: dict → dict
    ↓ 包装为
MapTransformer                       ← 函数包装: 行级 → 块级
    ↓ 传入
MapOperator(PhysicalOperator)        ← Operator: RefBundle → RefBundle
    ↓ 提交
_map_task.remote(transformer, blocks) ← ray.remote: 在 worker 执行
    ↓ 调度
Executor                             ← 执行引擎: 驱动调度循环
```

## 4. 全部算子的 scatter + map + gather 分解

大部分集合通信算子可以分解为 scatter + map + gather 的组合：

| 算子 | = | scatter | + map | + gather/reduce |
|------|---|---------|-------|-----------------|
| **map** | | — | f(x) 对每个 item | — |
| **broadcast** | | 复制到所有 rank | — | — |
| **scatter** | | 拆分到各 rank | — | — |
| **gather** | | — | — | 收集到 dst |
| **reduce** | | — | — | 聚合到 dst |
| **all_reduce** | | scatter (隐式) | — | reduce + broadcast |
| **all_gather** | | — | — | gather + broadcast |
| **reduce_scatter** | | scatter | — | reduce |
| **map + gather** | | scatter 数据 | f(x) 对每片 | gather 结果 |

## 5. Rock Job Operator 对应

Rock Job 的执行单元是 sandbox（重量级容器，sandbox 之间隔离不通信），因此只需要部分算子：

### 5.1 已实现

| 算子 | Rock 实现 | 说明 |
|------|----------|------|
| **map** | `MapOperator.apply()` | 同一 task 起 N 个 sandbox 并行 |
| **gather** | `JobExecutor.wait()` | 收集所有 TaskClient 结果 |
| **reduce** | `JobResult._build_result()` | 聚合 TaskResult (score, n_completed 等) |
| **barrier** | `asyncio.gather` | 等所有 sandbox 完成 |
| **broadcast** | `MapOperator(concurrency=N)` | 同一 config 广播到 N 个 sandbox |

### 5.2 未来方向

| 算子 | 场景 | 实现方式 |
|------|------|---------|
| **scatter** | 大数据集拆分到多个 sandbox | `ScatterOperator`: 接收数据集，拆分，每个 sandbox 拿一片 |
| **reduce** (自定义) | 自定义聚合逻辑 (不只是 score 平均) | `ReduceOperator` 或 `JobResult` 自定义 reduce_fn |
| **scatter + map + gather** | 完整分布式数据处理流 | Pipeline 组合多个 Operator |

### 5.3 不适用

| 算子 | 不适用原因 |
|------|----------|
| **all_reduce** | sandbox 之间不共享内存，无法进程间通信 |
| **all_gather** | 同上 |
| **all_to_all** | 同上 |
| **send / recv** | sandbox 之间无点对点通道 |

## 6. ScatterOperator 设计草案 (未来)

如果未来需要框架级 scatter 支持：

```python
class ScatterOperator(Operator):
    """Scatter 算子: 拆分数据，每个 sandbox 拿一片

    类比:
      torch: dist.scatter(output, scatter_list, src=0)
      Ray Data: ds.split(n=4)
      Flink: dataStream.rebalance()
    """

    def __init__(self, data: list[dict[str, str]], concurrency: int | None = None):
        """
        Args:
            data: 待拆分的数据列表，每个 dict 注入为 sandbox env vars
            concurrency: 并发数，默认 = len(data)
        """
        self.data = data
        self.concurrency = concurrency or len(data)

    async def apply(self, config, submit_fn):
        task = _create_task(config)
        sem = asyncio.Semaphore(self.concurrency)

        async def submit_one(shard: dict[str, str]) -> TaskClient:
            async with sem:
                # 为每个 shard 克隆 task，注入 shard 数据到 env
                shard_task = _create_task(config.model_copy(
                    update={"env": {**config.env, **shard}}
                ))
                return await submit_fn(shard_task)

        return await asyncio.gather(*[submit_one(s) for s in self.data])


# 使用
import csv
with open("eval_tasks.csv") as f:
    rows = list(csv.DictReader(f))

job = Job(
    BashJobConfig(script="python eval.py", environment=...),
    operator=ScatterOperator(data=rows, concurrency=8),
)
result = await job.run()
```

scatter + map 的完整流程：

```
ScatterOperator.apply(config, submit_fn)
  │
  ├─ 1. _create_task(config)              # 创建基础 Task
  ├─ 2. 对 data 中每个 shard:
  │     ├─ 克隆 config，注入 shard env     # scatter: 每个 sandbox 拿不同数据
  │     ├─ _create_task(shard_config)      # 创建带 shard 数据的 Task
  │     └─ submit_fn(shard_task)           # map: 启动 sandbox 执行
  └─ 3. asyncio.gather(...)               # barrier: 等所有完成
                                           # gather: 收集结果 → list[TaskClient]
```
