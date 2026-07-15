class MetricsConstants:
    METRICS_METER_NAME = "XRL_GATEWAY_CONFIG"

    SANDBOX_REQUEST_TOTAL = "request.total"
    SANDBOX_REQUEST_SUCCESS = "request.success"
    SANDBOX_REQUEST_FAILURE = "request.failure"
    SANDBOX_REQUEST_CLIENT_ERROR = "request.client_error"

    SANDBOX_REQUEST_RT = "request.rt"

    SANDBOX_TOTAL_COUNT = "sandbox.count.total"
    SANDBOX_COUNT_IMAGE = "sandbox.count.image"

    SANDBOX_CPU = "system.cpu"
    SANDBOX_MEM = "system.memory"
    SANDBOX_DISK = "system.disk"
    SANDBOX_DISK_LOG = "system.disk.log"
    SANDBOX_DISK_DIND = "system.disk.dind"
    SANDBOX_NET = "system.network"

    TOTAL_CPU_RESOURCE = "resource.cpu.total"
    TOTAL_MEM_RESOURCE = "resource.mem.total"
    AVAILABLE_CPU_RESOURCE = "resource.cpu.available"
    AVAILABLE_MEM_RESOURCE = "resource.mem.available"
    TOTAL_DISK_RESOURCE = "resource.disk.total"
    AVAILABLE_DISK_RESOURCE = "resource.disk.available"
    DISK_OVERCOMMIT_RATIO = "resource.disk.overcommit_ratio"

    SANDBOX_PHASE_FAILURE = "sandbox.phase.failure"

    METASTORE_TOTAL = "meta_store.total"
    METASTORE_SUCCESS = "meta_store.success"
    METASTORE_FAILURE = "meta_store.failure"
    METASTORE_RT = "meta_store.rt"

    METASTORE_DB_TOTAL = "meta_store.db.total"
    METASTORE_DB_SUCCESS = "meta_store.db.success"
    METASTORE_DB_FAILURE = "meta_store.db.failure"
    METASTORE_DB_RT = "meta_store.db.rt"

    SCHEDULER_UP = "scheduler.up"
    SCHEDULER_WORKERS_ALIVE = "scheduler.workers.alive"
    SCHEDULER_WORKER_ALIVE = "scheduler.worker.alive"
    SCHEDULER_WORKER_CACHE_LAST_SUCCESS_TIMESTAMP = "scheduler.worker_cache.last_success.timestamp"
    SCHEDULER_WORKER_CACHE_TTL = "scheduler.worker_cache.ttl"
    SCHEDULER_TASKS_REGISTERED = "scheduler.tasks.registered"
    SCHEDULER_TASK_INTERVAL = "scheduler.task.interval"
    SCHEDULER_WORKER_CACHE_REFRESH_TOTAL = "scheduler.worker_cache.refresh.total"
    SCHEDULER_WORKER_FAILURES_TOTAL = "scheduler.worker.failures.total"
    SCHEDULER_WORKER_LAST_FAILURE_TIMESTAMP = "scheduler.worker.last_failure.timestamp"
