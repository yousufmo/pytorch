# Owner(s): ["module: inductor"]

import functools
from collections import namedtuple
from typing import Callable

from unittest import expectedFailure

import torch
from torch._inductor.test_case import TestCase as InductorTestCase
from torch.nn.attention.templated_attention import _compose, templated_attention
from torch.testing._internal import common_utils
from torch.testing._internal.common_cuda import PLATFORM_SUPPORTS_BF16
from torch.testing._internal.common_utils import requires_cuda


Tolerances = namedtuple("Tolerances", ["atol", "rtol"])


def create_attention(score_mod):
    return functools.partial(templated_attention, score_mod=score_mod)


test_dtypes = (
    [torch.float16, torch.bfloat16, torch.float32]
    if PLATFORM_SUPPORTS_BF16
    else [torch.float16, torch.float32]
)


class TestTemplatedSDPA(InductorTestCase):
    def run_test(self, score_mod: Callable, dtype: torch.dtype = torch.float16):
        sdpa_partial = create_attention(score_mod)
        compiled_sdpa = torch.compile(sdpa_partial)
        q = torch.randn((4, 8, 2048, 64), dtype=dtype, device="cuda")
        k = torch.randn((4, 8, 2048, 64), dtype=dtype, device="cuda")
        v = torch.randn((4, 8, 2048, 64), dtype=dtype, device="cuda")
        ref_out = sdpa_partial(
            q.to(torch.float64), k.to(torch.float64), v.to(torch.float64)
        )
        compiled_out = compiled_sdpa(q, k, v)

        tolerance = (
            Tolerances(atol=5e-3, rtol=5e-3)
            if dtype != torch.float32
            else Tolerances(atol=2e-2, rtol=2e-2)
        )
        torch.testing.assert_close(
            ref_out.to(dtype=torch.float32),
            compiled_out.to(dtype=torch.float32),
            atol=tolerance.atol,
            rtol=tolerance.rtol,
        )

    @requires_cuda
    @common_utils.parametrize("dtype", test_dtypes)
    def test_identity(self, dtype: torch.dtype):
        def score_mod(score, b, h, m, n):
            return score

        self.run_test(score_mod, dtype)

    @requires_cuda
    @common_utils.parametrize("dtype", test_dtypes)
    def test_causal_mask(self, dtype: torch.dtype):
        def score_mod(score, b, h, token_q, token_kv):
            return torch.where(token_q >= token_kv, score, float("-inf"))

        self.run_test(score_mod, dtype)

    @requires_cuda
    @common_utils.parametrize("dtype", test_dtypes)
    def test_rel_bias(self, dtype: torch.dtype):
        def score_mod(score, b, h, m, n):
            return score + (m - n)

        self.run_test(score_mod, dtype)

    @requires_cuda
    @common_utils.parametrize("dtype", test_dtypes)
    def test_alibi_bias(self, dtype: torch.dtype):
        def score_mod(score, b, h, m, n):
            return score + (m - n) * h

        self.run_test(score_mod, dtype)

    @requires_cuda
    @common_utils.parametrize("dtype", test_dtypes)
    def test_rel_causal(self, dtype: torch.dtype):
        def score_mod(score, b, h, m, n):
            return torch.where(m <= n, score + (m - n), float("-inf"))

        self.run_test(score_mod, dtype)

    @requires_cuda
    @common_utils.parametrize("dtype", test_dtypes)
    def test_alibi_causal(self, dtype: torch.dtype):
        def score_mod(score, b, h, m, n):
            return torch.where(m <= n, score + (m - n) * h, float("-inf"))

        self.run_test(score_mod, dtype)

    @requires_cuda
    @common_utils.parametrize("dtype", test_dtypes)
    def test_function_composition(self, dtype: torch.dtype):
        def score_mod_1(score, b, h, m, n):
            return score + (m - n)

        def score_mod_2(score, b, h, m, n):
            return torch.where(m <= n, score, float("-inf"))

        composed_score_mod = _compose(score_mod_1, score_mod_2)

        self.run_test(composed_score_mod, dtype)

    # TODO We are currently not capturing free variables in the closure correctly
    @expectedFailure
    @requires_cuda
    @common_utils.parametrize("dtype", test_dtypes)
    def test_captured_buffers(self, dtype: torch.dtype):
        head_offset = torch.rand(8, device="cuda", dtype=dtype)

        def score_mod(score, b, h, m, n):
            return score + head_offset[h]

        self.run_test(score_mod, dtype)


common_utils.instantiate_parametrized_tests(TestTemplatedSDPA)

if __name__ == "__main__":
    from torch._inductor.test_case import run_tests

    run_tests()
