import asyncio
import os
from pathlib import Path

from rock.sdk.sandbox.client import Sandbox
from rock.sdk.sandbox.config import SandboxConfig
from rock.sdk.sandbox.model_service.base import ModelService

MODEL_PAYLOAD = (
    '{"id":"chat-","object":"chat.completion","created":1769156933,"model":"",'
    '"choices":[{"index":0,"finish_reason":"stop","message":{"role":"assistant","content":"Hello! I am ROCK"}}]}'
)


async def _call_llm(request_str: str) -> str:
    """
    Call an LLM endpoint and return the raw response as a string.

    Notes:
        - This function is a template: you may fetch the model response in any way you prefer
          (e.g., by calling the OpenAI API).
        - `request_str` is not used in the current stub. If needed, parse/convert it into the
          request body and send it to the model server.
        - The current implementation is a placeholder for local testing/integration: it returns
          `MODEL_PAYLOAD` directly until a real backend is wired up.

    Args:
        request_str: The request content for the LLM call (often a JSON string or other
            serializable description).

    Returns:
        The raw response text from the model endpoint (in this stub: `MODEL_PAYLOAD`).
    """
    # You can get model response by any way you want, For example, call OpenAI API
    # async with httpx.AsyncClient() as http_client:
    #     http_response = await http_client.post(
    #         url="https://api.openai.com/v1/chat/completions",
    #         headers={"Authorization": f"Bearer YOUR_API_KEY"},
    #         json=json.loads(request_str),
    #     )
    #     return http_response.text
    _ = request_str
    return MODEL_PAYLOAD


async def model_service_loop(model_service: ModelService) -> None:
    """Main loop for Whale ModelService interaction (single fixed payload)."""
    if not model_service:
        raise Exception("ModelService is not initialized")

    index = 0
    response_payload = None

    while True:
        agent_request_json_str = await model_service.anti_call_llm(
            index=index,
            response_payload=response_payload,
        )

        if agent_request_json_str == "SESSION_END":
            break

        response_payload = await _call_llm(agent_request_json_str)

        index += 1


async def main() -> None:
    sandbox = Sandbox(SandboxConfig())

    await sandbox.start()

    await sandbox.agent.install()

    asyncio.create_task(model_service_loop(sandbox.agent.model_service))

    result = await sandbox.agent.run("Hello")

    print(f"[Agent] run finished, result={result}")

    await sandbox.stop()


if __name__ == "__main__":
    os.chdir(Path(__file__).resolve().parent)

    # Ensure admin server is running before executing
    print("IMPORTANT: Make sure the admin server is running before executing this demo!")
    print("Start the admin server with: rock admin start")
    asyncio.run(main())
