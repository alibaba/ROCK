from __future__ import annotations

import torch

import tinker
from tinker_cookbook.tinker_backend_cookbook.train_swe_bench import (
    select_training_examples,
)


def _datum(prompt_len: int, target_len: int) -> tinker.types.Datum:
    return tinker.types.Datum(
        model_input=tinker.types.ModelInput.from_ints(list(range(prompt_len))),
        loss_fn_inputs={"target_tokens": list(range(target_len))},
    )


def test_select_training_examples_honors_count_and_token_budget():
    examples = [_datum(10, 2), _datum(20, 3), _datum(30, 4)]
    old_logprobs = [torch.zeros(2), torch.zeros(3), torch.zeros(4)]
    advantages = [torch.ones(2), torch.ones(3), torch.ones(4)]

    selected, selected_old, selected_adv = select_training_examples(
        examples,
        old_logprobs,
        advantages,
        max_train_transitions=2,
        max_train_total_tokens=40,
    )

    assert selected == examples[:2]
    assert selected_old == old_logprobs[:2]
    assert selected_adv == advantages[:2]


def test_select_training_examples_keeps_at_least_one_over_budget_example():
    examples = [_datum(100, 5), _datum(1, 1)]
    old_logprobs = [torch.zeros(5), torch.zeros(1)]
    advantages = [torch.ones(5), torch.ones(1)]

    selected, selected_old, selected_adv = select_training_examples(
        examples,
        old_logprobs,
        advantages,
        max_train_total_tokens=10,
    )

    assert selected == examples[:1]
    assert selected_old == old_logprobs[:1]
    assert selected_adv == advantages[:1]
