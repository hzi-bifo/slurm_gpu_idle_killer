# Slurm GPU Idle Killer

A Python script to kill any Slurm jobs which have requested GPU resource and which are not using them properly.

Checks are made every minute, and for those jobs with a GPU(s) allocated, the GPU resource must have been idle for check consecutive checks.

The idle check consists of no processes allocated (as viewed through nvidia-smi) and low GPU usage.

## Options

If REQUIRE_ALL_NODES_IDLE is set to True (default), a node using GPUs on multiple nodes will only be cancelled if all nodes are (GPU) idle. This has not been fully tested yet with a value of False.

If GPU_ACTIVITY_POLICY is set to 'zero' (default), all GPUs in an allocation must be unused before the job will be cancelled.

If DRY_RUN is set to True (default is False), jobs will not actually be cancelled, but will be ignored after the normal cancellation point, and will no longer monitored.

If DEBUG is set to True (default), log messages showing behaviour will be written to stdout.

Options are set in slurm-gpu-idle-killer, typically located as below in /etc/default

### Emailing the job owner

When a job is killed, an email can also be sent to the job's owner, via the local MTA using mailx. This is disabled unless USER_EMAIL_CMD is set.

- USER_EMAIL_CMD: command which prints a user's email address; the Slurm username is appended as the last argument, e.g. /root/bin/getUserEmail.sh
- USER_MAIL_SUBJECT: email subject (default 'GPU job killed')
- USER_MAIL_BODY_FILE: file with the text which starts the email (template: user_mail_body.txt, installed by deploy.sh to /etc/slurm-gpu-idle-killer-user-mail.txt). The host, job details, time and slurm job info are appended after it. The file is re-read for each email, so edits take effect without a restart.
- USER_MAIL_TEST_ADDRESS: if set, user emails are sent only to this address. The real address is still looked up and shown at the top of the email.
- MAIL_FROM: used as the sender if set

With DRY_RUN set, user emails are only sent when USER_MAIL_TEST_ADDRESS is set. A failure to email the user is logged and does not stop the job being cancelled.

## Installation

(as root)

wget https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Linux-x86_64.sh

bash Miniforge3-$(uname)-$(uname -m).sh

eval "$(/root/miniforge3/bin/conda shell.bash hook)"

conda create --name slurm python=3 dataclasses

conda activate slurm

cp slurm-gpu-idle-killer.service /etc/systemd/system/slurm-gpu-idle-killer.service

cp slurm_gpu_idle_killer.py /opt

cp slurm-gpu-idle-killer /etc/default

sudo systemctl daemon-reload

sudo systemctl enable --now slurm-gpu-idle-killer.service

sudo systemctl status slurm-gpu-idle-killer.service


Alternatively run deploy.sh (as root) from the repository. It always updates the script and service file, but installs the config file and user email body from their templates only if they don't already exist. Existing ones are left unchanged and any differences from the template are shown, so new settings can be merged in by hand.

## Operation

View logs with journalctl -u slurm-gpu-idle-killer.service -f


## Testing

(as a user)

wget https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Linux-x86_64.sh

bash Miniforge3-$(uname)-$(uname -m).sh

eval "$(~/miniforge3/bin/conda shell.bash hook)"

conda create -n torchcu -y python=3.12 pip

conda activate torchcu

pip install --upgrade pip

pip install torch --index-url https://download.pytorch.org/whl/cu121

python testing/working_cuda.py

python testing/idle_gpu.py

sbatch testing/gpu_job_valid.sh

sbatch testing/cpu_job_invalid.sh

Testing involves running a valid job and an invalid job, confirm that valid job remains, invalid is cancelled

