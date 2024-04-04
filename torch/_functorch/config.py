# Copyright (c) Facebook, Inc. and its affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

"""
Global flags for aot autograd
"""
import os
import sys
from typing import TYPE_CHECKING

# Converts torch rng ops to their functional philox rng equivalents. Note that
# we functionalize only CUDA rng ops today.
functionalize_rng_ops = False

# can be useful for debugging if we are incorrectly creating meta fake tensors
fake_tensor_allow_meta = os.environ.get("FAKE_ALLOW_META", True)

# Enables optional asserts in hotpath code to check for errors.  If
# you are seeing weird accuracy problems, try turning this on.
# This is currently off by default as it will harm tracing time,
# but it is on by default for aot_eager.
debug_assert = False

debug_partitioner = os.environ.get("AOT_PARTITIONER_DEBUG", False)

static_weight_shapes = True

# Applies CSE to the graph before partitioning
cse = True

# Restricts the amount of computation AOTAutograd can do.
# NB: We have essentially disabled this heuristic now. However, this is kept
# here for now in case it's useful. Setting it low can artificially reduce the
# amount of recomputation AOTAutograd performs, although not in any kind of
# principled way.
max_dist_from_bw = 1000


# Bans recomputation of nodes that are reading from nodes that is far before
# the current node
ban_recompute_used_far_apart = True
# Breaks up long chain of fusible ops, as otherwise we can have an arbitrarily
# long chain of recomputation in the backwards pass.
ban_recompute_long_fusible_chains = True
# Bans recomputation of nodes that must be materialized in the backwards pass
# (used by a non-fusible node)
ban_recompute_materialized_backward = True
# Chooses to ban recomputation of nodes based off an allowlist. Setting it to
# False changes it to use a denylist. Main change is on operators like
# sort/pool/stuff that isn't cheap enough to be fusible for free but also isn't
# that expensive
ban_recompute_not_in_allowlist = True
# Chooses to ban recomputation of reductions. This is generally a good idea, as
# the result of reductions is generally very small but recomputing reductions in
# a fusion can be expensive.
ban_recompute_reductions = True


# Sets all of the ban_recompute heuristics to False except ban_recompute_reductions
# Generally, this will probably result in some memory improvement, but at the
# cost of some performance
aggressive_recomputation = False

# Unlifts effect tokens from the inputs/outputs in the traced graph and instead
# inserts make_token/sink_token calls in the graph to create tokens and then
# sink them at the end.
unlift_effect_tokens = False

if TYPE_CHECKING:
    from torch.utils._config_typing import *  # noqa: F401, F403

from torch.utils._config_module import install_config_module

# adds patch, save_config, invalid config checks, etc
install_config_module(sys.modules[__name__])
