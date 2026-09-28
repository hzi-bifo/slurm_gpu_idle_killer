#!/bin/bash

cp slurm-gpu-idle-killer.service /etc/systemd/system/slurm-gpu-idle-killer.service
cp slurm_gpu_idle_killer.py /opt
cp slurm-gpu-idle-killer /etc/default/
cp user_mail_body.txt /etc/slurm-gpu-idle-killer-user-mail.txt
systemctl daemon-reload
systemctl restart slurm-gpu-idle-killer
journalctl -u slurm-gpu-idle-killer -b -f

