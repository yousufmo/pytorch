import weakref
from typing import Any, cast, Dict, Iterable, List, Optional, Set, Tuple

import typing_extensions

import torch
import torch.nn as nn
from torch.distributed._composable_state import _State
from torch.nn.parallel import DistributedDataParallel

from .contract import _get_registry, contract

_ROOT_MODULE_PREFIX = ""


class _ReplicateState(_State):
    def __init__(self) -> None:
        super().__init__()
        self.module: nn.Module = nn.ParameterList()
        self.has_initialized: bool = False
        self._param_list: nn.ParameterList = nn.ParameterList()
        # TODO(@fegin): this variable is originally create for testing, we
        # should remove this if possible.
        self._param_names: List[str] = []
        self._no_sync: bool = False
        self._init_args: Optional[Tuple[Any, ...]] = None
        self._init_kwargs: Dict[str, Any] = {}
        self._comm_hook_args: List[Any] = []

    def _collect_params(
        self,
        module: nn.Module,
        ignored_modules: Set[nn.Module],
        ignored_params: Set[nn.Parameter],
        prefix: str = _ROOT_MODULE_PREFIX,
    ) -> None:
        # skip if managed by fully_sharded API
        if _is_fully_sharded(module):
            return

        # if a module is ignored, all descendants of the module are ignored.
        if module in ignored_modules:
            return

        recurse_prefix = (
            f"{prefix}." if prefix != _ROOT_MODULE_PREFIX else _ROOT_MODULE_PREFIX
        )

        for n, p in module.named_parameters(recurse=False):
            if p not in ignored_params:
                self._param_list.append(p)
                self._param_names.append(f"{recurse_prefix}{n}")

        for name, child_module in module.named_children():
            self._collect_params(
                child_module,
                ignored_modules,
                ignored_params,
                prefix=f"{recurse_prefix}{name}",
            )

    @torch._dynamo.disable(recursive=True)
    def init(
        self,
        module: nn.Module,
        ignored_modules: Set[nn.Module],
        **kwargs,
    ) -> None:
        if _is_fully_sharded(module):
            raise RuntimeError(
                "Cannot apply `replicate()` on a Module already managed by `fully_shard`"
            )

        if self.has_initialized:
            return

        self.has_initialized = True

        device_mesh = kwargs.get("device_mesh", None)
        self.module = module
        ignored_params = {p for m in ignored_modules for p in m.parameters()}
        self._collect_params(module, ignored_modules, ignored_params)

        if "device_id" in kwargs:
            # replicate() supports a small usability enhancement where
            # user can pass in device_id as a Union[int, torch.device] even for
            # CPU devices so users don't have to change code for CPU/GPU runs.
            # We derive the right device_ids to feed into DDP to support this.
            if kwargs["device_id"] is not None:
                device_id = kwargs["device_id"]
                # Convert to device_ids that DDP expects.
                if isinstance(device_id, torch.device) and device_id.type == "cpu":
                    # CPU modules receive device_ids None
                    kwargs["device_ids"] = None
                else:
                    # GPU modules expect device_ids=[cuda_device]
                    kwargs["device_ids"] = [device_id]
            else:
                kwargs["device_ids"] = None
            kwargs.pop("device_id")

        self._ddp = DistributedDataParallel(self._param_list, **kwargs)
        # Weakref to the DDP instance is currently only used for testing.
        replicate.state(self.module)._ddp_weakref = weakref.ref(self._ddp)

    @torch._dynamo.disable(recursive=True)
    def register_comm_hook(self) -> None:
        for comm_args, comm_kwargs in self._comm_hook_args:
            self._ddp.register_comm_hook(*comm_args, **comm_kwargs)
        self._comm_hook_args.clear()

    def record_init_args(self, *args, **kwargs) -> None:
        self._init_args = args
        self._init_kwargs = kwargs

    def forward_pre_hook(
        self, module: nn.Module, args: Tuple[Any, ...], kwargs: Dict[str, Any]
    ) -> Any:
        if self._init_args or self._init_kwargs:
            self.init(*self._init_args, **self._init_kwargs)
            self.register_comm_hook()
            self._init_args = tuple()
            self._init_kwargs = {}
        self._ddp.require_backward_grad_sync = not self._no_sync
        return self._ddp._pre_forward(*args, **kwargs)

    def forward_post_hook(
        self,
        module: nn.Module,
        input: Tuple[torch.Tensor],
        output: torch.Tensor,
    ) -> torch.Tensor:
        return self._ddp._post_forward(output)


def unimplemented_deepcopy(*args: Any, **kwargs: Any) -> typing_extensions.Never:
    raise AssertionError(
        "FSDP does not support deepcopy. Please use state dict for serialization."
    )


class DDP:
    def __new__(cls, *args, **kwargs):
        """
        Override ``__new__`` to remove the FSDP class and directly construct
        the original class for cases like indexing into a container module.
        """
        # Use index 2 since 0 is the dynamically constructed `FSDP<...>` class
        # and index 1 is the `FSDP` class itself
        orig_cls = cls.__mro__[2]
        self = orig_cls.__new__(orig_cls, *args, **kwargs)
        self.__init__(*args, **kwargs)
        return self

    def set_requires_gradient_sync(self, requires_gradient_sync: bool) -> None:
        """
        Sets if the module should sync gradients. This can be used to implement
        gradient accumulation without communication. For HSDP, this controls
        both reduce-scatter and all-reduce together.

        Args:
            requires_gradient_sync (bool): Whether to reduce gradients for the
                module's parameters.
        """
        replicate.state(self)._no_sync = not requires_gradient_sync

    def register_comm_hook(self, *args, **kwargs) -> None:
        replicate.state(self)._comm_hook_args.append((args, kwargs))


@contract(state_cls=_ReplicateState)
def replicate(
    module: nn.Module,
    ignored_modules: Optional[Iterable[torch.nn.Module]] = None,
    **kwargs,
) -> nn.Module:
    r"""Replicates a module

    Args:
        module (torch.nn.Module): module to replicate

    Example::
        >>> # xdoctest: +REQUIRES(module:torch._C._distributed_c10d)
        >>> module = nn.Linear(3, 3)
        >>> replicate(module)
    """
    torch._C._log_api_usage_once("torch.distributed.replicate")

    # TODO(fegin): using kwargs is not a good idea if we would like to make
    # replicate a formal API to replace DDP.
    if "device_id" in kwargs:
        if not isinstance(kwargs["device_id"], (int, torch.device)):
            raise RuntimeError(
                "Expected device_id to be int or torch.device, "
                f"but got {type(kwargs['device_id'])}"
            )

    if ignored_modules is None:
        ignored_modules = {}
    else:
        ignored_modules = set(ignored_modules)

    state = cast(_ReplicateState, replicate.state(module))
    module.register_forward_pre_hook(state.forward_pre_hook, with_kwargs=True)
    module.register_forward_hook(state.forward_post_hook)  # type: ignore[arg-type]
    device_mesh = kwargs.get("device_mesh", None)
    if device_mesh is not None:
        from torch.distributed.device_mesh import _mesh_resources

        if _mesh_resources.get_parent_mesh(device_mesh) is not None:
            # TODO: This is a temporary work around to enable DDP + TP.
            # We should do the logic in DDP so that the 2D implementation is
            # sound and the state_dict works out of the box.
            #
            # This won't conflict with what is done in DDP class as the module
            # replicate is going to pass is NOT the original module.
            from torch.distributed.tensor.parallel.ddp import _pre_dp_module_transform

            _pre_dp_module_transform(module)
    state.record_init_args(module, ignored_modules, **kwargs)

    # Place FSDP leftmost for highest priority in the method resolution order
    cls = module.__class__
    dct = {"__deepcopy__": unimplemented_deepcopy}
    new_cls = type(f"DDP{cls.__name__}", (DDP, cls), dct)
    module.__class__ = new_cls
    return module


def _is_fully_sharded(module: nn.Module) -> bool:
    r"""Check if module is marked with fully_shard."""
    registry = _get_registry(module)
    if registry is None:
        return False
    return "fully_shard" in registry
