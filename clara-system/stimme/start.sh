#!/bin/bash
# starts stimme-dienst (system/stimme/stimme_dienst.py) in conda env stimme-de; used by the systemd unit stimme-dienst
E=$HOME/miniconda3/envs/stimme-de
SP=$E/lib/python3.11/site-packages/nvidia
export LD_LIBRARY_PATH=$SP/cublas/lib:$SP/cudnn/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}   # faster-whisper needs CUDA 12 cuBLAS/cuDNN
export HF_HUB_OFFLINE=1 PYTHONNOUSERSITE=1 OMP_NUM_THREADS=8 TOKENIZERS_PARALLELISM=false
exec "$E/bin/python" -I $HOME/local_agentic_system/system/stimme/stimme_dienst.py
