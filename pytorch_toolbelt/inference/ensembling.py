import torch
from torch import nn, Tensor
from typing import List, Union, Iterable, Optional, Dict

__all__ = ["ApplySoftmaxTo", "ApplySigmoidTo", "Ensembler", "PickModelOutput", "SelectByIndex"]

from pytorch_toolbelt.inference.tta import _deaugment_averaging


class ApplySoftmaxTo(nn.Module):
    def __init__(self, model: nn.Module, output_key: Union[str, List[str]] = "logits", dim=1, temperature=1):
        """
        Apply softmax activation on given output(s) of the model
        :param model: Model to wrap
        :param output_key: string or list of strings, indicating to what outputs softmax activation should be applied.
        :param dim: Tensor dimension for softmax activation
        :param temperature: Temperature scaling coefficient. Values > 1 will make logits sharper.
        """
        super().__init__()
        output_key = output_key if isinstance(output_key, (list, tuple)) else [output_key]
        # By converting to set, we prevent double-activation by passing output_key=["logits", "logits"]
        self.output_keys = set(output_key)
        self.model = model
        self.dim = dim
        self.temperature = temperature

    def forward(self, *input, **kwargs):
        output = self.model(*input, **kwargs)
        for key in self.output_keys:
            output[key] = output[key].mul(self.temperature).softmax(dim=1)
        return output


class ApplySigmoidTo(nn.Module):
    def __init__(self, model: nn.Module, output_key: Union[str, List[str]] = "logits", temperature=1):
        """
        Apply sigmoid activation on given output(s) of the model
        :param model: Model to wrap
        :param output_key: string or list of strings, indicating to what outputs sigmoid activation should be applied.
        :param temperature: Temperature scaling coefficient. Values > 1 will make logits sharper.
        """
        super().__init__()
        output_key = output_key if isinstance(output_key, (list, tuple)) else [output_key]
        # By converting to set, we prevent double-activation by passing output_key=["logits", "logits"]
        self.output_keys = set(output_key)
        self.model = model
        self.temperature = temperature

    def forward(self, *input, **kwargs):  # skipcq: PYL-W0221
        output = self.model(*input, **kwargs)
        for key in self.output_keys:
            output[key] = output[key].mul(self.temperature).sigmoid()
        return output


class Ensembler(nn.Module):
    __slots__ = ["outputs", "reduction"]

    """
    Compute sum (or average) of outputs of several models.
    """

    def __init__(self, models: List[nn.Module], reduction: str = "mean", outputs: Optional[Iterable[str]] = None):
        """

        :param models:
        :param reduction: Reduction key ('mean', 'sum', 'gmean', 'hmean' or None)
        :param outputs: Name of model outputs to average and return from Ensembler.
            If None, all outputs from the first model will be used.
        """
        super().__init__()
        self.outputs = outputs
        self.models = nn.ModuleList(models)
        self.reduction = reduction

    def forward(self, *input, **kwargs):  # skipcq: PYL-W0221
        outputs = [model(*input, **kwargs) for model in self.models]

        if self.outputs:
            keys = self.outputs
        elif isinstance(outputs[0], dict):
            keys = outputs[0].keys()
        elif torch.is_tensor(outputs[0]):
            keys = None
        else:
            raise RuntimeError()

        if keys is None:
            predictions = torch.stack(outputs)
            predictions = _deaugment_averaging(predictions, self.reduction)
            averaged_output = predictions
        else:
            averaged_output = {}
            for key in keys:
                predictions = [output[key] for output in outputs]
                predictions = torch.stack(predictions)
                predictions = _deaugment_averaging(predictions, self.reduction)
                averaged_output[key] = predictions

        return averaged_output


class PickModelOutput(nn.Module):
    """
    Wraps a model that returns dict or list and returns only a specific element.

    Usage example:
        >>> model = MyAwesomeSegmentationModel() # Returns dict {"OUTPUT_MASK": Tensor, ...}
        >>> net  = nn.Sequential(PickModelOutput(model, "OUTPUT_MASK")), nn.Sigmoid())
    """

    __slots__ = ["target_key"]

    def __init__(self, model: nn.Module, key: Union[str, int]):
        super().__init__()
        self.model = model
        self.target_key = key

    def forward(self, *input, **kwargs) -> Tensor:
        output = self.model(*input, **kwargs)
        return output[self.target_key]


class SelectByIndex(nn.Module):
    """
    Select a single Tensor from the dict or list of output tensors.

    Usage example:
        >>> model = MyAwesomeSegmentationModel() # Returns dict {"OUTPUT_MASK": Tensor, ...}
        >>> net  = nn.Sequential(model, SelectByIndex("OUTPUT_MASK"), nn.Sigmoid())
    """

    __slots__ = ["target_key"]

    def __init__(self, key: Union[str, int]):
        super().__init__()
        self.target_key = key

    def forward(self, outputs: Dict[str, Tensor]) -> Tensor:
        return outputs[self.target_key]
