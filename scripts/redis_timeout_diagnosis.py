"""Redis timeout diagnosis script — traces every step of a Redis operation."""

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import redis.asyncio as aioredis  # noqa: E402


async def diagnose_timeout():
    """Diagnose where the 25s timeout is happening."""
    print("=== Redis Timeout Diagnosis ===\n")

    # Create connection with production config
    pool = aioredis.ConnectionPool(
        host="r-uf6gof1cr5hs5yneyq.redis.rds.aliyuncs.com",
        port=6379,
        password="Admin@gal",
        decode_responses=True,
        max_connections=10,
        health_check_interval=30,
        socket_connect_timeout=5,
        socket_timeout=5,
    )

    client = aioredis.Redis(connection_pool=pool)

    # Step 1: Initial connection
    t0 = time.perf_counter()
    print(f"[{t0:.3f}] Step 1: Calling ping() to establish connection...")
    await client.ping()
    t1 = time.perf_counter()
    print(f"[{t1:.3f}] ✓ Initial ping took {t1 - t0:.3f}s\n")

    # Step 2: Simple SET operation
    t0 = time.perf_counter()
    print(f"[{t0:.3f}] Step 2: Calling set('test_key', 'value')...")
    await client.set("test_key", "value")
    t1 = time.perf_counter()
    print(f"[{t1:.3f}] ✓ SET took {t1 - t0:.3f}s\n")

    # Step 3: Simple GET operation
    t0 = time.perf_counter()
    print(f"[{t0:.3f}] Step 3: Calling get('test_key')...")
    result = await client.get("test_key")
    t1 = time.perf_counter()
    print(f"[{t1:.3f}] ✓ GET took {t1 - t0:.3f}s, result={result}\n")

    # Step 4: Wait and check health_check_interval behavior
    print(f"[{time.perf_counter():.3f}] Step 4: Waiting 35 seconds (health_check_interval=30)...")
    for i in range(35):
        await asyncio.sleep(1)
        if (i + 1) % 5 == 0:
            print(f"  Waited {i + 1}s...")
    print(f"[{time.perf_counter():.3f}] ✓ Wait complete\n")

    # Step 5: Operation after health check interval
    t0 = time.perf_counter()
    print(f"[{t0:.3f}] Step 5: Calling get('test_key') after 35s wait (should trigger health check)...")
    try:
        result = await client.get("test_key")
        t1 = time.perf_counter()
        print(f"[{t1:.3f}] ✓ GET after wait took {t1 - t0:.3f}s, result={result}\n")
    except Exception as e:
        t1 = time.perf_counter()
        print(f"[{t1:.3f}] ✗ GET failed after {t1 - t0:.3f}s: {e}\n")

    # Step 6: Get connection directly and trace operations
    t0 = time.perf_counter()
    print(f"[{t0:.3f}] Step 6: Getting connection directly from pool...")
    conn = await pool.get_connection("GET")
    t1 = time.perf_counter()
    print(f"[{t1:.3f}] ✓ get_connection took {t1 - t0:.3f}s")
    print(f"  Connection is_connected: {conn.is_connected}")
    print(
        f"  Connection next_health_check: {conn.next_health_check if hasattr(conn, 'next_health_check') else 'N/A'}\n"
    )

    # Step 7: Send command with the connection
    t0 = time.perf_counter()
    print(f"[{t0:.3f}] Step 7: Sending GET command with existing connection...")
    await conn.send_command("GET", "test_key")
    t1 = time.perf_counter()
    print(f"[{t1:.3f}] ✓ send_command took {t1 - t0:.3f}s\n")

    # Step 8: Read response
    t0 = time.perf_counter()
    print(f"[{t0:.3f}] Step 8: Reading response...")
    response = await conn.read_response()
    t1 = time.perf_counter()
    print(f"[{t1:.3f}] ✓ read_response took {t1 - t0:.3f}s, result={response}\n")

    # Step 9: Release connection
    await pool.release(conn)
    print(f"[{time.perf_counter():.3f}] Step 9: Connection released\n")

    # Cleanup
    await client.delete("test_key")
    await client.aclose()

    print("=== Diagnosis Complete ===")
    print("\nIf all operations complete quickly, the timeout issue is not in the Redis client itself.")
    print("Check:")
    print("  1. Network latency between pods")
    print("  2. Redis server load (SLOWLOG, INFO)")
    print("  3. Event loop blocking (py-spy showed SSL context creation)")
    print("  4. Connection pool exhaustion under high concurrency")


if __name__ == "__main__":
    asyncio.run(diagnose_timeout())
