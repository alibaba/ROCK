from rock.config import ArchiveAcrConfig, ArchiveDirStorageConfig
from rock.sandbox.archive.oss_storage import OssDirStorage
from rock.sandbox.archive.registry_v2 import DockerRegistryV2ImageStorage
from rock.sandbox.archive.s3_storage import S3DirStorage


def make_dir_storage_from_config(dir_storage_cfg: ArchiveDirStorageConfig):
    kwargs = dict(
        endpoint=dir_storage_cfg.endpoint,
        bucket=dir_storage_cfg.bucket,
        access_key_id=dir_storage_cfg.access_key_id,
        access_key_secret=dir_storage_cfg.access_key_secret,
        region=dir_storage_cfg.region,
    )
    if dir_storage_cfg.type == "s3":
        return S3DirStorage(**kwargs)
    return OssDirStorage(**kwargs)


def make_image_storage_from_config(acr_cfg: ArchiveAcrConfig) -> DockerRegistryV2ImageStorage:
    return DockerRegistryV2ImageStorage(
        registry_url=acr_cfg.registry_url,
        username=acr_cfg.username,
        password=acr_cfg.password,
    )
