from __future__ import absolute_import

import argparse
import collections
import os
from datetime import datetime

import albumentations as A
import cv2
import numpy as np
import torch
from catalyst.contrib.models import UNet
from catalyst.dl.callbacks import EarlyStoppingCallback, UtilsFactory, JaccardCallback
from catalyst.dl.experiments import SupervisedRunner
from pytorch_toolbelt.utils.catalyst_utils import ShowPolarBatchesCallback
from pytorch_toolbelt.utils.dataset_utils import ImageMaskDataset, TiledImageMaskDataset
from pytorch_toolbelt.utils.fs import auto_file, find_in_dir, id_from_fname, read_rgb_image, read_image_as_is
from pytorch_toolbelt.utils.random import set_manual_seed
from pytorch_toolbelt.utils.torch_utils import maybe_cuda, rgb_image_from_tensor, to_numpy, count_parameters
from sklearn.model_selection import train_test_split
from torch import nn
from torch.backends import cudnn
from torch.nn import BCEWithLogitsLoss
from torch.optim import Adam
from torch.utils.data import DataLoader, WeightedRandomSampler
from models.fpn import fpn_resnext50


def get_model(model_name: str, image_size=None) -> nn.Module:
    if model_name == 'unet':
        return UNet()

    if model_name == 'fpn_resnext50':
        return fpn_resnext50()

    raise ValueError("Unsupported model name " + model_name)


def read_inria_mask(fname):
    mask = read_image_as_is(fname)
    return (mask > 0).astype(np.uint8)


def get_optimizer(optimizer_name: str, parameters, lr: float, **kwargs):
    from torch import optim as O

    if optimizer_name.lower() == 'sgd':
        return O.SGD(parameters, lr, **kwargs)

    if optimizer_name.lower() == 'adam':
        return O.Adam(parameters, lr, **kwargs)

    raise ValueError("Unsupported optimizer name " + optimizer_name)


def get_dataloaders(data_dir: str,
                    batch_size=16,
                    num_workers=4,
                    fast=False,
                    image_size=(224, 224)):
    all_images = sorted(find_in_dir(os.path.join(data_dir, 'train', 'images')))
    all_masks = sorted(find_in_dir(os.path.join(data_dir, 'train', 'gt')))
    region = [id_from_fname(fname)[:6] for fname in all_images]
    train_img, valid_img, train_mask, valid_mask = train_test_split(all_images, all_masks, random_state=1234, test_size=0.1, stratify=region)

    fast_size = batch_size * 4
    if fast and len(train_img) > fast_size:
        train_img = train_img[:batch_size * 4]
        train_mask = train_mask[:batch_size * 4]

    if fast and len(valid_img) > fast_size:
        valid_img = valid_img[:batch_size * 4]
        valid_mask = valid_mask[:batch_size * 4]

    train_transform = A.Compose([
        # Make random-sized crop with scale [50%..200%] of target size 1.5 larger than target crop to have some space around for
        # further transforms
        A.RandomSizedCrop((image_size[0] // 2, image_size[0] * 2), int(image_size[0] * 1.5), int(image_size[1] * 1.5)),

        # Apply random rotations
        A.ShiftScaleRotate(shift_limit=0, scale_limit=0, rotate_limit=45, border_mode=cv2.BORDER_CONSTANT),
        A.OneOf([
            A.GridDistortion(border_mode=cv2.BORDER_CONSTANT),
            A.ElasticTransform(border_mode=cv2.BORDER_CONSTANT),
        ]),

        # Add occasion blur/sharpening
        A.OneOf([
            A.GaussianBlur(),
            A.MotionBlur(),
            A.IAASharpen()
        ]),

        # Crop to desired image size
        A.CenterCrop(image_size[0], image_size[1]),

        # D4 Augmentations
        A.Transpose(),
        A.RandomRotate90(),

        # Spatial-preserving augmentations:
        A.OneOf([
            A.Cutout(),
            A.GaussNoise(),
        ]),
        A.OneOf([
            A.RandomBrightnessContrast(),
            A.CLAHE(),
            A.HueSaturationValue(),
            A.RGBShift(),
            A.RandomGamma()
        ]),
        # Weather effects
        # A.OneOf([
        #     A.RandomFog(),
        #     A.RandomRain(),
        #     A.RandomSunFlare()
        # ]),

        # Normalize image to make use of pretrained model
        A.Normalize()
    ])
    trainset = ImageMaskDataset(train_img, train_mask, read_rgb_image, read_inria_mask, transform=train_transform)

    validset = TiledImageMaskDataset(valid_img, valid_mask, read_rgb_image, read_inria_mask, transform=A.Normalize(),
                                     # For validation we don't want tiles overlap
                                     tile_size=image_size,
                                     tile_step=image_size,
                                     target_shape=(5000, 5000),
                                     keep_in_mem=True)

    num_train_samples = int(len(trainset) * (5000 * 5000) / (image_size[0] * image_size[1]))

    trainloader = DataLoader(trainset,
                             batch_size=batch_size,
                             num_workers=num_workers,
                             pin_memory=True,
                             drop_last=True,
                             sampler=WeightedRandomSampler(np.ones(len(trainset)), num_train_samples))

    validloader = DataLoader(validset,
                             batch_size=batch_size,
                             num_workers=num_workers,
                             pin_memory=True,
                             shuffle=False)

    return trainloader, validloader


def visualize_inria_predictions(input: dict, output: dict, mean=(0.485, 0.456, 0.406), std=(0.229, 0.224, 0.225)):
    images = []
    for image, target, logits in zip(input['features'], input['targets'], output['logits']):
        image = rgb_image_from_tensor(image, mean, std)
        target = to_numpy(target).squeeze(0)
        logits = to_numpy(logits).squeeze(0)

        overlay = np.zeros_like(image)
        true_mask = target > 0
        pred_mask = logits > 0

        overlay[true_mask & pred_mask] = np.array([0, 250, 0], dtype=overlay.dtype)  # Correct predictions (Hits) painted with green
        overlay[true_mask & ~pred_mask] = np.array([250, 0, 0], dtype=overlay.dtype)  # Misses painted with red
        overlay[~true_mask & pred_mask] = np.array([250, 250, 0], dtype=overlay.dtype)  # False alarm painted with yellow

        # overlay[logits > 0] += np.array([255, 0, 0], dtype=overlay.dtype)
        # overlay[target > 0] += np.array([0, 255, 0], dtype=overlay.dtype)

        overlay = cv2.addWeighted(image, 0.5, overlay, 0.5, 0, dtype=cv2.CV_8U)
        images.append(overlay)
    return images


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--seed', type=int, default=42, help='Random seed')
    parser.add_argument('--fast', action='store_true')
    parser.add_argument('--fp16', action='store_true')
    parser.add_argument('-dd', '--data-dir', type=str, required=True, help='Data directory for INRIA sattelite dataset')
    parser.add_argument('-m', '--model', type=str, default='unet', help='')
    parser.add_argument('-b', '--batch-size', type=int, default=8, help='Batch Size during training, e.g. -b 64')
    parser.add_argument('-e', '--epochs', type=int, default=150, help='Epoch to run')
    parser.add_argument('-es', '--early-stopping', type=int, default=None, help='Maximum number of epochs without improvement')
    # parser.add_argument('-f', '--fold', default=None, required=True, type=int, help='Fold to train')
    #     # parser.add_argument('-fe', '--freeze-encoder', type=int, default=0, help='Freeze encoder parameters for N epochs')
    #     # parser.add_argument('-ft', '--fine-tune', action='store_true')
    parser.add_argument('-lr', '--learning-rate', type=float, default=1e-4, help='Initial learning rate')
    parser.add_argument('-o', '--optimizer', default='Adam', help='Name of the optimizer')
    parser.add_argument('-c', '--checkpoint', type=str, default=None, help='Checkpoint filename to use as initial model weights')
    parser.add_argument('-w', '--workers', default=8, type=int, help='Num workers')

    args = parser.parse_args()
    set_manual_seed(args.seed)

    data_dir = args.data_dir
    num_workers = args.workers
    num_epochs = args.epochs
    batch_size = args.batch_size
    learning_rate = args.learning_rate
    model_name = args.model
    optimizer_name = args.optimizer
    image_size = (512, 512)

    train_loader, valid_loader = get_dataloaders(data_dir=data_dir,
                                                 batch_size=batch_size,
                                                 num_workers=num_workers,
                                                 image_size=image_size,
                                                 fast=args.fast)

    model = maybe_cuda(get_model(model_name, image_size=image_size))
    optimizer = get_optimizer(optimizer_name, model.parameters(), learning_rate)

    loaders = collections.OrderedDict()
    loaders["train"] = train_loader
    loaders["valid"] = valid_loader

    scheduler = torch.optim.lr_scheduler.MultiStepLR(optimizer, milestones=[10, 20, 40], gamma=0.3)

    # model runner
    runner = SupervisedRunner()

    if args.checkpoint:
        checkpoint = UtilsFactory.load_checkpoint(auto_file(args.checkpoint))
        UtilsFactory.unpack_checkpoint(checkpoint, model=model)
        print('Loaded model weights from', args.checkpoint)

    current_time = datetime.now().strftime('%b%d_%H_%M')
    prefix = f'{current_time}_{args.model}'
    log_dir = os.path.join('runs', prefix)
    os.makedirs(log_dir, exist_ok=False)

    print('Train session:', prefix)
    print('\tEpochs     :', num_epochs)
    print('\tWorkers    :', num_workers)
    print('\tData dir   :', data_dir)
    print('\tLog dir    :', log_dir)
    print('\tTrain size :', len(train_loader), len(train_loader.dataset))
    print('\tValid size :', len(valid_loader), len(valid_loader.dataset))
    print('Model:', model_name)
    print('\tParameters:', count_parameters(model))
    print('\tImage size:', image_size)
    print('Optimizer:', optimizer_name)
    print('\tLearning rate:', learning_rate)
    print('\tBatch size   :', batch_size)

    # model training
    runner.train(
        model=model,
        criterion=BCEWithLogitsLoss(),
        optimizer=optimizer,
        scheduler=scheduler,
        callbacks=[
            JaccardCallback(),
            ShowPolarBatchesCallback(visualize_inria_predictions, metric='jaccard', minimize=False),
            EarlyStoppingCallback(patience=5, min_delta=0.01, metric='jaccard', minimize=False),
        ],
        loaders=loaders,
        logdir=log_dir,
        num_epochs=num_epochs,
        verbose=True,
        main_metric='jaccard',
        minimize_metric=False
    )

    # Training is finished. Let's run predictions using best checkpointing weights
    best_checkpoint = UtilsFactory.load_checkpoint(auto_file('best.pth', where=log_dir))
    UtilsFactory.unpack_checkpoint(best_checkpoint, model=model)


if __name__ == '__main__':
    torch.backends.cudnn.benchmark = True
    main()
