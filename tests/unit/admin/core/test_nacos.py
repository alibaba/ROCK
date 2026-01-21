import logging
import pytest

from rock.utils.providers.nacos_provider import NacosConfigProvider


@pytest.mark.asyncio
async def test_nacos():
    nacos_provider = NacosConfigProvider(
        endpoint="http://jmenv.tbsite.net:8080/diamond-server/diamond",
        namespace="",
        data_id="daily.chatos.xrl.sandbox.router",
        group="xrl-sandbox",
    )
    nacos_provider.add_listener()
    logging.getLogger("nacos.client").setLevel(logging.WARNING)
    logging.getLogger("do-pulling").setLevel(logging.WARNING)
    logging.getLogger("process-polling-result").setLevel(logging.WARNING)

    config = await nacos_provider.get_config()
    print(type(config))
    print(config)

    config = await nacos_provider.get_config()
    print(type(config))
    print(config)
