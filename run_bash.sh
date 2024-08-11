#!/bin/bash
teacher_path=/home/miura/lab/KD-hdas/results/teacher/cifar100/efficientnet_v2_s/FINETUNE2/pretrained-20240716-002108/best.pth.tar

# bash run_finetune.sh train FINETUNE2 efficientnet_v2_s pretrained pretrained_LR_features-0.001_classifier-0.01_cosine_warmup-0
# bash run_finetune.sh train FINETUNE2 efficientnet_v2_m pretrained pretrained_LR_features-0.001_classifier-0.01_cosine_warmup-0

# for seed in 0 1 2 3 4;do
#     bash run_search.sh train stage BASELINE224 non non s${seed} BASELINE_BEST baseline_stage_architecture_ ${seed}
# done

# Ls=(0.3 0.4 0.5 0.6 0.7)
# Ts=(10 20)
# for t in ${Ts[@]}; do
#     bash run_evaluate.sh train cell KD_VALID_NEW efficientnet_v2_s /home/miura/lab/KD-hdas/results/teacher/cifar100/efficientnet_v2_s/FINETUNE2/pretrained-20240716-002108/best.pth.tar BASELINE224 l0.3T${t} T^2_to_soft_loss 0.3 ${t}
# # done

dags=(
    /home/miura/lab/KD-hdas/results/search_stage_KD/cifar100/BASELINE224/s0-20240810-190635/DAG/EP46-best.pickle
    /home/miura/lab/KD-hdas/results/search_stage_KD/cifar100/BASELINE224/s1-20240810-204147/DAG/EP49-best.pickle
    /home/miura/lab/KD-hdas/results/search_stage_KD/cifar100/BASELINE224/s2-20240810-221715/DAG/EP44-best.pickle
    /home/miura/lab/KD-hdas/results/search_stage_KD/cifar100/BASELINE224/s3-20240810-235225/DAG/EP48-best.pickle
    /home/miura/lab/KD-hdas/results/search_stage_KD/cifar100/BASELINE224/s4-20240811-012801/DAG/EP47-best.pickle
)
for dag in ${dags[@]}; do
    extracted1=$(echo "$dag" | sed -n 's|.*-\([^/]*\)/DAG.*|\1|p')
    extracted2=$(echo "$dag" | sed -n 's|.*224/\([^/]*\)-2024.*|\1|p')
    bash run_evaluate.sh train stage BASELINE non non BASELINE_BEST ${dag} $extracted1$extracted2 baseline_stage_architecure_evaluation_nonkd 0
done