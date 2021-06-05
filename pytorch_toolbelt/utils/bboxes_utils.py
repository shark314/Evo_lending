from collections import namedtuple
from typing import Optional

import numpy as np
import torch
from pytorch_toolbelt.utils import to_numpy
from torchvision.ops import box_iou

__all__ = ["match_bboxes"]

BBoxesMatchResult = namedtuple(
    "BBoxesMatchResult",
    [
        # Array of shape [num_classes]
        "true_positives",
        # Array of shape [num_classes]
        "false_positives",
        # Array of shape [num_classes]
        "false_negatives",
        # Matrix of shape [num_classes+1, num_classes+1], where last class corresponds to None,
        # in other words - no detection.
        "confusion_matrix",
    ],
)


@torch.no_grad()
def match_bboxes(
    pred_boxes: np.ndarray,
    pred_labels: np.ndarray,
    pred_scores: np.ndarray,
    true_boxes: np.ndarray,
    true_labels: np.ndarray,
    num_classes: int,
    iou_threshold: float = 0.5,
    min_size: Optional[int] = None,
) -> BBoxesMatchResult:
    """
    Match predictect and ground-truth bounding boxes with following matching rules:
        - Matches are assigned by Hungarian algorithm to maximize IoU between predicted and ground-truth box
        - There can be only one match between predicted and ground-truth box
        - For multi-class case, if the boxes match, but their classes does not match,
          this counts as 1 FN to ground-truth class and 1 FP to predicted class

    :param pred_boxes: Detected bboxes in [x1, y1, x2, y2] format of shape [N,4]
    :param pred_labels: Detected labels of shape [N]
    :param pred_scores: Detected scores of shape [N]
    :param true_boxes:  Ground-truth bboxes in [x1, y1, x2, y2] format of shape [M,4]
    :param true_labels: Ground-truth labels of shape [M]
    :param num_classes: Total number of classes
    :param iou_threshold: IoU threshold to count detection as "match"
    :param min_size: If not None, will exclude boxes with area smaller than this parameter from evaluation
    :return:
        Tuple of [num_classes], [num_classes], [num_classes] corresponding to
        true positives, false positive and false negative counts per class
    """
    from scipy.optimize import linear_sum_assignment

    if len(pred_labels) != len(pred_boxes) or len(pred_labels) != len(pred_scores):
        raise ValueError(
            f"Inconsistent lengths of predicted bboxes:{len(pred_boxes)} labels:{len(pred_labels)} and their scores: {len(pred_scores)}"
        )

    if len(true_boxes) != len(true_labels):
        raise ValueError(
            f"Inconsistent lengths of ground-truth bboxes:{len(true_boxes)} and their labels:{len(true_labels)}"
        )

    # Reorder predictions to start matching with the most confident ones
    order = np.argsort(pred_scores)
    pred_boxes = pred_boxes[order]
    pred_labels = pred_labels[order]

    true_positives = np.zeros(num_classes, dtype=int)
    false_positives = np.zeros(num_classes, dtype=int)
    false_negatives = np.zeros(num_classes, dtype=int)

    # Confusion matrix [gt, pred]
    confusion_matrix = np.zeros((num_classes + 1, num_classes + 1), dtype=int)
    none_class = num_classes

    if min_size is not None:
        raise NotImplementedError("Min size is not supported")

    num_pred_objects = len(pred_boxes)
    num_true_objects = len(true_boxes)

    if num_pred_objects == 0 and num_true_objects == 0:
        return BBoxesMatchResult(
            true_positives=true_positives,
            false_positives=false_positives,
            false_negatives=false_negatives,
            confusion_matrix=confusion_matrix,
        )
    elif num_pred_objects == 0:
        for true_class in true_labels:
            false_negatives[true_class] += 1
            confusion_matrix[true_class, none_class] += 1
        return BBoxesMatchResult(
            true_positives=true_positives,
            false_positives=false_positives,
            false_negatives=false_negatives,
            confusion_matrix=confusion_matrix,
        )
    elif num_true_objects == 0:
        for pred_class in pred_labels:
            false_positives[pred_class] += 1
            confusion_matrix[none_class, pred_class] += 1
        return BBoxesMatchResult(
            true_positives=true_positives,
            false_positives=false_positives,
            false_negatives=false_negatives,
            confusion_matrix=confusion_matrix,
        )

    iou_matrix = to_numpy(box_iou(torch.from_numpy(pred_boxes).float(), torch.from_numpy(true_boxes).float()))
    row_ind, col_ind = linear_sum_assignment(iou_matrix, maximize=True)

    remainig_preds = np.ones(num_pred_objects, dtype=np.bool)
    remainig_trues = np.ones(num_true_objects, dtype=np.bool)

    for ri, ci in zip(row_ind, col_ind):
        pred_class = pred_labels[ri]
        true_class = true_labels[ci]
        if iou_matrix[ri, ci] > iou_threshold:
            remainig_preds[ri] = False
            remainig_trues[ci] = False
            if pred_class == true_class:
                # If there is a matching polygon found above, increase the count of true positives by one (TP).
                true_positives[true_class] += 1
            else:
                # If classes does not match, then we add false-positive for predicted class and
                # false-negative to target class
                false_positives[pred_class] += 1
                false_negatives[true_class] += 1

            confusion_matrix[true_class, pred_class] += 1

    if remainig_preds.any():
        for pred_class in pred_labels[remainig_preds]:
            false_positives[pred_class] += 1
            confusion_matrix[none_class, pred_class] += 1

    if remainig_trues.any():
        for true_class in true_labels[remainig_trues]:
            false_negatives[true_class] += 1
            confusion_matrix[true_class, none_class] += 1

    return BBoxesMatchResult(
        true_positives=true_positives,
        false_positives=false_positives,
        false_negatives=false_negatives,
        confusion_matrix=confusion_matrix,
    )
