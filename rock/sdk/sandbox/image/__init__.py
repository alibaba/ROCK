"""Image-related types for declarative `Image.from_dockerfile()` sandbox creation."""

# SandboxConfig.image is typed `str | Image`; sandbox/config.py only forward-references
# Image (to avoid a circular import). Re-resolve the field types now that Image is
# importable, with Image explicitly available in the rebuild namespace.
from rock.sdk.sandbox.config import SandboxConfig  # noqa: E402
from rock.sdk.sandbox.image.config import BuilderConfig, BuildSpec, ImageRegistry
from rock.sdk.sandbox.image.image import Image
from rock.sdk.sandbox.image.image_builder import ImageBuilder

SandboxConfig.model_rebuild(_types_namespace={"Image": Image})


__all__ = ["BuildSpec", "BuilderConfig", "Image", "ImageBuilder", "ImageRegistry"]
