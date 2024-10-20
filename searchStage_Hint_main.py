# Copyright (c) Malong LLC
# All rights reserved.
#
# Contact: github@malongtech.com
#
# This source code is licensed under the LICENSE file in the root directory of this source tree.

import os
from config.searchStage_HINT_config import SearchStageHintConfig
from trainer.searchStage_Hint_trainer import SearchStageTrainer_HintKD
import utils
from utils.logging_util import get_std_logging
from genotypes.genotypes import save_DAG
from utils.visualize import plot2, png2gif
from utils.eval_util import RecordDataclass

from config import *

from tqdm import tqdm

LOSS_TYPES = ["training_hard_loss", "training_soft_loss", "training_loss", "validation_loss", "training_hint_loss"]
ACC_TYPES = ["training_accuracy", "validation_accuracy"]

def run_task(config):
    logger = get_std_logging(os.path.join(config.path, "{}.log".format(config.exp_name)))
    config.logger = logger

    config.print_params(logger.info)
    
    # set seed
    utils.set_seed_gpu(config.seed, config.gpus)
    
    # ================= define trainer ==================
    trainer = SearchStageTrainer_HintKD(config)
    trainer.resume_model()
    start_epoch = trainer.start_epoch
    # ================= record initial genotype ==================
    previous_arch = macro_arch = trainer.model.DAG()
    DAG_path = os.path.join(config.DAG_path, "EP00")
    plot_path = os.path.join(config.plot_path, "EP00")
    caption = "Initial DAG"
    plot2(macro_arch.DAG1, plot_path + '-DAG1', caption)
    plot2(macro_arch.DAG2, plot_path + '-DAG2', caption)
    plot2(macro_arch.DAG3, plot_path + '-DAG3', caption)
    save_DAG(macro_arch, DAG_path)
    
    # loss, accを格納する配列
    Record = RecordDataclass(LOSS_TYPES, ACC_TYPES)

    best_top1 = 0.
    is_best = False
    hint_step = 1
    # Step1:Hint learning
    logger.info("Step{}: Start Hint learning until stage{}: Epoch:[0 - {}][{}]".format(hint_step, hint_step, trainer.hint_epochs[hint_step-1], trainer.total_epochs))
    # Stage2,3を凍結させる
    trainer.model.freeze_stage(stage_ex=[1])
    for epoch in tqdm(range(start_epoch, trainer.hint_epochs[-1])):
        if epoch == trainer.hint_epochs[hint_step-1]:
            hint_step += 1
            logger.info("Step{}: Start Hint learning until stage{}: Epoch:[{} - {}][{}]".format(hint_step, hint_step, epoch, trainer.hint_epochs[hint_step-1], trainer.total_epochs))
            trainer.model.freeze_stage(stage_ex=list(range(1, hint_step+2)))
            
        train_top1, train_hint_loss, arch_train_hint_loss, arch_depth_loss = trainer.train_hint_epoch(epoch, printer=logger.info, stage=hint_step)
        val_top1, val_loss = trainer.val_epoch(epoch, printer=logger.info)
        trainer.lr_scheduler.step()
        
        
        # ================= record genotype logs ==================
        macro_arch = trainer.model.DAG()
        logger.info("DAG = {}".format(macro_arch))
        
        plot_path = os.path.join(config.plot_path, "EP{:02d}".format(epoch + 1))
        DAG_path = os.path.join(config.DAG_path, "EP{:02d}".format(epoch + 1))
        caption = "Epoch {}".format(epoch + 1)
        plot2(macro_arch.DAG1, plot_path + '-DAG1', caption)
        plot2(macro_arch.DAG2, plot_path + '-DAG2', caption)
        plot2(macro_arch.DAG3, plot_path + '-DAG3', caption)

        # ================= write tensorboard ==================
        trainer.writer.add_scalar('train/lr', round(trainer.lr_scheduler.get_last_lr()[0], 5), epoch)
        trainer.writer.add_scalar('train/hintloss', train_hint_loss, epoch)
        trainer.writer.add_scalar('train/archhintloss', arch_train_hint_loss, epoch)
        trainer.writer.add_scalar('train/archdepthloss', arch_depth_loss, epoch)
        trainer.writer.add_scalar('train/top1', train_top1, epoch)
        trainer.writer.add_scalar('val/loss', val_loss, epoch)
        trainer.writer.add_scalar('val/top1', val_top1, epoch)
        
        if previous_arch != macro_arch:
            save_DAG(macro_arch, DAG_path, is_best=is_best)
        trainer.save_checkpoint(epoch, is_best=is_best)

        Record.add(["training_hint_loss"], [train_hint_loss])
        Record.save(config.path)
        
    logger.info("Step4: Start KD learning: Epoch:[{} - {}][{}]".format(epoch, trainer.total_epochs, trainer.total_epochs))
    trainer.model.freeze_stage(stage_ex=(1,2,3,"linear"))
    # Step3:KD learning
    for epoch in tqdm(range(trainer.hint_epochs[hint_step-1], trainer.total_epochs)):
        train_top1, train_hardloss, train_softloss, train_loss, arch_train_hardloss, arch_train_softloss, arch_train_loss, arch_depth_loss = trainer.train_epoch(epoch, printer=logger.info)
        val_top1, val_loss = trainer.val_epoch(epoch, printer=logger.info)
        trainer.lr_scheduler.step()

        # ================= record genotype logs ==================
        macro_arch = trainer.model.DAG()
        logger.info("DAG = {}".format(macro_arch))
        
        plot_path = os.path.join(config.plot_path, "EP{:02d}".format(epoch + 1))
        DAG_path = os.path.join(config.DAG_path, "EP{:02d}".format(epoch + 1))
        caption = "Epoch {}".format(epoch + 1)
        plot2(macro_arch.DAG1, plot_path + '-DAG1', caption)
        plot2(macro_arch.DAG2, plot_path + '-DAG2', caption)
        plot2(macro_arch.DAG3, plot_path + '-DAG3', caption)

        # ================= write tensorboard ==================
        trainer.writer.add_scalar('train/lr', round(trainer.lr_scheduler.get_last_lr()[0], 5), epoch)
        trainer.writer.add_scalar('train/hardloss', train_hardloss, epoch)
        trainer.writer.add_scalar('train/softloss', train_softloss, epoch)
        trainer.writer.add_scalar('train/loss', train_loss, epoch)
        trainer.writer.add_scalar('train/archhardloss', arch_train_hardloss, epoch)
        trainer.writer.add_scalar('train/archsoftloss', arch_train_softloss, epoch)
        trainer.writer.add_scalar('train/archloss', arch_train_loss, epoch)
        trainer.writer.add_scalar('train/archdepthloss', arch_depth_loss, epoch)
        trainer.writer.add_scalar('train/top1', train_top1, epoch)
        trainer.writer.add_scalar('val/loss', val_loss, epoch)
        trainer.writer.add_scalar('val/top1', val_top1, epoch)

        # ================= record genotype and checkpoint ==================
        if best_top1 < val_top1:
            best_top1, is_best = val_top1, True
            best_macro = macro_arch
        else:
            is_best = False
        if previous_arch != macro_arch:
            save_DAG(macro_arch, DAG_path, is_best=is_best)
        trainer.save_checkpoint(epoch, is_best=is_best)
        logger.info("Until now, best Prec@1 = {:.4%}".format(best_top1))

        Record.add(LOSS_TYPES[:-1]+ACC_TYPES, [train_hardloss, train_softloss, train_loss, val_loss, train_top1, val_top1])
        Record.save(config.path)

    logger.info("Final best Prec@1 = {:.4%}".format(best_top1))
    logger.info("Final Best Genotype = {}".format(best_macro))

    png2gif(config.plot_path, config.DAG_path, file_name="DAG1_history", pattern="*DAG1*")
    png2gif(config.plot_path, config.DAG_path, file_name="DAG2_history", pattern="*DAG2*")
    png2gif(config.plot_path, config.DAG_path, file_name="DAG3_history", pattern="*DAG3*")
    
    trainer.writer.add_text('result/acc', utils.ListToMarkdownTable(["best_val_acc"], [best_top1]), 0)


def main():
    config = SearchStageHintConfig()
    run_task(config)


if __name__ == "__main__":
    main()
