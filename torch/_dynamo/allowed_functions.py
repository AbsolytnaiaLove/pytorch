import builtins
import collections
import copy
import functools
import inspect
import itertools
import math
import operator
import types
import warnings
from typing import Dict, Optional, Set

import torch
from torch._dynamo.exc import IncorrectUsage
from torch.fx._symbolic_trace import is_fx_tracing

from . import config
from .external_utils import is_compiling
from .utils import HAS_NUMPY, is_safe_constant, np

"""
A note on allowed functions:

Dynamo consults this file to determine if a particular function/module
is allowed to appear as a node in its fx output.

If a function is disallowed, it may either be traced-through, or skipped.

Trace-through means dynamo will continue to trace the interior code for
the function/module rather than stopping at its boundary and recording it
as a node in the fx graph. Whether tracing through or allowing, the functionality
of the function/module is part of the dynamo graph.  Caveat: if tracing through,
any interior operation could trigger its own graph-break.

Skips are determined by (torch/_dynamo/skipfiles.py) - see "a note on
skipfiles" there.
"""


def make_function_id_set(lazy_initializer):
    """
    Track a set of `id()`s of objects which are either allowed or not
    allowed to go into the generated FX graph.  Use to test for torch.*,
    numpy.*, builtins.*, etc.

    Support user modification to permit customization of what can be
    added to the graph and what will cause a graph break.
    """

    class FunctionIdSet:
        function_ids: Optional[Set[int]] = None
        function_names: Optional[Dict[int, str]] = None

        def __call__(self):
            if self.function_ids is None:
                value = lazy_initializer()
                if isinstance(value, dict):
                    self.function_ids = set(value.keys())
                    self.function_names = value
                else:
                    assert isinstance(value, set)
                    self.function_ids = value
            return self.function_ids

        def get_name(self, idx: int, default: str):
            self()  # lazy init
            return self.function_names.get(idx, default)

        def add(self, idx: int):
            self()  # lazy init
            self.function_ids.add(idx)

        def remove(self, idx: int):
            if idx in self():
                self.function_ids.remove(idx)

        def __contains__(self, idx: int):
            return idx in self()

    return FunctionIdSet()


@make_function_id_set
def _disallowed_function_ids():
    remove = [
        True,
        False,
        None,
        collections.OrderedDict,
        copy.copy,
        copy.deepcopy,
        inspect.signature,
        math.__package__,
        torch.__builtins__,
        torch.autocast_decrement_nesting,
        torch.autocast_increment_nesting,
        torch.autograd.grad,
        torch.clear_autocast_cache,
        torch.cuda.current_device,
        torch.distributions.constraints.is_dependent,
        torch.distributions.normal.Normal,
        torch.inference_mode,
        torch.set_anomaly_enabled,
        torch.set_autocast_cache_enabled,
        torch.set_autocast_cpu_dtype,
        torch.set_autocast_cpu_enabled,
        torch.set_autocast_enabled,
        torch.set_autocast_gpu_dtype,
        warnings.warn,
        torch._C._dynamo.eval_frame.unsupported,
    ]
    # extract all dtypes from torch
    dtypes = [
        obj for obj in torch.__dict__.values() if isinstance(obj, type(torch.float32))
    ]
    remove += dtypes
    storage = [
        obj
        for obj in torch.__dict__.values()
        if isinstance(obj, type(torch.FloatStorage))
    ]
    remove += storage
    return {id(x) for x in remove}


@make_function_id_set
def _allowed_function_ids():
    """
    Walk torch.* and get the ids of all the stuff in it
    """
    warnings.filterwarnings("ignore", category=UserWarning, module="torch.distributed")
    torch_object_ids = dict()

    def _is_allowed_module_prefix(obj):
        allowed_modules = ("torch", "math")
        # torch.nn.modules.rnn is disallowed because these modules internally
        # flatten their parameters.  This flattening process will call
        # Tensor.set_ with a Storage, and Storages cannot be traced with
        # AOTAutograd; so we need to graph-break. To ensure this, we inline
        # these functions, rather than keep them opaque-ly in the graph.
        disallowed_modules = (
            "torch.optim.",
            "torch.nn.modules.rnn.",
            "torch._dynamo.",
            "torch._C._dynamo.",
            "torch._inductor.",
            "torch._C.inductor.",
            "torch.fx.",
            "torch.distributed.fsdp.",
        )
        allowed_modules_dot = tuple([x + "." for x in allowed_modules])
        module = inspect.getmodule(obj)
        if module is None:
            return False

        mod_name = module.__name__

        if any(mod_name.startswith(m) for m in disallowed_modules):
            return False

        return mod_name in allowed_modules or mod_name.startswith(allowed_modules_dot)

    def _find_torch_objects(module):
        if any(
            module.__name__.startswith(mod_name)
            for mod_name in config.allowed_functions_module_string_ignorelist
        ):
            return
        torch_object_ids[id(module)] = module.__name__
        for name, obj in list(module.__dict__.items()):
            if id(obj) not in torch_object_ids:
                if isinstance(obj, types.ModuleType):
                    if obj.__name__.startswith("torch.") and _is_allowed_module_prefix(
                        obj
                    ):
                        torch_object_ids[id(obj)] = f"{module.__name__}.{name}"
                        _find_torch_objects(obj)
                elif _is_allowed_module_prefix(obj):
                    torch_object_ids[id(obj)] = f"{module.__name__}.{name}"
                elif inspect.getmodule(obj) is None and not is_safe_constant(obj):
                    torch_object_ids[id(obj)] = f"{module.__name__}.{name}"

    _find_torch_objects(torch)
    _find_torch_objects(math)

    # torch.Tensor.{fn}
    for name in dir(torch.Tensor):
        method = getattr(torch.Tensor, name)
        if isinstance(method, types.MethodDescriptorType):
            torch_object_ids[id(method)] = f"torch.Tensor.{name}"

    for idx in _disallowed_function_ids():
        if idx in torch_object_ids:
            del torch_object_ids[idx]

    for extra in (is_fx_tracing, is_compiling):
        torch_object_ids[id(extra)] = f"{extra.__module__}.{extra.__name__}"

    return torch_object_ids


@make_function_id_set
def _builtin_function_ids():
    rv = {
        id(v): f"builtins.{k}"
        for k, v in builtins.__dict__.items()
        if not k.startswith("_") and callable(v)
    }
    rv.update(
        {
            id(v): f"operator.{k}"
            for k, v in operator.__dict__.items()
            if not k.startswith("_") and callable(v)
        }
    )
    rv.update(
        {id(v): f"functools.{v.__name__}" for v in (itertools.chain, itertools.islice)}
    )
    rv[id(functools.reduce)] = "functools.reduce"
    return rv


@make_function_id_set
def _numpy_function_ids():
    rv = dict()
    if HAS_NUMPY:
        for mod in (np, np.random):
            rv.update(
                {
                    id(v): f"{mod.__name__}.{k}"
                    for k, v in mod.__dict__.items()
                    if callable(v)
                    and (getattr(v, "__module__", None) or mod.__name__) == mod.__name__
                }
            )
    return rv


@make_function_id_set
def _builtin_constant_ids():
    """
    Collects constant builtins by eliminating callable items.
    """
    rv = {
        id(v): f"builtins.{k}"
        for k, v in builtins.__dict__.items()
        if not k.startswith("_") and not callable(v)
    }
    return rv


def is_allowed(obj):
    """Is this safe to trace like torch.add ?"""
    # torch.ops is populated lazily so we don't necessarily have them in
    # _allowed_function_ids.  Figure it out by testing the type instead
    # in those cases
    return id(obj) in _allowed_function_ids or isinstance(
        obj,
        (torch._ops.OpOverloadPacket, torch._ops.OpOverload, torch._ops._OpNamespace),
    )


def torch_get_name(obj, default):
    """Convert a torch.* function to a string"""
    return _allowed_function_ids.get_name(id(obj), default)


def is_builtin_callable(obj):
    return id(obj) in _builtin_function_ids


def is_builtin_constant(obj):
    return id(obj) in _builtin_constant_ids


def is_numpy(obj):
    if HAS_NUMPY:
        return isinstance(obj, np.ndarray) or id(obj) in _numpy_function_ids
    else:
        return False


def allow_in_graph(fn):
    """
    Customize which functions TorchDynamo will include in the generated
    graph. Similar to `torch.fx.wrap()`.
    ::

        torch._dynamo.allow_in_graph(my_custom_function)

        @torch._dynamo.optimize(...)
        def fn(a):
            x = torch.add(x, 1)
            x = my_custom_function(x)
            x = torch.add(x, 1)
            return x

        fn(...)

    Will capture a single graph containing `my_custom_function()`.
    """
    if isinstance(fn, (list, tuple)):
        return [allow_in_graph(x) for x in fn]
    assert callable(fn), "allow_in_graph expects a callable"
    _allowed_function_ids.add(id(fn))
    _disallowed_function_ids.remove(id(fn))
    return fn


def _disallow_in_graph_helper(throw_if_not_allowed):
    def inner(fn):
        if isinstance(fn, (list, tuple)):
            return [disallow_in_graph(x) for x in fn]
        assert callable(fn), "disallow_in_graph expects a callable"
        if throw_if_not_allowed and not is_allowed(fn):
            raise IncorrectUsage(
                "disallow_in_graph is expected to be used on an already allowed callable (like torch.* ops). "
                "Allowed callables means callables that TorchDynamo puts as-is in the extracted graph."
            )
        _allowed_function_ids.remove(id(fn))
        _disallowed_function_ids.add(id(fn))
        return fn

    return inner


def disallow_in_graph(fn):
    """
    Customize which functions TorchDynamo will exclude in the generated
    graph and force a graph break on.
    ::

        torch._dynamo.disallow_in_graph(torch.sub)

        @torch._dynamo.optimize(...)
        def fn(a):
            x = torch.add(x, 1)
            x = torch.sub(x, 1)
            x = torch.add(x, 1)
            return x

        fn(...)

    Will break the graph on `torch.sub`, and give two graphs each with a
    single `torch.add()` op.
    """
    return _disallow_in_graph_helper(throw_if_not_allowed=True)(fn)


@_disallow_in_graph_helper(throw_if_not_allowed=False)
def graph_break():
    """Force a graph break"""
    pass


def forbid_in_graph(fn):
    """
    Customize which functions TorchDynamo will assert are not present while tracing.

    If you want a graph break on this function instead, use disallow_in_graph.
    TODO(voz): We now have allow_in_graph, disallow_in_graph, forbid_in_graph - some more robust
    documentation would not be amiss.
    """
    if isinstance(fn, (list, tuple)):
        return [forbid_in_graph(x) for x in fn]
    assert callable(fn), "forbid_in_graph applies only to callables"
    fn._dynamo_forbidden = True
    return fn


def _allow_in_graph_einops():
    try:
        import einops

        try:
            from einops._torch_specific import (  # requires einops>=0.6.1, torch >= 2.0
                allow_ops_in_compiled_graph,
            )

            # einops >= 0.6.1
            allow_ops_in_compiled_graph()
        except ImportError:
            # einops < 0.6.1
            allow_in_graph(einops.rearrange)
            allow_in_graph(einops.reduce)
            if hasattr(einops, "repeat"):
                allow_in_graph(einops.repeat)  # available since einops 0.2.0
            if hasattr(einops, "einsum"):
                allow_in_graph(einops.einsum)  # available since einops 0.5.0
            if hasattr(einops, "pack"):
                allow_in_graph(einops.pack)  # available since einops 0.6.0
            if hasattr(einops, "unpack"):
                allow_in_graph(einops.unpack)  # available since einops 0.6.0
    except ImportError:
        pass


_allow_in_graph_einops()
