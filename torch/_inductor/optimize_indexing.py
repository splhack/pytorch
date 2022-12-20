import dataclasses
import functools
import itertools
import logging
import math
import operator
from typing import Dict, Iterable, Union

import sympy

import torch
from .ir import IndexingDiv, InterpreterShim, LoopBody, ModularIndexing
from .utils import sympy_subs
from .virtualized import V

log = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class ValueRanges(object):
    lower: Union[sympy.Symbol, sympy.Number, int, float, bool]
    upper: Union[sympy.Symbol, sympy.Number, int, float, bool]

    @classmethod
    def wrap(cls, arg):
        if isinstance(arg, ValueRanges):
            return arg
        assert isinstance(arg, (int, float, bool))
        return ValueRanges(arg, arg)

    @classmethod
    def unary_map(cls, x, fn):
        """map lower and upper bound with fn"""
        x = cls.wrap(x)
        return ValueRanges(fn(x.lower), fn(x.upper))

    @classmethod
    def checked_unary_map(cls, x, fn):
        """check the max and min of computed upper and lower bound for the output"""
        out = cls.unary_map(x, fn)
        return ValueRanges(min(out.lower, out.upper), max(out.lower, out.upper))

    @classmethod
    def binary_map(cls, x, y, fn):
        """map upper and lower bounds accessing corresponding values of inputs"""
        x, y = cls.wrap(x), cls.wrap(y)

        return ValueRanges(
            fn(x.lower, y.lower),
            fn(x.upper, y.upper),
        )

    @classmethod
    def binary_map_products(cls, a, b, fn):
        """compute the product of all lower and upper bounds and take min and max"""
        a, b = cls.wrap(a), cls.wrap(b)
        products = [
            fn(x, y)
            for x, y in itertools.product([a.lower, a.upper], [b.lower, b.upper])
        ]
        return ValueRanges(min(products), max(products))


class ValueRangeAnalysis(object):
    def __init__(self):
        boolean_operators = (
            "eq",
            "ne",
            "lt",
            "gt",
            "le",
            "ge",
            "and_",
            "or_",
            "xor",
            "logical_and",
            "logical_or",
            "logical_not",
        )
        for op in boolean_operators:
            setattr(self, op, self.bool_handler)

    @staticmethod
    def bool_handler(*args, **kwargs):
        # just assuming bools can have both values
        return ValueRanges(
            sympy.logic.boolalg.BooleanFalse, sympy.logic.boolalg.BooleanTrue
        )

    @staticmethod
    def default_handler(*args, **kwargs):
        # many ops are unlikely to show up in optimizable indexing compute,
        # so we dont have full coverage
        return ValueRanges(-math.inf, math.inf)

    def load(self, name: str, index: sympy.Expr):
        return ValueRanges(-math.inf, math.inf)

    def store(self, name, index, value, mode=None):
        return

    def reduction(self, name, dtype, src_dtype, reduction_type, index, value):
        return ValueRanges(-math.inf, math.inf)

    def index_expr(self, index, dtype):
        assert isinstance(index, ValueRanges)
        return index

    @staticmethod
    def to_dtype(x, dtype: torch.dtype):
        def is_bool(val):
            return (
                isinstance(val, bool) or hasattr(low, "is_Boolean") and low.is_Boolean
            )

        x = ValueRanges.wrap(x)
        low, up = x.lower, x.upper
        if is_bool(low):
            assert is_bool(up)
            if dtype.is_floating_point:
                return ValueRanges(sympy.Float(0.0), sympy.Float(1.0))
            else:
                return ValueRanges(sympy.Integer(0), sympy.Integer(1))
        return ValueRanges.wrap(x)

    @staticmethod
    def constant(value, dtype):
        # using nan makes subsequent computation throw, and for the purposes of optimization
        # returning -math.inf - math.inf is equivalent to giving up
        if math.isnan(value):
            return ValueRanges(-math.inf, math.inf)
        if isinstance(value, int):
            return ValueRanges(sympy.Integer(value), sympy.Integer(value))
        else:
            return ValueRanges(sympy.Float(value), sympy.Float(value))

    @staticmethod
    def reciprocal(x):
        return ValueRanges.checked_unary_map(x, lambda y: 1 / y)

    @staticmethod
    def abs(x):
        return ValueRanges.checked_unary_map(x, abs)

    @staticmethod
    def neg(x):
        return ValueRanges.checked_unary_map(x, lambda x: -x)

    @staticmethod
    def truediv(a, b):
        return ValueRanges.binary_map_products(a, b, operator.truediv)

    @staticmethod
    def div(a, b):
        return ValueRanges.binary_map_products(a, b, operator.truediv)

    @staticmethod
    def add(a, b):
        return ValueRanges.binary_map(a, b, operator.add)

    @staticmethod
    def mul(a, b):
        return ValueRanges.binary_map_products(a, b, operator.mul)

    @staticmethod
    def sub(a, b):
        return ValueRanges.binary_map_products(a, b, operator.sub)

    @staticmethod
    def exp(x):
        return ValueRanges.unary_map(x, sympy.functions.elementary.exponential.exp)

    @staticmethod
    def square(x):
        return ValueRanges.checked_unary_map(x, lambda y: y * y)

    @staticmethod
    def log(x):
        return ValueRanges.checked_unary_map(
            x, lambda y: -math.inf if y <= 0 else sympy.log(y)
        )

    @staticmethod
    def sqrt(x):
        return ValueRanges.unary_map(x, sympy.sqrt)

    @staticmethod
    def pow(a, b):
        return ValueRanges.binary_map_products(a, b, operator.pow)

    @staticmethod
    def minimum(a, b):
        return ValueRanges.binary_map(a, b, min)

    @staticmethod
    def maximum(a, b):
        return ValueRanges.binary_map(a, b, max)

    @staticmethod
    def where(a, b, c):
        return ValueRanges(min(b.lower, c.lower), max(b.upper, c.upper))

    @staticmethod
    def floor(x):
        return ValueRanges.unary_map(x, sympy.functions.elementary.integers.floor)

    @staticmethod
    def ceil(x):
        return ValueRanges.unary_map(x, sympy.functions.elementary.integers.ceiling)

    def __getattr__(self, name):
        log.warning(f"unhandled ValueRange op {name}")
        return self.default_handler


def dominated_nodes(
    initial_queue: Union[torch.fx.Node, Iterable[torch.fx.Node]], skip_filter=None
):
    if isinstance(initial_queue, torch.fx.Node):
        initial_queue = [initial_queue]

    dominated_set = set(initial_queue)

    while initial_queue:
        node = initial_queue.pop()
        for user in node.users:
            if skip_filter and skip_filter(user):
                continue
            if user not in dominated_set:
                dominated_set.add(user)
                initial_queue.append(user)

    return dominated_set


def val_expressable_in_32_bits(val):
    if isinstance(val, sympy.Expr):
        assert val.is_constant()
        if val.is_Integer or val.is_Boolean:
            val = int(val)
        else:
            val = float(val)

    # bound within mantissa
    if isinstance(val, float):
        return val <= (2**24) and val >= -(2**24)

    if isinstance(val, int):
        iinfo = torch.iinfo(torch.int32)
        return val <= iinfo.max and val >= iinfo.min

    raise Exception(f"Unexpected value {val}")


def range_expressable_in_32_bits(range):
    return val_expressable_in_32_bits(range.lower) and val_expressable_in_32_bits(
        range.upper
    )


class OptimizeIndexing(object):
    """
    Performs Value Range Analysis on LoopBody's fx graph to reduce precision of
    intermediaries from int64 to int32. This is an important optimization for indexing
    kernels such as Upsample and Interpolate.
    """

    def __init__(
        self,
        loop_body: LoopBody,
        indices_ranges: Dict[sympy.Symbol, int],
        indexing_exprs: Dict[str, sympy.Expr],
    ):
        self.loop_body = loop_body
        self.indices_range = indices_ranges
        self.indexing_exprs = indexing_exprs
        self.replacement_vals = {}
        self.interp_env = {}
        self.submodules = self.swap_submodules(dict(loop_body.submodules))

        indirect_var_set = set(loop_body.indirect_vars)
        self.index_indirect_dependecies = {
            index: expr.free_symbols & indirect_var_set
            for index, expr in indexing_exprs.items()
        }
        self.all_graphs = [loop_body.root_block.graph] + [
            block.graph for block in loop_body.subblocks.values()
        ]

        for k, v in indices_ranges.items():
            self.replace_indirect(k, ValueRanges(0, v))

        # avoid computing these values, pessimistically assume that they are unbounded
        self.tensor_values_set = dominated_nodes(
            [
                node
                for node in self.all_nodes
                if node.target in ["load", "reduction"]
                or "masked_subblock" in node.target
            ]
        )

    def run(self):
        """Compute Value Ranges and try reduce precision of 'to_dtype' nodes to int32 where possible"""

        for node in self.tensor_values_set:
            # we need to evaluate masked_subblock to recurse, and we need to set indirect values
            if (
                "masked_subblock" not in node.target
                and "set_indirect" not in node.target
            ):
                self.interp_env[node] = torch._inductor.optimize_indexing.ValueRanges(
                    -math.inf, math.inf
                )

        interpreter = InterpreterShim(self.loop_body.root_block.graph, self.submodules)
        interpreter.run(V.get_ops_handler(), initial_env=self.interp_env)

        # TODO - if this is empty, we should just return. will do in follow up,
        # want to stress test this pass.
        int64_dtype_nodes = [
            node
            for node in self.all_nodes
            if (
                node.target == "to_dtype"
                and node.args[2] == torch.int64
                and node not in self.tensor_values_set
            )
        ]

        # TODO - if dominated node of one to_dtype is not expressible in int32,
        # we should short circuit another to_dtype node if that node also dominates
        for node in int64_dtype_nodes:
            self.try_to_reduce_precision(node)

    def try_to_reduce_precision(self, node):
        # if a downstream use of a node explicitly converts to int32, or float16/float32/float64,
        # then it's precision is set for that chain of uses, and we don't need to consider those
        # dominated values
        def skip_filter(node):
            return node.target == "to_dtype" and node.args[2] in (
                torch.int32,
                torch.float32,
                torch.float64,
            )

        # TODO - there are dominated uses whose dtype does not depend on whether
        # we reduce the precision here, e.g. add(int64, int64) one of the args can be reduced to
        # int32 without changing the output precision of the node. this case hasn't shown up
        for dominated in dominated_nodes(node, skip_filter):
            if dominated.target in ["store", "output"]:
                continue

            if "set_indirect" in dominated.target:
                idx = int(dominated.target[len("set_indirect") :])
                indirect_var = self.loop_body.indirect_vars[idx]

                for index, indirect_vals in self.index_indirect_dependecies.items():
                    if indirect_var in indirect_vals:
                        index_val = self.replacement_vals[index]

                        if math.isinf(index_val.lower) or math.isinf(index_val.upper):
                            return

                        # all indices are integers, so make sure that we
                        # use the bounds of integers instead of floats.
                        # TODO - not sure if we should be doing int/float casts while tracing,
                        # might interfere with sympy.

                        index_val_int = ValueRanges(
                            int(index_val.lower), int(index_val.upper)
                        )
                        if not range_expressable_in_32_bits(index_val_int):
                            return

            if not range_expressable_in_32_bits(self.interp_env[dominated]):
                return

        args = list(node.args)
        args[2] = torch.int32
        node.args = tuple(args)

    @property
    def all_nodes(self):
        for graph in self.all_graphs:
            for node in graph.nodes:
                yield node

    def swap_submodules(self, submodules):
        keys = list(submodules.keys())
        for key in keys:
            if key == "get_index":
                submodules[key] = self.get_index
            elif "masked_subblock" in key:
                subblock = self.loop_body.subblocks[key]
                submodules[key] = functools.partial(
                    self.masked_subblock, subblock, self.interp_env
                )
            else:
                assert "set_indirect" in key
                idx = int(key[len("set_indirect") :])
                var = self.loop_body.indirect_vars[idx]
                indirect = functools.partial(self.set_indirect, var)
                submodules[key] = indirect

        return submodules

    def masked_subblock(self, subblock, env, mask, value):
        interp = InterpreterShim(subblock.graph, self.submodules)
        interp.run(V.get_ops_handler(), initial_env=env)
        output = [node for node in subblock.graph.nodes if node.target == "output"]
        assert len(output) == 1
        # dont bother unioning with value since the load from buffer will be
        # pessimistically assumed to be inf anyway
        return interp.env[output[0]]

    def set_indirect(self, var, new_var):
        self.replace_indirect(var, new_var)
        return new_var

    def replace_indirect(self, old, new):
        """Swap in a variable used in indirect indexing"""
        assert isinstance(new, ValueRanges)
        self.replacement_vals[old] = new

    def get_index(self, name):
        if name in self.replacement_vals:
            return self.replacement_vals[name]

        out = self._get_index_impl(name)
        self.replacement_vals[name] = out
        return out

    def _get_index_impl(self, name):
        expr = self.indexing_exprs[name]

        free_symbols = list(expr.free_symbols)

        if len(free_symbols) == 0:
            return ValueRanges(expr, expr)

        if expr in self.replacement_vals:
            return self.replacement_vals[expr]

        def replace_symbols_for_deriv(expr, ignore_mod=False):
            # for the purposes of finding local, minimum, maximum, assume smoothness
            def mod_indexing_rep(x, y, z):
                if z.is_constant():
                    return x / y

                # never really happens, we'll bail on optimizing
                return (x / y) % z

            def indexing_div_rep(x, y):
                return x / y

            return expr.replace(ModularIndexing, mod_indexing_rep).replace(
                IndexingDiv, indexing_div_rep
            )

        symbols = expr.free_symbols
        monotonic_increasing = []
        monotonic_decreasing = []
        other_symbols = []

        expr_for_deriv = replace_symbols_for_deriv(expr, True)
        for symbol in symbols:
            diff = sympy.diff(expr_for_deriv, symbol)
            if diff.is_positive:
                monotonic_increasing.append(symbol)
            elif diff.is_positive is False:  # can return None
                monotonic_decreasing.append(symbol)
            else:
                other_symbols.append(symbol)

        if not other_symbols:
            max_val = sympy_subs(
                expr,
                {
                    k: (v.upper if k in monotonic_increasing else v.lower)
                    for k, v in self.replacement_vals.items()
                },
            )
            min_val = sympy_subs(
                expr,
                {
                    k: (v.lower if k in monotonic_increasing else v.upper)
                    for k, v in self.replacement_vals.items()
                },
            )
            return ValueRanges(min_val, max_val)
        else:
            # bail on optimizing, have not run into this yet
            return ValueRanges(-math.inf, math.inf)


def indexing_dtype_strength_reduction(
    loop_body: LoopBody, indices: Dict[sympy.Symbol, int]
):
    """
    Performs Value Range Analysis on LoopBody's fx graph to reduce precision of
    intermediaries from int64 to int32
    """
    index = list(indices.keys())
    assert len(index) == len(loop_body.var_ranges), (index, loop_body.var_ranges)
    assert all(v not in loop_body.var_ranges for v in index)
    replacements = dict(zip(loop_body.var_ranges.keys(), index))
    indexing = {
        name: sympy_subs(expr, replacements)
        for name, expr in loop_body.indexing_exprs.items()
    }
    with V.set_ops_handler(ValueRangeAnalysis()):
        OptimizeIndexing(loop_body, indices, indexing).run()
