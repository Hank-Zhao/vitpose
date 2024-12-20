import math
import sys
import time
import torch

from dataset import transforms
import train_utils.distributed_utils as utils
from .coco_eval import EvalCOCOMetric
from .loss import KpLoss

#from torchjpeg import dct
from torch.nn import functional as F
import random


def train_one_epoch(model, optimizer, data_loader, device, epoch,
                    print_freq=100, warmup=False, scaler=None):
    model.train()
    metric_logger = utils.MetricLogger(delimiter="  ")
    metric_logger.add_meter('lr', utils.SmoothedValue(window_size=1, fmt='{value:.6f}'))
    header = 'Epoch: [{}]'.format(epoch)

    lr_scheduler = None
    if epoch == 0 and warmup is True:  # 当训练第一轮（epoch=0）时，启用warmup训练方式，可理解为热身训练
        warmup_factor = 1.0 / 1000
        warmup_iters = min(1000, len(data_loader) - 1)

        lr_scheduler = utils.warmup_lr_scheduler(optimizer, warmup_iters, warmup_factor)

    mse = KpLoss()
    mloss = torch.zeros(1).to(device)  # mean losses
    for i, [images, targets] in enumerate(metric_logger.log_every(data_loader, print_freq, header)):
        images = torch.stack([image.to(device) for image in images])
        #print(images.shape)
        #images = dct_transform(images, chs_remove = [0, 1, 2, 3, 8, 9, 10, 16, 17, 24])
        #print(images.shape)
        # 混合精度训练上下文管理器，如果在CPU环境中不起任何作用
        with torch.cuda.amp.autocast(enabled=scaler is not None):
            results = model(images)

            losses = mse(results, targets)

        # reduce losses over all GPUs for logging purpose
        loss_dict_reduced = utils.reduce_dict({"losses": losses})
        losses_reduced = sum(loss for loss in loss_dict_reduced.values())

        loss_value = losses_reduced.item()
        # 记录训练损失
        mloss = (mloss * i + loss_value) / (i + 1)  # update mean losses

        if not math.isfinite(loss_value):  # 当计算的损失为无穷大时停止训练
            print("Loss is {}, stopping training".format(loss_value))
            print(loss_dict_reduced)
            sys.exit(1)

        optimizer.zero_grad()
        if scaler is not None:
            scaler.scale(losses).backward()
            scaler.step(optimizer)
            scaler.update()
        else:
            losses.backward()
            optimizer.step()

        if lr_scheduler is not None:  # 第一轮使用warmup训练方式
            lr_scheduler.step()

        metric_logger.update(loss=losses_reduced)
        now_lr = optimizer.param_groups[0]["lr"]
        metric_logger.update(lr=now_lr)

    return mloss, now_lr


@torch.no_grad()
def evaluate(model, data_loader, device, flip=False, flip_pairs=None):
    if flip:
        assert flip_pairs is not None, "enable flip must provide flip_pairs."

    model.eval()
    metric_logger = utils.MetricLogger(delimiter="  ")
    header = "Test: "

    key_metric = EvalCOCOMetric(data_loader.dataset.coco, "keypoints", "key_results.json")
    for image, targets in metric_logger.log_every(data_loader, 100, header):
        images = torch.stack([img.to(device) for img in image])
        #images = dct_transform(images, chs_remove = [0, 1, 2, 3, 8, 9, 10, 16, 17, 24])
        # 当使用CPU时，跳过GPU相关指令
        if device != torch.device("cpu"):
            torch.cuda.synchronize(device)

        model_time = time.time()
        outputs = model(images)
        if flip:
            flipped_images = transforms.flip_images(images)
            flipped_outputs = model(flipped_images)
            flipped_outputs = transforms.flip_back(flipped_outputs, flip_pairs)
            # feature is not aligned, shift flipped heatmap for higher accuracy
            # https://github.com/leoxiaobin/deep-high-resolution-net.pytorch/issues/22
            flipped_outputs[..., 1:] = flipped_outputs.clone()[..., 0:-1]
            outputs = (outputs + flipped_outputs) * 0.5

        model_time = time.time() - model_time

        # decode keypoint
        reverse_trans = [t["reverse_trans"] for t in targets]
        outputs = transforms.get_final_preds(outputs, reverse_trans, post_processing=True)

        key_metric.update(targets, outputs)
        metric_logger.update(model_time=model_time)

    # gather the stats from all processes
    metric_logger.synchronize_between_processes()
    print("Averaged stats:", metric_logger)

    # 同步所有进程中的数据
    key_metric.synchronize_results()

    if utils.is_main_process():
        coco_info = key_metric.evaluate()
    else:
        coco_info = None

    return coco_info
'''
def dct_transform(x, chs_remove=None, chs_pad=False,
                  size=8, stride=8, pad=0, dilation=1, ratio=8, chs_select = 9):
    """
        Transform a spatial image into its frequency channels.
        Prune low-frequency channels if necessary.
    """

    # assert x is a (3, H, W) RGB image
    assert x.shape[1] == 3

    # convert the spatial image's range into [0, 1], recommended by TorchJPEG
    x = x / 255.0

    # up-sample
    x = F.interpolate(x, scale_factor=ratio, mode='bilinear', align_corners=True)

    # convert to the YCbCr color domain, required by DCT
    x = x * 255
    x = dct.to_ycbcr(x)
    x = x - 128

    # perform block discrete cosine transform (BDCT)
    b, c, h, w = x.shape
    h_block = h // stride
    w_block = w // stride
    x = x.view(b * c, 1, h, w)
    x = F.unfold(x, kernel_size=(size, size), dilation=dilation, padding=pad, stride=(stride, stride))
    x = x.transpose(1, 2)
    x = x.view(b, c, -1, size, size)
    x_freq = dct.block_dct(x)
    x_freq = x_freq.view(b, c, h_block, w_block, size * size).permute(0, 1, 4, 2, 3)

    # prune channels
    if chs_remove is not None:
        channels = list(set([i for i in range(64)]) - set(chs_remove))
        selected_channels = random.sample(channels, chs_select)
        if not chs_pad:
            # simply remove channels
            x_freq = x_freq[:, :, selected_channels, :, :]
        else:
            # pad removed channels with zero, helpful for visualization
            x_freq[:, :, channels] = 0

    # stack frequency channels from each color domain
    x_freq = x_freq.reshape(b, -1, h_block, w_block)

    return x_freq
    '''