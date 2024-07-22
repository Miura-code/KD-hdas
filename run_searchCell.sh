#!/bin/bash

type=$1
if [ ${type} = "train" ]; then
# ===== セルレベル探索　=====
    name=$2
    teacher_model=$3
    teacher_path=$4
    save=$5
    description=$6
    dataset=cifar100
    lambda=0.6
    T=3
    batch_size=64
    epoch=50
    train_portion=0.5 # searchの場合train_portionは0.5が最大値
    seed=0
    python searchCell_KD_main.py \
        --name $name \
        --teacher_name $teacher_model\
        --teacher_path $teacher_path \
        --l $lambda\
        --T $T \
        --dataset $dataset\
        --batch_size $batch_size \
        --epochs $epoch \
        --train_portion $train_portion \
        --seed $seed \
        --save $save \
        --advanced \
        --description $description
elif [ ${type} = "test" ]; then
    # ===== モデルをテスト =====
    resume_path=$2
    genotype=$3
    save=test
    dataset=cifar100
    cutout=0
    batch_size=64
    seed=0
    train_portion=1.0
    python testCell_main.py \
            --save $save \
            --resume_path $resume_path \
            --genotype $genotype \
            --dataset $dataset\
            --seed $seed
else
    echo ""
fi



