from typing import Literal


class DeprecatedMeta(type):
    def __getattribute__(cls, name):
        # only raise warning for public constants
        if not name.startswith("_") and name.isupper():
            raise DeprecationWarning(f"Constants.{name} is deprecated. Use rock.envs instead")
        return super().__getattribute__(name)


class Constants(metaclass=DeprecatedMeta):
    BASE_URL_PRODUCT = ""
    BASE_URL_ALIYUN = ""
    BASE_URL_INNER = ""
    BASE_URL_PRE = ""
    BASE_URL_LOCAL = ""
    REQUEST_TIMEOUT_SECONDS = 180


RunModeType = Literal["normal", "nohup"]
