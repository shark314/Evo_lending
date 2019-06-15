from __future__ import absolute_import
import torch

from torch import nn
from torch.nn import functional as F

__all__ = ['FPNBottleneckBlock', 'FPNBottleneckBlockBN', 'FPNPredictionBlock', 'FPNFuse', 'FPNFuseSum', 'UpsampleAdd', 'UpsampleAddConv']


class FPNBottleneckBlock(nn.Module):
    def __init__(self, input_channels, output_channels):
        super().__init__()
        self.conv = nn.Conv2d(input_channels, output_channels, kernel_size=1)

    def forward(self, x):
        x = self.conv(x)
        return x


class FPNBottleneckBlockBN(nn.Module):
    def __init__(self, input_channels, output_channels):
        super().__init__()
        self.conv = nn.Conv2d(input_channels, output_channels, kernel_size=1, bias=False)
        self.bn = nn.BatchNorm2d(output_channels)

    def forward(self, x):
        x = self.conv(x)
        return x


class FPNPredictionBlock(nn.Module):
    def __init__(self, input_channels, output_channels, mode='nearest'):
        super().__init__()
        self.input_channels = input_channels
        self.output_channels = output_channels
        self.conv = nn.Conv2d(self.input_channels, self.output_channels, kernel_size=3, padding=1)
        self.mode = mode

    def forward(self, x, y=None):
        if y is not None:
            x = x + F.interpolate(y, size=x.size()[2:], mode=self.mode, align_corners=True if self.mode == 'bilinear' else None)

        x = self.conv(x)
        return x


class UpsampleAdd(nn.Module):
    """
    Compute pixelwise sum of first tensor and upsampled second tensor and convolve with 3x3 kernel
    to smooth aliasing artifacts
    """

    def __init__(self, filters: int, upsample_scale=None, mode='nearest', align_corners=None):
        super().__init__()
        self.interpolation_mode = mode
        self.upsample_scale = upsample_scale
        self.align_corners = align_corners

    def forward(self, x, y=None):
        if y is not None:

            if self.upsample_scale is not None:
                y = F.interpolate(y,
                                  scale_factor=self.upsample_scale,
                                  mode=self.interpolation_mode,
                                  align_corners=self.align_corners)
            else:
                y = F.interpolate(y,
                                  size=(x.size(2), x.size(3)),
                                  mode=self.interpolation_mode,
                                  align_corners=self.align_corners)

            x = x + y

        return x


class UpsampleAddConv(nn.Module):
    """
    Compute pixelwise sum of first tensor and upsampled second tensor and convolve with 3x3 kernel
    to smooth aliasing artifacts
    """

    def __init__(self, filters: int, upsample_scale=None, mode='nearest', align_corners=None):
        super().__init__()
        self.interpolation_mode = mode
        self.upsample_scale = upsample_scale
        self.align_corners = align_corners
        self.conv = nn.Conv2d(filters, filters,
                              kernel_size=3,
                              padding=1)

    def forward(self, x, y=None):
        if y is not None:

            if self.upsample_scale is not None:
                y = F.interpolate(y,
                                  scale_factor=self.upsample_scale,
                                  mode=self.interpolation_mode,
                                  align_corners=self.align_corners)
            else:
                y = F.interpolate(y,
                                  size=(x.size(2), x.size(3)),
                                  mode=self.interpolation_mode,
                                  align_corners=self.align_corners)

            x = x + y

        x = self.conv(x)
        return x


class FPNFuse(nn.Module):
    def __init__(self, mode='bilinear', align_corners=True):
        super().__init__()
        self.mode = mode
        self.align_corners = align_corners

    def forward(self, features):
        layers = []
        dst_size = features[0].size()[-2:]

        for f in features:
            layers.append(F.interpolate(f, size=dst_size, mode=self.mode, align_corners=self.align_corners))

        return torch.cat(layers, dim=1)


class FPNFuseSum(nn.Module):
    """Compute a sum of individual FPN layers"""

    def __init__(self, mode='bilinear', align_corners=True):
        super().__init__()
        self.mode = mode
        self.align_corners = align_corners

    def forward(self, features):
        output = features[0]
        dst_size = features[0].size()[-2:]

        for f in features[1:]:
            output = output + F.interpolate(f, size=dst_size, mode=self.mode, align_corners=self.align_corners)

        return output
