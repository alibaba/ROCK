def dir_archive_key(sandbox_id: str, prefix: str) -> str:
    return f"{prefix}{sandbox_id}.tar.gz"


def image_ref(sandbox_id: str, registry_url: str, namespace: str) -> str:
    return f"{registry_url}/{namespace}/sandbox_archived:{sandbox_id}"
