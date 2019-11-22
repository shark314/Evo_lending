from typing import List

import torch
import torch.nn.functional as F
from torch import nn

from ..activated_batch_norm import ABN
from .common import DecoderModule
from ..unet import UnetCentralBlock, UnetDecoderBlock

__all__ = ["UNetDecoder"]


class UNetDecoder(DecoderModule):
    def __init__(self, feature_maps: List[int], decoder_features: int, mask_channels: int):
        super().__init__()

        if not isinstance(decoder_features, list):
            decoder_features = [decoder_features * (2 ** i) for i in range(len(feature_maps))]

        self.center = UnetCentralBlock(in_dec_filters=feature_maps[-1], out_filters=decoder_features[-1])

        blocks = []
        for block_index, in_enc_features in enumerate(feature_maps[:-1]):
            blocks.append(
                UnetDecoderBlock(
                    in_dec_filters=decoder_features[block_index + 1],
                    in_enc_filters=in_enc_features,
                    out_filters=decoder_features[block_index],
                )
            )

        self.blocks = nn.ModuleList(blocks)
        self.output_filters = decoder_features

        self.final = nn.Conv2d(decoder_features[0], mask_channels, kernel_size=1)

    def forward(self, feature_maps: List[torch.Tensor]) -> torch.Tensor:
        output = self.center(feature_maps[-1])

        for decoder_block, encoder_output in zip(reversed(self.blocks), reversed(feature_maps[:-1])):
            output = decoder_block(output, encoder_output)

        output = self.final(output)
        return output
