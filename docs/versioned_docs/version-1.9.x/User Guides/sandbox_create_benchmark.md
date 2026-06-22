# ROCK Sandbox Large-Scale Concurrent Creation Benchmark Report

## 1. Background

In Agentic Reinforcement Learning training and large-scale agent rollout scenarios, **a single training step often needs to spin up thousands of sandboxes concurrently**, so the throughput and latency of sandbox creation directly determine end-to-end training efficiency.

This report benchmarks ROCK Sandbox batch creation at concurrency levels of **1000 / 2000 / 4000 / 8000 / 16000**, to validate ROCK's stability and latency profile under heavy concurrent load.

## 2. Scope & Methodology

- **System under test**: the ROCK Sandbox creation path.
- **Metric**: per-sandbox wall-clock time from issuing the create request until the sandbox reaches the alive (usable) state (seconds).
- **Concurrency levels**: 1000, 2000, 4000, 8000, and 16000 sandboxes created in parallel.
- **Concurrency model**: a **distributed, multi-machine + pure multi-process** driver is used — load is generated from multiple machines simultaneously, and each machine runs every sandbox-creation task in an isolated OS process. This avoids GIL contention, event-loop overhead, and TLS-handshake serialization becoming client-side bottlenecks, so the numbers truly reflect the server-side capacity.

## 3. Results

### 3.1 Sandbox Creation Latency Statistics

| Concurrency | Samples | Success Rate | Min | Max | Avg | P50 | P95 | P99 |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| **1000** | 1000 | 100% | 0.47s | 9.84s | 4.72s | 4.93s | 6.89s | 8.69s |
| **2000** | 2000 | 100% | 0.64s | 11.20s | 4.90s | 4.92s | 9.76s | 10.53s |
| **4000** | 4000 | 100% | 0.93s | 15.51s | 7.16s | 7.05s | 11.26s | 13.34s |
| **8000** | 8000 | 100% | 3.85s | 33.99s | 18.06s | 17.86s | 30.09s | 32.03s |
| **16000** | 16000 | 100% | 2.66s | 63.84s | 37.17s | 39.98s | 56.68s | 60.11s |

> All values are in seconds. **Success rate is 100% (zero failures)** at every concurrency level.
>
> Note: as concurrency grows, ROCK applies rate limiting on the control plane to protect server-side stability, so the observed latency increases accordingly with higher concurrency.

![Sandbox Create Latency Distribution](../../../static/img/sandbox_create_latency_distribution.png)

## 4. Conclusion

The benchmark from 1000 up to 16000 concurrent sandbox creations on ROCK shows:

1. **100% success rate**, validating ROCK's reliability under heavy concurrent load.
2. **Small-scale concurrency (≤ 2000) is essentially queue-free**, with P50 below 5s — comfortably suitable for latency-sensitive training and evaluation scenarios.
3. **At large scale (≥ 4000), latency grows roughly linearly with concurrency**, giving predictable, easy-to-capacity-plan behaviour for sandbox pools.
4. **Even at 16000 concurrency, P99 stays under ~60s**, sufficient to support very large agent rollouts and parallel RL steps.

ROCK is therefore proven capable of supporting **tens of thousands of concurrent sandbox creations**, providing solid environment infrastructure for large-scale agentic RL training.
