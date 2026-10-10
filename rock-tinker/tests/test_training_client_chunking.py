from __future__ import annotations

import json

from tinker import types
from tinker.lib.internal_client_holder import InternalClientHolder
from tinker.lib.public_interfaces.training_client import (
    MAX_CHUNK_BYTES_COUNT,
    TrainingClient,
)


class _EstimateHolder:
    estimate_bytes_count_in_chunk = InternalClientHolder.estimate_bytes_count_in_chunk
    estimate_bytes_count_in_model_input = (
        InternalClientHolder.estimate_bytes_count_in_model_input
    )


class _ChunkingClient(TrainingClient):
    def __init__(self) -> None:
        self.holder = _EstimateHolder()


def _long_training_datum() -> types.Datum:
    return types.Datum(
        model_input=types.ModelInput.from_ints([151643] * 50_000),
        loss_fn_inputs={
            "target_tokens": types.TensorData(
                data=[151643] * 2_000,
                dtype="int64",
                shape=[2_000],
            ),
            "weights": types.TensorData(
                data=[0.12345678901234568] * 2_000,
                dtype="float32",
                shape=[2_000],
            ),
        },
    )


def _serialized_request_size(data: list[types.Datum], seq_id: int) -> int:
    request = types.ForwardBackwardRequest(
        forward_backward_input=types.ForwardBackwardInput(
            data=data,
            loss_fn="cross_entropy",
            loss_fn_config=None,
        ),
        model_id="model-chunk-test",
        seq_id=seq_id,
    )
    payload = request.model_dump(exclude_unset=False, exclude_none=True, mode="json")
    return len(json.dumps(payload, separators=(",", ":")).encode())


def test_long_forward_backward_batch_is_split_before_five_megabytes() -> None:
    client = _ChunkingClient()
    datum = _long_training_datum()
    data = [datum] * 12

    datum_estimate = client._estimate_bytes_count(datum)
    chunks = list(client._chunked_requests_generator(data))
    chunk_estimates = [
        sum(client._estimate_bytes_count(item) for item in chunk) for chunk in chunks
    ]
    serialized_sizes = [
        _serialized_request_size(chunk, seq_id=index + 1)
        for index, chunk in enumerate(chunks)
    ]

    assert datum_estimate == 540_000
    assert datum_estimate * len(data) == 6_480_000
    assert [len(chunk) for chunk in chunks] == [9, 3]
    assert chunk_estimates == [4_860_000, 1_620_000]
    assert all(size <= MAX_CHUNK_BYTES_COUNT for size in chunk_estimates)
    assert serialized_sizes[0] > 1024 * 1024
    assert all(size < 40 * 1024 * 1024 for size in serialized_sizes)
