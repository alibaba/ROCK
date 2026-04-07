"""Example: use SharedInformer to watch BatchSandbox custom resources.

The informer runs a background daemon thread that keeps a local cache
synchronised with the Kubernetes API server.  The main thread is free to
query the cache at any time without worrying about connectivity or retries.
"""

import time

from kubernetes import config
from kubernetes.client import CustomObjectsApi

from rock.utils.k8s.informer import ADDED, DELETED, MODIFIED, SharedInformer

# BatchSandbox CRD configuration
GROUP = "sandbox.opensandbox.io"
VERSION = "v1alpha1"
PLURAL = "batchsandboxes"
NAMESPACE = "rock"


def on_sandbox_added(sandbox):
    """Handle ADDED event for BatchSandbox."""
    metadata = sandbox.get("metadata", {})
    name = metadata.get("name", "unknown")
    print(f"[ADDED]    {name}")


def on_sandbox_modified(sandbox):
    """Handle MODIFIED event for BatchSandbox."""
    metadata = sandbox.get("metadata", {})
    name = metadata.get("name", "unknown")
    print(f"[MODIFIED] {name}")


def on_sandbox_deleted(sandbox):
    """Handle DELETED event for BatchSandbox."""
    metadata = sandbox.get("metadata", {})
    name = metadata.get("name", "unknown")
    print(f"[DELETED]  {name}")


def main():
    config.load_kube_config()

    custom_api = CustomObjectsApi()

    # Create list function compatible with SharedInformer
    # Note: namespace will be passed by SharedInformer via kwargs
    def list_batch_sandboxes(
        namespace: str = NAMESPACE,
        watch: bool = False,
        resource_version: str | None = None,
        timeout_seconds: int | None = None,
        label_selector: str | None = None,
        field_selector: str | None = None,
        **kwargs,
    ):
        return custom_api.list_namespaced_custom_object(
            group=GROUP,
            version=VERSION,
            namespace=namespace,
            plural=PLURAL,
            watch=watch,
            resource_version=resource_version,
            timeout_seconds=timeout_seconds,
            label_selector=label_selector,
            field_selector=field_selector,
            **kwargs,
        )

    informer = SharedInformer(
        list_func=list_batch_sandboxes,
        namespace=NAMESPACE,
        resync_period=60,
    )

    informer.add_event_handler(ADDED, on_sandbox_added)
    informer.add_event_handler(MODIFIED, on_sandbox_modified)
    informer.add_event_handler(DELETED, on_sandbox_deleted)

    informer.start()
    print(f'Informer started. Watching BatchSandbox in "{NAMESPACE}" namespace ...')

    try:
        while True:
            cached = informer.cache.list()
            print(f"Cached BatchSandboxes: {len(cached)}")
            for sandbox in cached:
                name = sandbox.get("metadata", {}).get("name", "unknown")
                print(f"  - {name}")
            time.sleep(10)
    except KeyboardInterrupt:
        pass
    finally:
        informer.stop()
        print("Informer stopped.")


if __name__ == "__main__":
    main()
