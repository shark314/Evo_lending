from typing import Optional, List, Union, Mapping, Tuple

import numpy as np
import torch.jit
from torch import nn, Tensor

from pytorch_toolbelt.modules.interfaces import AbstractHead, FeatureMapsSpecification


class ResizeHead(AbstractHead):
    """
    Basic head that applies a dropout and convolution to the feature map with the smallest stride
    and upsamples the projected feature map to the original image size.
    """

    def __init__(
        self,
        input_spec: FeatureMapsSpecification,
        num_classes: int,
        output_name: Optional[str] = None,
        kernel_size: int = 3,
        dropout_rate: float = 0.0,
        dropout_inplace: bool = False,
        interpolation_mode="bilinear",
        interpolation_align_corners=True,
    ):
        """

        :param input_spec: Specification of input feature maps
        :param num_classes: Number of classes to predict
        :param output_name: Name of the output tensor. If None, returns tensor directly, otherwise returns dict with { output_name: tensor }
        :param dropout_rate: Dropout rate to apply before convolution.
        :param kernel_size: Convolution kernel size. Padding is automatically computed to using kernel_size // 2 formula.
        """

        super().__init__(input_spec)
        self.target_feature_map_index = input_spec.get_index_of_largest_feature_map()
        self.output_name = output_name

        channels = input_spec.channels[self.target_feature_map_index]

        self.dropout = nn.Dropout2d(dropout_rate, inplace=dropout_inplace)
        self.final = nn.Conv2d(channels, num_classes, kernel_size=kernel_size, padding=kernel_size // 2, bias=True)

        self.output_spec = FeatureMapsSpecification(channels=(num_classes,), strides=(1,))

        self.interpolation_mode = interpolation_mode
        self.interpolation_align_corners = interpolation_align_corners

    def forward(
        self, feature_maps: List[Tensor], output_size: Union[Tuple[int, int], torch.Size, None] = None
    ) -> Union[Tensor, Tuple[Tensor, ...], List[Tensor], Mapping[str, Tensor]]:
        x = feature_maps[self.target_feature_map_index]
        x = self.dropout(x)
        x = self.final(x)

        output = torch.nn.functional.interpolate(
            x, size=output_size, mode=self.interpolation_mode, align_corners=self.interpolation_align_corners
        )

        if self.output_name is not None:
            return {self.output_name: output}
        else:
            return output

    @torch.jit.unused
    def get_output_spec(self) -> FeatureMapsSpecification:
        return self.output_spec
