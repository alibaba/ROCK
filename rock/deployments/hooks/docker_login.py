from rock.common.constants import DeploymentHookStep
from rock.deployments.hooks.abstract import DeploymentHook
from rock.logger import init_logger
from rock.utils import DockerUtil, ImageUtil

logger = init_logger(__name__)


class DockerLoginHook(DeploymentHook):
    """Hook that performs Docker registry authentication before pulling images.

    When triggered by the PULLING_IMAGE step, this hook parses the
    registry from the image name and logs in using the provided credentials.
    """

    def __init__(self, image: str, username: str, password: str):
        self._image = image
        self._username = username
        self._password = password

    def on_custom_step(self, message: str):
        if message != DeploymentHookStep.PULLING_IMAGE:
            return

        registry, _ = ImageUtil.parse_registry_and_others(self._image)
        if registry:
            logger.info(f"Authenticating to registry {registry!r} before pulling image")
            DockerUtil.login(registry, self._username, self._password)
        else:
            logger.warning(f"No registry found in image name {self._image!r}, skipping docker login")
