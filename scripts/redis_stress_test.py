"""Redis stress test script — uses RedisProvider from the codebase.

Usage:
    python redis_stress_test.py                          # use defaults
    python redis_stress_test.py --port 6379 --password xxx --concurrency 50
"""

import argparse
import asyncio
import statistics
import sys
import time
from pathlib import Path

# Add project root to path so we can import rock modules
sys.path.insert(0, str(Path(__file__).parent.parent))

from rock.actions.sandbox.sandbox_info import pick_sandbox_info_fields  # noqa: E402
from rock.admin.core.redis_key import alive_sandbox_key, timeout_sandbox_key  # noqa: E402
from rock.utils.providers.redis_provider import RedisProvider  # noqa: E402

# --- defaults from rock-inner-vpc-nt-a-pool.yml ---
DEFAULT_HOST = "r-uf6gof1cr5hs5yneyq.redis.rds.aliyuncs.com"
DEFAULT_PORT = 6379
DEFAULT_PASSWORD = "Admin@gal"


async def run_stress_test(
    host: str,
    port: int,
    password: str,
    concurrency: int,
    iterations: int,
):
    print("=== Redis Stress Test ===")
    print(f"Host: {host}:{port}")
    print(f"Concurrency: {concurrency}, Iterations per worker: {iterations}")
    print()

    # Initialize RedisProvider
    provider = RedisProvider(host=host, port=port, password=password)

    # Verify connectivity
    t0 = time.perf_counter()
    await provider.init_pool()
    print(f"✓ Connection pool initialized ({time.perf_counter() - t0:.3f}s)")

    t0 = time.perf_counter()
    await provider.client.ping()
    print(f"✓ PING OK ({time.perf_counter() - t0:.3f}s)")

    # Warm up: create a few connections (same as meta_store.create)
    for i in range(min(5, concurrency)):
        sandbox_id = f"stress:warmup:{i}"
        sandbox_info = {
            "sandbox_id": sandbox_id,
            "state": "running",
            "memory": "2g",
            "cpus": 1.0,
        }
        redis_payload = pick_sandbox_info_fields(sandbox_info)
        await provider.json_set(alive_sandbox_key(sandbox_id), "$", redis_payload)
        timeout_info = {"auto_clear_time": "120", "expire_time": "2026-06-26T20:00:00+08:00"}
        await provider.json_set(timeout_sandbox_key(sandbox_id), "$", timeout_info)

    print(f"\n--- Starting stress test ({concurrency} workers × {iterations} iters) ---\n")

    all_latencies: list[float] = []
    errors: list[str] = []
    total_start = time.perf_counter()

    async def worker(worker_id: int):
        prefix = f"stress:worker{worker_id}"
        latencies = []
        for i in range(iterations):
            sandbox_id = f"{prefix}:item:{i % 10}"  # reuse 10 keys per worker
            alive_key = alive_sandbox_key(sandbox_id)
            timeout_key = timeout_sandbox_key(sandbox_id)

            # --- EXISTS (same as _check_sandbox_exists_in_redis) ---
            t = time.perf_counter()
            try:
                await provider.exists(alive_key)
                elapsed = time.perf_counter() - t
                latencies.append(elapsed)
            except Exception as e:
                errors.append(f"[w{worker_id}] EXISTS {alive_key}: {e}")
                elapsed = time.perf_counter() - t
                latencies.append(elapsed)

            # --- JSON.SET alive key (same as meta_store.create) ---
            t = time.perf_counter()
            try:
                sandbox_info = {
                    "role": "admin",
                    "env": "inner-vpc-nt-a-pool",
                    "image": "rock-instances-registry-vpc.cn-shanghai.cr.aliyuncs.com/instance/harbor:8e50674fc2",
                    "image_os": "linux",
                    "port": None,
                    "docker_args": [],
                    "startup_timeout": 600.0,
                    "pull": "missing",
                    "python_standalone_dir": None,
                    "platform": None,
                    "remove_container": False,
                    "auto_clear_time_minutes": 120,
                    "memory": "2g",
                    "cpus": 1.0,
                    "limit_cpus": None,
                    "disk_limit_rootfs": None,
                    "container_name": sandbox_id,
                    "auto_delete_seconds": None,
                    "type": "docker",
                    "enable_auto_clear": False,
                    "use_kata_runtime": False,
                    "kata_disk_size": "50G",
                    "kata_disk_base_path": "/data/docker-disk",
                    "actor_resource": "xrl-sandbox",
                    "actor_resource_num": 2,
                    "registry_username": None,
                    "runtime_config": {
                        "enable_auto_clear": False,
                        "project_root": "/distribution/rock-admin/code/OpenSource",
                        "python_env_path": "/root/miniconda3",
                        "envhub_db_url": "sqlite:////root/.rock/rock_envs.db",
                        "operator_type": "ray",
                        "standard_spec": {"memory": "8g", "cpus": 2},
                        "max_allowed_spec": {"memory": "64g", "cpus": 16},
                        "use_standard_spec_only": False,
                        "metrics_endpoint": "https://sunfire-ingestion-external.alibaba-inc.com",
                        "user_defined_tags": {"service.name": "chatos"},
                        "sandbox_disk_limit_rootfs": None,
                        "instance_registry_mirrors": [],
                        "image_os_profiles": {},
                    },
                    "num_gpus": None,
                    "accelerator_type": None,
                    "extended_params": {},
                    "image_os_profile": None,
                    "sandbox_id": sandbox_id,
                    "state": "running",
                    "create_time": "2026-06-25T20:00:00+08:00",
                }
                redis_payload = pick_sandbox_info_fields(sandbox_info)
                await provider.json_set(alive_key, "$", redis_payload)
                elapsed = time.perf_counter() - t
                latencies.append(elapsed)
            except Exception as e:
                errors.append(f"[w{worker_id}] JSON.SET {alive_key}: {e}")
                elapsed = time.perf_counter() - t
                latencies.append(elapsed)

            # --- JSON.SET timeout key (same as meta_store.create with timeout_info) ---
            t = time.perf_counter()
            try:
                timeout_info = {"auto_clear_time": "120", "expire_time": "2026-06-26T20:00:00+08:00"}
                await provider.json_set(timeout_key, "$", timeout_info)
                elapsed = time.perf_counter() - t
                latencies.append(elapsed)
            except Exception as e:
                errors.append(f"[w{worker_id}] JSON.SET {timeout_key}: {e}")
                elapsed = time.perf_counter() - t
                latencies.append(elapsed)

            # --- JSON.GET alive key (same as meta_store.get / build_sandbox_from_redis) ---
            t = time.perf_counter()
            try:
                await provider.json_get(alive_key, "$")
                elapsed = time.perf_counter() - t
                latencies.append(elapsed)
            except Exception as e:
                errors.append(f"[w{worker_id}] JSON.GET {alive_key}: {e}")
                elapsed = time.perf_counter() - t
                latencies.append(elapsed)

        return latencies

    # Run all workers concurrently
    tasks = [worker(i) for i in range(concurrency)]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    total_elapsed = time.perf_counter() - total_start

    # Collect results
    for r in results:
        if isinstance(r, Exception):
            errors.append(f"Worker failed: {r}")
        elif isinstance(r, list):
            all_latencies.extend(r)

    # Report
    print("=== Results ===")
    print(f"Total time: {total_elapsed:.2f}s")
    print(f"Total operations: {len(all_latencies)}")
    print(f"Errors: {len(errors)}")

    if all_latencies:
        sorted_lat = sorted(all_latencies)
        print("\n--- Latency Distribution ---")
        print(f"  Min:    {sorted_lat[0]:.4f}s")
        print(f"  P50:    {sorted_lat[len(sorted_lat) // 2]:.4f}s")
        print(f"  P90:    {sorted_lat[int(len(sorted_lat) * 0.9)]:.4f}s")
        print(f"  P95:    {sorted_lat[int(len(sorted_lat) * 0.95)]:.4f}s")
        print(f"  P99:    {sorted_lat[int(len(sorted_lat) * 0.99)]:.4f}s")
        print(f"  Max:    {sorted_lat[-1]:.4f}s")
        print(f"  Mean:   {statistics.mean(all_latencies):.4f}s")
        print(f"  Stdev:  {statistics.stdev(all_latencies):.4f}s")

        # Flag slow operations (>1s)
        slow = [l for l in all_latencies if l > 1.0]
        if slow:
            print(f"\n⚠ {len(slow)} operations exceeded 1s:")
            for i, l in enumerate(sorted(slow, reverse=True)[:20]):
                print(f"  #{i+1}: {l:.3f}s")

    if errors:
        print(f"\n--- Errors ({len(errors)}) ---")
        for e in errors[:20]:
            print(f"  {e}")
        if len(errors) > 20:
            print(f"  ... and {len(errors) - 20} more")

    # Pool status
    provider.log_pool_detailed_status()

    # Cleanup (delete both alive and timeout keys)
    for i in range(concurrency):
        for j in range(10):
            sandbox_id = f"stress:worker{i}:item:{j}"
            try:
                await provider.json_delete(alive_sandbox_key(sandbox_id))
            except Exception:
                pass
            try:
                await provider.json_delete(timeout_sandbox_key(sandbox_id))
            except Exception:
                pass
    for i in range(min(5, concurrency)):
        sandbox_id = f"stress:warmup:{i}"
        try:
            await provider.json_delete(alive_sandbox_key(sandbox_id))
        except Exception:
            pass
        try:
            await provider.json_delete(timeout_sandbox_key(sandbox_id))
        except Exception:
            pass

    await provider.close_pool()
    print("\n✓ Cleanup done, pool closed.")


def main():
    parser = argparse.ArgumentParser(description="Redis stress test")
    parser.add_argument("--host", default=DEFAULT_HOST, help="Redis host")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Redis port")
    parser.add_argument("--password", default=DEFAULT_PASSWORD, help="Redis password")
    parser.add_argument("--concurrency", type=int, default=20, help="Number of concurrent workers")
    parser.add_argument("--iterations", type=int, default=50, help="Iterations per worker")
    args = parser.parse_args()

    asyncio.run(
        run_stress_test(
            host=args.host,
            port=args.port,
            password=args.password,
            concurrency=args.concurrency,
            iterations=args.iterations,
        )
    )


if __name__ == "__main__":
    main()
