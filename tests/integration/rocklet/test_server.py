import requests

from tests.integration.conftest import RemoteServer


def test_is_alive(rocklet_remote_server: RemoteServer):
    response = requests.get(
        f"http://127.0.0.1:{rocklet_remote_server.port}/is_alive", headers=rocklet_remote_server.headers
    )
    assert response.json()["is_alive"]


def test_hello_world(rocklet_remote_server: RemoteServer):
    assert (
        requests.get(f"http://127.0.0.1:{rocklet_remote_server.port}/", headers=rocklet_remote_server.headers).json()[
            "message"
        ]
        == "hello world"
    )
