# Copyright (c) Meta Platforms, Inc. and affiliates
from typing import Dict, Union

import torch
import torch.distributed._tensor.random as random
import torch.nn as nn
from torch.distributed._tensor import (
    DeviceMesh,
)
from torch.distributed._tensor.random import (
    is_rng_supported_mesh,
    TensorParallelRNGTracker,
)
from torch.distributed.tensor.parallel._utils import _validate_tp_mesh_dim
from torch.distributed.tensor.parallel.style import (
    ParallelStyle,
)


__all__ = [
    "parallelize_module",
]


def parallelize_module(  # type: ignore[return]
    module: nn.Module,
    device_mesh: DeviceMesh,
    parallelize_plan: Union[ParallelStyle, Dict[str, ParallelStyle]],
) -> nn.Module:
    """
    Apply Tensor Parallelism in PyTorch by parallelizing modules or sub-modules based on a user-specified plan.

    We parallelize module or sub_modules based on a parallelize_plan. The parallelize_plan contains
    :class:`ParallelStyle`, which indicates how user wants the module or sub_module
    to be parallelized.

    User can also specify different parallel style per module fully qualified name (FQN).

    Note that ``parallelize_module`` only accepts a 1-D :class:`DeviceMesh`, if you have a 2-D or N-D :class:`DeviceMesh`,
    slice the DeviceMesh to a 1-D sub DeviceMesh first then pass to this API(i.e. ``device_mesh[\"tp\"]``)

    Args:
        module (:class:`nn.Module`):
            Module to be parallelized.
        device_mesh (:class:`DeviceMesh`):
            Object which describes the mesh topology
            of devices for the DTensor.
        parallelize_plan (Union[:class:`ParallelStyle`, Dict[str, :class:`ParallelStyle`]]):
            The plan used to parallelize the module. It can be either a
            :class:`ParallelStyle` object which contains how
            we prepare input/output for Tensor Parallelism or it can be a
            dict of module FQN and its corresponding :class:`ParallelStyle` object.
    Return:
        A :class:`nn.Module` object parallelized.

    Example::
        >>> # xdoctest: +SKIP("distributed")
        >>> from torch.distributed.tensor.parallel import parallelize_module, ColwiseParallel
        >>> from torch.distributed.device_mesh import init_device_mesh
        >>>
        >>> # Define the module.
        >>> m = Model(...)
        >>> tp_mesh = init_device_mesh("cuda", (8,))
        >>> m = parallelize_module(m, tp_mesh, {"w1": ColwiseParallel(), "w2": RowwiseParallel()})
        >>>

    .. note:: For complex module architecture like Attention, MLP layers, we recommend composing
        different ParallelStyles together (i.e. ``ColwiseParallel`` and ``RowwiseParallel``) and pass
        as a parallelize_plan, to achieves the desired sharding computation.
    """
    torch._C._log_api_usage_once("torch.distributed.tensor.parallel.parallelize_module")

    _validate_tp_mesh_dim(device_mesh)

    # instantiate a TP RNG state tracker if it's not there
    if is_rng_supported_mesh(device_mesh) and not isinstance(
        random._rng_tracker, TensorParallelRNGTracker
    ):
        random._rng_tracker = TensorParallelRNGTracker(device_mesh.device_type)
        # TODO: we should allow user to pass in the default seed from a config
        random._rng_tracker._manual_seed(device_mesh, base_seed=1234)
        # By default we execute random ops in non-tensor-parallel region. If users want
        # to execute in tensor-parallel region, they can manually set this field to True
        # after parallelizing the model.
        random._rng_tracker.distribute_region_enabled = False

    if isinstance(parallelize_plan, ParallelStyle):
        return parallelize_plan._apply(module, device_mesh)
    elif isinstance(parallelize_plan, dict):
        for module_path, parallelize_style in parallelize_plan.items():
            parent_module = leaf_module = module
            path_splits = module_path.split(".")
            if len(path_splits) == 0:
                raise ValueError(
                    "Expect module path to be non-empty, but got empty string!"
                )
            atom: str = ""
            while path_splits:
                atom = path_splits.pop(0)
                if atom == "*":
                    # Rest of the path after "*"
                    leaf_path = ".".join(path_splits)
                    # recursively apply the plan to all submodules
                    for submodule in parent_module.children():  # corresponds to "*"
                        if leaf_path:
                            # we haven't reached the leaf, apply in dict style
                            parallelize_module(submodule, device_mesh, {leaf_path: parallelize_style})
                        else:
                            # otherwise, directly apply style
                            parallelize_module(submodule, device_mesh, parallelize_style)
                else:
                    # proceed in depth
                    parent_module = leaf_module
                    try:
                        leaf_module = leaf_module.get_submodule(atom)
                    except AttributeError:
                        # No match for child string. For example, `*.lin` will
                        # apply to all children at the first level, but it is
                        # possible that not all children have `lin`. We stop
                        # going deeper and do not raise an error.
                        leaf_module = None  # type: ignore[assignment]
                        break
                    except Exception as e:
                        raise RuntimeError(
                            f"Encountered error when trying to get submodule {atom} in {module_path}"
                        ) from e

            if leaf_module is None:  # no match
                # Go to next item in plan dict.
                continue

            # When `path_split` is empty, `leaf_module` should point to the target.
            # Thus we apply the plan to the target module.
            assert len(atom) > 0, "we should have entered the while loop at least once"
            parent_module.register_module(  # type: ignore[call-arg] # pyre-ignore[20]
                atom,
                parallelize_module(  # type: ignore[arg-type]
                    leaf_module, device_mesh, parallelize_style  # type: ignore[arg-type] # pyre-ignore[6]
                ),
            )
        return module
    else:
        raise TypeError(  # pyre-ignore[7]
            "Expect Union[ParallelStyle, Dict[str, ParallelStyle]] for"
            f" parallelize_plan, {type(parallelize_plan)} found!"
        )
