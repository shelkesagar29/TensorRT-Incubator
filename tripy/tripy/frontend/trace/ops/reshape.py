#
# SPDX-FileCopyrightText: Copyright (c) 1993-2024 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

from dataclasses import dataclass
from typing import Optional, Sequence, Tuple, List, Union

from tripy import export, utils
from tripy.common.exception import raise_error
from tripy.frontend import utils as frontend_utils
from tripy.frontend.trace.ops import utils as op_utils
from tripy.frontend.trace.ops.base import BaseTraceOp


@dataclass(repr=False)
class Reshape(BaseTraceOp):

    output_rank: int
    output_len: Optional[int] = None  # only used to help with infer_len for a shape input

    def infer_dtypes(self):
        self.outputs[0].dtype = self.inputs[0].dtype

    def infer_len(self):
        if self.output_len is not None:
            return [self.output_len]
        # skip inference for now because it requires obtaining the concrete _value_ of the second input,
        # not just its shape
        return [None]

    def infer_shape_output_idxs(self, inputs):
        from tripy.frontend.shape import Shape
        from tripy.utils import Result

        # Only wrap the reshaped output if the result is rank 1, otherwise don't wrap
        if isinstance(inputs[0], Shape) and self.output_rank == 1:
            return Result.ok([0])
        return Result.ok([])

    def infer_rank(self):
        if self.output_rank is None:
            shape_of_shape_input = op_utils.get_trace_shape(self.inputs[1])
            assert len(shape_of_shape_input) == 1
            assert shape_of_shape_input[0] >= 0, f"incorrect shape computation {shape_of_shape_input}"
            self.outputs[0].rank = shape_of_shape_input[0]
        else:
            self.outputs[0].rank = self.output_rank

    def to_flat_ir(self, inputs, outputs):
        from tripy.flat_ir.ops import DynamicReshapeOp

        DynamicReshapeOp.build(inputs, outputs)


@frontend_utils.convert_inputs_to_tensors(exclude=["input", "output_rank", "output_len"], shape_argument=["shape"])
def reshape_impl(
    input: "tripy.Tensor", shape: Sequence, output_rank: int, output_len: Optional[int] = None
) -> "tripy.Tensor":
    return Reshape.build([input, shape], output_rank, output_len)


@export.public_api(document_under="operations/functions")
def reshape(input: "tripy.Tensor", shape: Union["tripy.Shape", Sequence[Union[int, "tripy.Tensor"]]]) -> "tripy.Tensor":
    """
    Returns a new tensor with the contents of the input tensor in the specified shape.

    Args:
        input: The input tensor.
        shape: The desired compatible shape. If a shape dimension is -1, its value
            is inferred based on the other dimensions and the number of elements in the input.
            Atmost one dimension can be -1.

    Returns:
        A new tensor of the same data type as the input tensor and the specified shape.

    .. code-block:: python
        :linenos:
        :caption: Example

        input = tp.iota((2, 3), dtype=tp.float32)
        output = tp.reshape(input, (1, 6))

        assert np.array_equal(cp.from_dlpack(output).get(), np.reshape(cp.from_dlpack(input).get(), (1, 6)))
    """
    from tripy.frontend.tensor import Tensor
    from tripy.frontend.shape import Shape

    if isinstance(shape, Tensor):
        return Reshape.build([input, shape], None)

    def compute_unknown_dim(input_tensor, out_shape):
        # Elements in `out_shape` can be i) int ii) -1 iii) scalar Tensor
        # Compute the product of known dimensions in the reshape shape
        known_dims_product = 1
        for dim in out_shape:
            if isinstance(dim, int) and dim == -1:
                continue
            known_dims_product *= dim

        # Compute the total number of elements in the original shape
        total_elements = 1
        input_shape = input_tensor.shape
        for i in range(input_tensor.rank):
            total_elements *= input_shape[i]

        # Infer the dimension
        inferred_dim = total_elements / known_dims_product

        return inferred_dim

    unknown_dim_index = -1
    for i, dim in enumerate(shape):
        if isinstance(dim, int) and dim == -1:
            if unknown_dim_index != -1:
                raise_error(f"Reshape operation size operand can have only one dimension as -1, got shape={shape}.")
            unknown_dim_index = i
    if unknown_dim_index != -1:
        shape = list(shape)
        shape[unknown_dim_index] = compute_unknown_dim(input, shape)

    # we can support infer_len for tp.Shape if the result is rank 1 and the shape is constant
    output_len = None
    if isinstance(input, Shape) and len(shape) == 1 and isinstance(shape[0], int):
        output_len = shape[0]

    return reshape_impl(input, shape, len(shape), output_len)


@dataclass(repr=False)
class Squeeze(BaseTraceOp):

    dims: Tuple[int]
    out_shape: List[int]

    def infer_dtypes(self):
        self.outputs[0].dtype = self.inputs[0].dtype

    # Even if given a shape input, the output should not be a shape because the result will not be rank 1.
    # We should permit this, though, since it may be useful to extract a dimension from a shape as a scalar.
    infer_shape_output_idxs = op_utils.ShapeOutputIdxPolicies.never_return_shape

    def infer_rank(self):

        if len(self.dims) > 0:
            self.outputs[0].rank = self.inputs[0].rank - len(self.dims)
        else:
            from tripy.backend.mlir.utils import ShapeContext

            input_0_shape = op_utils.get_trace_shape(self.inputs[0])

            def squeeze_shape(shape, indices_to_squeeze):
                # Convert shape to list if it's not already
                shape = list(shape)
                if not indices_to_squeeze:  # If the list is empty, squeeze all dimensions that are 1
                    shape = [dim for dim in shape if dim != 1]
                else:
                    # Sort indices to squeeze in descending order to avoid index shifting issues
                    indices_to_squeeze.sort(reverse=True)
                    for idx in indices_to_squeeze:
                        if shape[idx] == 1:
                            shape.pop(idx)
                        else:
                            raise ValueError(f"Cannot squeeze dimension at index {idx} with value {shape[idx]}")

                return shape

            out_shape = squeeze_shape(input_0_shape, list(self.dims))
            self.outputs[0].rank = len(out_shape)
            self.out_shape = out_shape

    def to_flat_ir(self, inputs, outputs):
        from tripy.flat_ir.ops import DynamicReshapeOp

        if len(self.dims) > 0:
            select_indices = [i for i in range(inputs[0].rank) if i not in self.dims]
            input_shape = op_utils.get_shape_of_tensor(inputs[0])
            shape_slice = []
            for index in select_indices:
                shape_slice.append(op_utils.slice_rank1_tensor(input_shape, index, reason_details=""))

            output_shape = (
                op_utils.concatenate_tensors(shape_slice, dim=0)
                if len(shape_slice) > 0
                else op_utils.add_constant_tensor_from_list([], inputs[0].device)
            )

        else:
            output_shape = op_utils.add_constant_tensor_from_list(self.out_shape, inputs[0].device)
        DynamicReshapeOp.build([inputs[0], output_shape], outputs)


@export.public_api(document_under="operations/functions")
def squeeze(input: "tripy.Tensor", dims: Union[Tuple, int] = None) -> "tripy.Tensor":
    """
    Returns a new tensor with all specified singleton dimensions of the input tensor removed.

    Args:
        input: The input tensor.
        dims: The singleton dimensions to be removed.
              If this is not provided, all dimensions of size 1 are removed.

    Raises:
        TripyException: If any of the specified dimensions have a size that is not equal to 1.

    Returns:
        A new tensor of the same data type as the input tensor.

    .. code-block:: python
        :linenos:
        :caption: Squeeze All Dimensions

        input = tp.iota((1, 2, 1), dtype=tp.float32)
        output = tp.squeeze(input, dims=(0, 2))
        assert np.array_equal(cp.from_dlpack(output).get(), np.squeeze(cp.from_dlpack(input).get()))


    .. code-block:: python
        :linenos:
        :caption: Squeeze First Dimension

        input = tp.iota((1, 2, 1), dtype=tp.float32)
        output = tp.squeeze(input, 0)
        assert np.array_equal(cp.from_dlpack(output).get(), np.squeeze(cp.from_dlpack(input).get(), 0))

    .. code-block:: python
        :linenos:
        :caption: Squeeze First And Third Dimension

        input = tp.iota((1, 2, 1), dtype=tp.float32)
        output = tp.squeeze(input, (0, 2))

        assert np.array_equal(cp.from_dlpack(output).get(), np.squeeze(cp.from_dlpack(input).get(), (0, 2)))
    """

    if isinstance(dims, int):
        dims = utils.make_tuple(dims)

    return Squeeze.build([input], dims, None)
