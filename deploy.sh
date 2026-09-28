#!/bin/bash

# Install a config file from its template only if it doesn't exist yet; otherwise leave it alone
# and show how it differs from the template, so new settings can be merged in by hand.
install_config() {
    local template=$1 target=$2
    if [ ! -e "$target" ]; then
        cp "$template" "$target"
        echo "Installed $target from template - edit it before relying on it"
    elif ! diff -q "$target" "$template" > /dev/null; then
        echo "Leaving existing $target unchanged. Differences from template $template:"
        diff "$target" "$template"
        echo
    fi
}

cp slurm-gpu-idle-killer.service /etc/systemd/system/slurm-gpu-idle-killer.service
cp slurm_gpu_idle_killer.py /opt
install_config slurm-gpu-idle-killer /etc/default/slurm-gpu-idle-killer
install_config user_mail_body.txt /etc/slurm-gpu-idle-killer-user-mail.txt
systemctl daemon-reload
systemctl restart slurm-gpu-idle-killer
journalctl -u slurm-gpu-idle-killer -b -f
