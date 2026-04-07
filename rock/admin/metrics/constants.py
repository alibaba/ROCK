class MetricsConstants:
    METRICS_METER_NAME = "XRL_GATEWAY_CONFIG"

    SANDBOX_REQUEST_TOTAL = "request.total"
    SANDBOX_REQUEST_SUCCESS = "request.success"
    SANDBOX_REQUEST_FAILURE = "request.failure"

    SANDBOX_REQUEST_RT = "request.rt"

    SANDBOX_TOTAL_COUNT = "sandbox.count.total"
    SANDBOX_COUNT_IMAGE = "sandbox.count.image"

    SANDBOX_CPU = "system.cpu"
    SANDBOX_MEM = "system.memory"
    SANDBOX_DISK = "system.disk"
    SANDBOX_NET = "system.network"

    TOTAL_CPU_RESOURCE = "resource.cpu.total"
    TOTAL_MEM_RESOURCE = "resource.mem.total"
    AVAILABLE_CPU_RESOURCE = "resource.cpu.available"
    AVAILABLE_MEM_RESOURCE = "resource.mem.available"
    WORKER_DISK_DOCKER_DIR_PERCENT = "resource.worker_pod.disk.docker_dir.percent"
    WORKER_DISK_LOG_DIR_PERCENT = "resource.worker_pod.disk.log_dir.percent"

    SANDBOX_PHASE_FAILURE = "sandbox.phase.failure"
