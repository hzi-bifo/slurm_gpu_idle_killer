#!/bin/bash
#SBATCH --gres=gpu:1

source ~/miniforge3/etc/profile.d/conda.sh

conda activate torchcu

sleep 900
