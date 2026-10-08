"""End-to-end smoke for runtime launch + action/future + dummy rollout."""
from __future__ import annotations

import httpx

BASE = "http://localhost:9000"


def retrieve_future(request_id: str, timeout: float = 120.0) -> dict:
    response = httpx.post(
        f"{BASE}/api/v1/retrieve_future",
        json={"request_id": request_id},
        timeout=timeout,
    )
    response.raise_for_status()
    return response.json()


def main() -> None:
    session = httpx.post(
        f"{BASE}/api/v1/create_session",
        json={"tags": ["e2e"], "sdk_version": "0.1"},
    )
    session.raise_for_status()
    session_id = session.json()["session_id"]
    print(f"1. session={session_id}")

    runtime_response = httpx.post(
        f"{BASE}/api/v1/create_runtime",
        json={
            "runtime_type": "dummy",
            "config_type": "yaml",
            "config_content": "runtime:\n  launch_script: /root/tinker-dummy/run_dummy.py\n  workdir: /root/tinker-dummy\nbackend:\n  base_url: http://127.0.0.1:9000\n",
            "session_id": session_id,
        },
    )
    runtime_response.raise_for_status()
    runtime_future = runtime_response.json()
    runtime_id = runtime_future["runtime_id"]
    print(f"2. runtime={runtime_id} future={runtime_future['future_id']}")

    runtime_ready = retrieve_future(runtime_future["request_id"])
    assert runtime_ready.get("ready") is True, runtime_ready
    print(f"3. runtime_ready={runtime_ready}")

    init_response = httpx.post(
        f"{BASE}/api/v1/init_task_env",
        headers={"X-Tinker-Backend-Runtime": runtime_id},
        json={
            "instance_id": "dummy_task_001",
            "dataset_name": "dummy",
            "dataset_type": "debug",
        },
    )
    init_response.raise_for_status()
    init_future = init_response.json()
    print(f"4. init_future={init_future['future_id']}")

    init_result = retrieve_future(init_future["request_id"])
    env_id = init_result["env_id"]
    print(f"5. env_id={env_id}")

    step0 = httpx.post(
        f"{BASE}/api/v1/get_step",
        headers={"X-Tinker-Backend-Runtime": runtime_id},
        json={"env_id": env_id, "step_id": 0},
        timeout=120,
    )
    step0.raise_for_status()
    step0_data = step0.json()
    assert step0_data["prompt"]["chunks"][0]["type"] == "encoded_text", step0_data
    print(f"6. step0={step0_data}")

    sample_response = httpx.post(
        f"{BASE}/api/v1/asample",
        headers={"X-Tinker-Backend-Runtime": runtime_id},
        json={
            "env_id": env_id,
            "prompt": step0_data["prompt"],
            "sampling_params": {"max_tokens": 64, "temperature": 0.0},
            "num_samples": 1,
        },
    )
    sample_response.raise_for_status()
    sample_future = sample_response.json()
    print(f"7. sample_future={sample_future['future_id']}")

    sample_result = retrieve_future(sample_future["request_id"])
    assert sample_result["sequences"][0]["stop_reason"] == "stop", sample_result
    assert sample_result["sequences"][0]["tokens"], sample_result
    print(f"8. sample_result={sample_result}")

    terminal = httpx.post(
        f"{BASE}/api/v1/get_step",
        headers={"X-Tinker-Backend-Runtime": runtime_id},
        json={"env_id": env_id, "step_id": 1},
        timeout=120,
    )
    terminal.raise_for_status()
    terminal_data = terminal.json()
    assert terminal_data["finish_reason"] == "finish", terminal_data
    assert terminal_data["reward"] == 1.0, terminal_data
    print(f"9. terminal={terminal_data}")

    print("E2E test PASSED")


if __name__ == "__main__":
    main()
