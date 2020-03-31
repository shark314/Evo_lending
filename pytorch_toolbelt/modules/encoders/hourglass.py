from collections import OrderedDict
from typing import List, Callable

import torch

from pytorch_toolbelt.modules import ACT_RELU, get_activation_block
from pytorch_toolbelt.modules.encoders import EncoderModule, make_n_channel_input
from torch import nn
import torch.nn.functional as F

__all__ = ["StackedHGEncoder", "StackedSupervisedHGEncoder"]


class HGResidualBlock(nn.Module):
    def __init__(self, input_channels: int, output_channels: int, reduction=2, activation: Callable = nn.ReLU):
        super(HGResidualBlock, self).__init__()

        mid_channels = input_channels // reduction

        self.bn1 = nn.BatchNorm2d(input_channels)
        self.act1 = activation(inplace=True)
        self.conv1 = nn.Conv2d(input_channels, mid_channels, kernel_size=1, bias=False)

        self.bn2 = nn.BatchNorm2d(mid_channels)
        self.act2 = activation(inplace=True)
        self.conv2 = nn.Conv2d(mid_channels, mid_channels, kernel_size=3, padding=1, bias=False)

        self.bn3 = nn.BatchNorm2d(mid_channels)
        self.act3 = activation(inplace=True)
        self.conv3 = nn.Conv2d(mid_channels, output_channels, kernel_size=1, bias=True)

        if input_channels == output_channels:
            self.skip_layer = nn.Identity()
        else:
            self.skip_layer = nn.Conv2d(input_channels, output_channels, kernel_size=1)

    def forward(self, x):
        residual = self.skip_layer(x)

        out = self.bn1(x)
        out = self.act1(out)
        out = self.conv1(out)

        out = self.bn2(out)
        out = self.act2(out)
        out = self.conv2(out)

        out = self.bn3(out)
        out = self.act3(out)
        out = self.conv3(out)
        out += residual
        return out


class HGStemBlock(nn.Module):
    def __init__(self, input_channels, output_channels, activation: Callable = nn.ReLU):
        super().__init__()

        self.conv1 = nn.Conv2d(input_channels, 16, kernel_size=3, padding=1, stride=2, bias=False)
        self.bn1 = nn.BatchNorm2d(16)
        self.act1 = activation(inplace=True)

        self.conv2 = nn.Conv2d(16, 32, kernel_size=3, padding=1, stride=1, bias=False)
        self.bn2 = nn.BatchNorm2d(32)
        self.act2 = activation(inplace=True)

        self.conv3 = nn.Conv2d(32, 64, kernel_size=3, padding=1, stride=2, bias=False)
        self.bn3 = nn.BatchNorm2d(64)
        self.act3 = activation(inplace=True)

        self.residual1 = HGResidualBlock(64, 128)
        self.residual2 = HGResidualBlock(128, output_channels)

    def forward(self, x):
        x = self.act1(self.bn1(self.conv1(x)))
        x = self.act2(self.bn2(self.conv2(x)))
        x = self.act3(self.bn3(self.conv3(x)))
        x = self.residual1(x)
        x = self.residual2(x)
        return x


class HGBlock(nn.Module):
    def __init__(self, depth: int, input_features: int, features, increase=0, activation=nn.ReLU):
        super(HGBlock, self).__init__()
        nf = features + increase
        self.up1 = HGResidualBlock(input_features, features, activation=activation)
        # Lower branch
        self.down = nn.Conv2d(features, features, kernel_size=3, padding=1, stride=2, groups=features, bias=False)
        # Start with average pool
        torch.nn.init.constant_(self.down.weight, 1.0 / 9.0)

        self.low1 = HGResidualBlock(input_features, nf, activation=activation)
        self.depth = depth
        # Recursive hourglass
        if self.depth > 1:
            self.low2 = HGBlock(depth - 1, nf, nf, increase=increase, activation=activation)
        else:
            self.low2 = HGResidualBlock(nf, nf, activation=activation)
        self.low3 = HGResidualBlock(nf, features, activation=activation)
        self.up = nn.Upsample(scale_factor=2, mode="nearest")

    def forward(self, x):
        up1 = self.up1(x)
        pool1 = self.down(x)
        low1 = self.low1(pool1)
        low2 = self.low2(low1)
        low3 = self.low3(low2)
        up2 = self.up(low3)
        hg = up1 + up2
        return hg


class HGFeaturesBlock(nn.Module):
    def __init__(self, features: int, activation: Callable):
        super().__init__()
        self.residual = HGResidualBlock(features, features, activation=activation)
        self.conv_bn_act = nn.Sequential(
            OrderedDict(
                [
                    ("conv", nn.Conv2d(features, features, kernel_size=1, bias=False)),
                    ("bn", nn.BatchNorm2d(features)),
                    ("relu", activation(inplace=True)),
                ]
            )
        )

    def forward(self, x):
        x = self.residual(x)
        x = self.conv_bn_act(x)
        return x


class HGSupervisionBlock(nn.Module):
    def __init__(self, features, supervision_channels: int):
        super().__init__()
        self.squeeze = nn.Conv2d(features, supervision_channels, kernel_size=1)
        self.expand = nn.Conv2d(supervision_channels, features, kernel_size=1)

    def forward(self, x):
        sup_mask = self.squeeze(x)
        sup_features = self.expand(sup_mask)
        return sup_mask, sup_features


class StackedHGEncoder(EncoderModule):
    """
    Original implementation: https://github.com/princeton-vl/pytorch_stacked_hourglass/blob/master/models/layers.py
    """

    def __init__(
        self, input_channels: int = 3, stack_level: int = 8, depth: int = 4, features: int = 256, activation=ACT_RELU
    ):
        super().__init__(
            channels=[features] + [features] * stack_level,
            strides=[4] + [4] * stack_level,
            layers=list(range(0, stack_level + 1)),
        )
        act = get_activation_block(activation)
        self.stem = HGStemBlock(input_channels, features, activation=act)

        input_features = features
        modules = []

        for _ in range(stack_level):
            modules.append(HGBlock(depth, input_features, features, increase=0, activation=act))
            input_features = features
        self.blocks = nn.ModuleList(modules)
        self.features = nn.ModuleList([HGFeaturesBlock(features, activation=act) for _ in range(stack_level)])
        self.num_blocks = len(modules)

        self.merge_features = nn.ModuleList(
            [nn.Conv2d(features, features, kernel_size=1) for _ in range(stack_level - 1)]
        )

    def forward(self, x):
        x = self.stem(x)
        outputs = [x]

        for i, hourglass in enumerate(self.blocks):
            hg = hourglass(x)
            features = self.features[i](hg)
            if i < self.num_blocks - 1:
                x = x + self.merge_features[i](features)
            outputs.append(features)

        return outputs

    def change_input_channels(self, input_channels: int, mode="auto"):
        self.stem.conv1 = make_n_channel_input(self.stem.conv1, input_channels, mode)
        return self

    @property
    def encoder_layers(self) -> List[nn.Module]:
        return [self.stem] + self.blocks


class StackedSupervisedHGEncoder(StackedHGEncoder):
    def __init__(
        self,
        supervision_channels: int,
        input_channels: int = 3,
        stack_level: int = 8,
        depth: int = 4,
        features: int = 256,
        activation=ACT_RELU,
    ):
        super().__init__(input_channels, stack_level, depth, features, activation)

        self.supervision_blocks = nn.ModuleList(
            [HGSupervisionBlock(features, supervision_channels) for _ in range(stack_level - 1)]
        )

    def forward(self, x):
        x = self.stem(x)
        outputs = [x]
        supervision = []

        for i, hourglass in enumerate(self.blocks):
            hg = hourglass(x)
            features = self.features[i](hg)

            if i < self.num_blocks - 1:
                sup_mask, sup_features = self.supervision_blocks[i](features)
                x = x + self.merge_features[i](features) + sup_features
                supervision.append(sup_mask)

            outputs.append(features)

        return outputs, supervision
