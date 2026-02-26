"""K8S operator constants for labels and annotations."""


class K8sConstants:
    """Constants for K8S BatchSandbox labels and annotations."""
    
    # CRD configuration
    CRD_GROUP = "sandbox.opensandbox.io"
    CRD_VERSION = "v1alpha1"
    CRD_PLURAL = "batchsandboxes"
    CRD_KIND = "BatchSandbox"
    CRD_API_VERSION = f"{CRD_GROUP}/{CRD_VERSION}"  # sandbox.opensandbox.io/v1alpha1
    
    # Annotation keys
    ANNOTATION_ENDPOINTS = "sandbox.opensandbox.io/endpoints"
    ANNOTATION_PORTS = "rock.sandbox/ports"
    
    # Label keys
    LABEL_SANDBOX_ID = "rock.sandbox/sandbox-id"
    LABEL_RESOURCE_SPEEDUP = "batchsandbox.alibabacloud.com/resource-speedup"
    LABEL_TEMPLATE = "rock.sandbox/template"
    
    # Extension keys for DockerDeploymentConfig.extended_params
    EXT_POOL_NAME = "pool_name"
    EXT_TEMPLATE_NAME = "template_name"
