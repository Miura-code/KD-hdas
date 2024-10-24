
'''
Fine tune teacher model for knowledge distillation using timm
'''
import os
import sys
import time
import numpy as np
from tqdm import tqdm

import torch
import torch.nn as nn
import torchvision.models
from torch.utils.tensorboard import SummaryWriter

import teacher_models
from teacher_models.utils import *
import utils
from utils.data_util import get_data, split_dataloader
from utils.eval_util import AverageMeter, RecordDataclass, accuracy
from utils.file_management import save_checkpoint
from utils.logging_util import get_std_logging

from utils.parser import BaseConfig, get_parser, parse_gpus
from utils.visualize import showModelOnTensorboard

LOSS_TYPES = ["training_loss", "validation_loss"]
ACC_TYPES = ["training_accuracy", "validation_accuracy"]

DEVICE = 'cuda'

class Config(BaseConfig):
    def build_parser(self):
        parser = get_parser("Search cells of H-DAS config")
        # ================= file settings ==================
        parser.add_argument('--name', required=True)
        parser.add_argument('--save', type=str, default='EXP', help='experiment name')
        # ================= dataset settings ==================
        parser.add_argument('--dataset', type=str, default='cifar10', help='CIFAR10')
        parser.add_argument('--batch_size', type=int, default=64, help='batch size')
        parser.add_argument('--cutout_length', type=int, default=0, help='cutout length')
        parser.add_argument('--advanced', action='store_true', help='advanced data transform. apply resize (224,224)')
        # ================= optimizer settings ==================
        parser.add_argument('--lr', type=float, default=0.025, help='lr for weights')
        parser.add_argument('--lr_min', type=float, default=0.001, help='minimum lr for weights')
        parser.add_argument('--momentum', type=float, default=0.9, help='momentum for weights')
        parser.add_argument('--weight_decay', type=float, default=3e-4,
                            help='weight decay for weights')
        # ================= training settings ==================
        parser.add_argument('--epochs', type=int, default=200, help='# of training epochs')
        parser.add_argument('--seed', type=int, default=2, help='random seed')
        parser.add_argument('--gpus', default='0', help='gpu device ids separated by comma. '
                            '`all` indicates use all gpus.')
        parser.add_argument('--workers', type=int, default=4, help='# of workers')
        parser.add_argument('--train_portion', type=float, default=0.9, help='portion of training data')
        # ================= model settings ==================
        parser.add_argument('--model_name', type=str, default='densenet121', help='teacher model name')
        parser.add_argument('--pretrained', action='store_true', help='use pretrained model.(finetune)')
        # ================= details ==================
        parser.add_argument('--description', type=str, default='', help='experiment details')

        return parser
    
    def __init__(self):
        parser = self.build_parser()
        args = parser.parse_args()
        super().__init__(**vars(args))

        self.data_path = '../data/'
        
        self.path = os.path.join(f'results/teacher/{self.dataset}/{self.model_name}', self.name)
        self.exp_name = '{}-{}'.format(args.save, time.strftime("%Y%m%d-%H%M%S"))
        self.path = os.path.join(self.path, self.exp_name)
        
        self.gpus = parse_gpus(self.gpus)

        self.pretrained = True if args.pretrained else False
        self.cifar = True if "cifar" in args.dataset else False

def run_task(config):
    logger = get_std_logging(os.path.join(config.path, "{}.log".format(config.name)))
    config.logger = logger
    config.print_params(logger.info)
    
    # set seed
    DEVICE = utils.set_seed_gpu(config.seed, config.gpus)


    # ================= define data loader ==================
    input_size, input_channels, n_classes, train_data = get_data(
        config.dataset, config.data_path, cutout_length=config.cutout_length, validation=False, advanced=config.advanced
    )
    train_loader, valid_loader = split_dataloader(train_data, config.train_portion, config.batch_size, config.workers)

    # ================= load model from timm ==================
    try:
        Exception_pretrained_model(model_name=config.model_name, cifar=config.cifar, pretrained=config.pretrained)
    except Exception as e:
        print(e)
        sys.exit()
    try:
        model = teacher_models.__dict__[config.model_name](num_classes = 1000 if config.pretrained else n_classes, weights="DEFAULT" if config.pretrained else None)
    except (RuntimeError, KeyError) as e:
        logger.info("model loading error!: {}\n \
                    tring to load from torchvision.models".format(e))
        model = torchvision.models.__dict__[config.model_name](num_classes = 1000 if config.pretrained else n_classes, 
                                                               weights="DEFAULT" if config.pretrained else None)

    # 最終層をつけかえる
    if config.pretrained and config.cifar:
        replace_classifier_to_numClasses(model, n_classes, printer=logger.info)
    # stem層を付け替える
    if (not config.advanced) and config.cifar:
        replace_stem_for_cifar(config.model_name, model, printer=logger.info)
    
    model = model.to(DEVICE)
    # teacher_models.utils.freeze_model(model)
    # logger.info("model parameters freezed excepting last classifier layer!")

    writer = SummaryWriter(log_dir=os.path.join(config.path, "tb"))
    writer.add_text('config', config.as_markdown(), 0)
    showModelOnTensorboard(writer, model, train_loader)
    print("load model end!")
    # ================= build Optimizer (CosineAnnealingLR, MultiStepLR) ==================
    warmup_epoch = 0
    if config.pretrained:
        params=set_params_lr(model, lr=(0.001, 0.01))
        optimizer = torch.optim.SGD(params, momentum=config.momentum, weight_decay=config.weight_decay)
    else:
        optimizer = torch.optim.SGD(model.parameters(), config.lr, momentum=config.momentum, weight_decay=config.weight_decay)
    
    lr_scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, config.epochs, eta_min=config.lr_min)
    # milestone = [int(0.5*config.epochs), int(0.75*config.epochs)]
    # gamma = 0.1
    # lr_scheduler = torch.optim.lr_scheduler.MultiStepLR(optimizer=optimizer, milestones=milestone, gamma=gamma)
    # ================= define criteria ==================
    criterion = nn.CrossEntropyLoss().to(DEVICE)
    
    # loss, accを格納する配列
    Record = RecordDataclass(LOSS_TYPES, ACC_TYPES)

    best_top1 = 0.
    steps = 0
    for epoch in tqdm(range(0, config.epochs)):
        train_top1, train_loss, steps = train(epoch, config.epochs, steps, model, train_loader, optimizer, criterion, printer=logger.info)
        val_top1, val_loss = valid(epoch, config.epochs, model, valid_loader, criterion, printer=logger.info)
        
        lr_scheduler.step()
        # if epoch > warmup_epoch:  
        #     lr_scheduler.step()
        # elif epoch == warmup_epoch:
        #     teacher_models.utils.freeze_model(model, unfreeze=True)
        #     logger.info("warm up end! [{}]/[{}]".format(epoch, config.epochs))
        #     logger.info("model parameters unfreezed!")

        writer.add_scalar('train/lr_0', round(lr_scheduler.get_last_lr()[0], 5), epoch)
        # writer.add_scalar('train/lr_1', round(lr_scheduler.get_last_lr()[1], 5), epoch)
        writer.add_scalar('train/loss', train_loss, epoch)
        writer.add_scalar('train/top1', train_top1, epoch)

        writer.add_scalar('val/loss', val_loss, epoch)
        writer.add_scalar('val/top1', val_top1, epoch)

    
        if best_top1 < val_top1:
            best_top1, is_best = val_top1, True
        else:
            is_best = False
        model_state = {'config': config,
                     'epoch': epoch,
                     'steps': steps,
                     'model': model.state_dict(),
                     'w_optim': optimizer.state_dict(),
        }
        save_checkpoint(model_state, config.path, is_best=is_best)

        Record.add(LOSS_TYPES+ACC_TYPES, [train_loss, val_loss, train_top1, val_top1])
        Record.save(config.path)
        logger.info("Until now, best Prec@1 = {:.4%}".format(best_top1))
    
    logger.info("Final best Prec@1 = {:.4%}".format(best_top1))
    writer.add_text('val/Top1-Acc', utils.ListToMarkdownTable(["best_val_acc"], [best_top1]), 0)

def train(epoch, total_epoch, step, model, train_loader, optimizer, criterion, printer):
    model.train()
    top1 = AverageMeter()
    top5 = AverageMeter()
    losses = AverageMeter()

    for trn_X, trn_y in tqdm(train_loader):
        N = trn_X.size(0)
        step += 1

        trn_X = trn_X.to(DEVICE)
        trn_y = trn_y.to(DEVICE)
        
        # ================= optimize network parameter ==================
        optimizer.zero_grad()
        logits = model(trn_X)
        loss = criterion(logits, trn_y)
        loss.backward()
        optimizer.step()
        # ================= evaluate model ==================
        prec1, prec5 = accuracy(logits, trn_y, topk=(1, 5))
        losses.update(loss.item(), N)
        top1.update(prec1.item(), N)
        top5.update(prec5.item(), N)

    printer("Train: [{:3d}/{}] Final Prec@1 {:.4%}".format(epoch, total_epoch - 1, top1.avg))

    return top1.avg, losses.avg, step

def valid(epoch, total_epochs, model, valid_loader, criterion, printer):
    top1 = AverageMeter()
    top5 = AverageMeter()
    losses = AverageMeter()

    model.eval()
    i = 0

    with torch.no_grad():
        for X, y in tqdm(valid_loader):
            N = X.size(0)
            i += 1

            X = X.to(DEVICE)
            y = y.to(DEVICE)

            logits = model(X)
            loss = criterion(logits, y)

            prec1, prec5 = accuracy(logits, y, topk=(1, 5))
            losses.update(loss.item(), N)
            top1.update(prec1.item(), N)
            top5.update(prec5.item(), N)
            
    printer("Valid: [{:3d}/{}] Final Prec@1 {:.4%}".format(epoch, total_epochs - 1, top1.avg))
    
    return top1.avg, losses.avg

def main():
    config = Config()
    run_task(config)


if __name__ == "__main__":
    main()
