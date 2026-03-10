#!/usr/bin/env python3
"""
slurm_gpu_idle_killer.py  (controller-side)

- Finds running jobs requesting GPUs.
- For each job, parses `scontrol -d show job <jobid>` to determine the allocated GPU IDX per node:
    JOB_GRES=...
      Nodes=<node> ... GRES=... (IDX:<n>[,<m> | -range])
- Checks only those allocated GPUs for "idleness".
- Idle definition:
    (A) no compute processes on allocated GPU UUID(s), AND
    (B) util <= GPU_UTIL_MAX and mem <= GPU_MEM_MAX_MIB for allocated GPUs
- If idle for IDLE_THRESHOLD_POLLS consecutive polls -> scancel job.
- Safe default: if allocation can't be parsed or metrics can't be read -> do NOT kill.
"""

import os
import json
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from typing import Dict, List, Set, Tuple, Optional

# -------------------- CONFIG --------------------
STATE_FILE = Path("/var/tmp/slurm_gpu_idle_state.json")

POLL_INTERVAL_S = int(os.environ.get("POLL_INTERVAL_S", 60))
IDLE_THRESHOLD_POLLS = int(os.environ.get("IDLE_THRESHOLD_POLLS", 10))

GPU_UTIL_MAX = 5.0         # percent
GPU_MEM_MAX_MIB = 200.0    # MiB

SSH = ["ssh", "-oBatchMode=yes", "-oConnectTimeout=5"]

# Require ALL nodes' allocated GPUs to be idle before counting job idle.
# For single-node jobs (like yours) it makes no difference.
REQUIRE_ALL_NODES_IDLE = os.environ.get("REQUIRE_ALL_NODES_IDLE", "0") in ("1", "true", "True")

# --- policy ---
# "zero"  => consider node idle only if ZERO allocated GPUs are active (default; safest)
# "all"   => consider node idle if NOT ALL allocated GPUs are active (aggressive underutilization policy)
GPU_ACTIVITY_POLICY = os.environ.get("GPU_ACTIVITY_POLICY", "zero")

# Don't actually kill, just ignore this job
DRY_RUN = os.environ.get("DRY_RUN", "0") in ("1", "true", "True")

# Show debug messages
DEBUG = os.environ.get("DEBUG", "0") in ("1", "true", "True")

SEND_MAIL = os.environ.get("SEND_MAIL", "0") in ("1", "true", "True")

MAIL_TO = os.environ.get("MAIL_TO", "")
MAIL_FROM = os.environ.get("MAIL_FROM", "")

# ------------------------------------------------


def run(cmd: List[str], timeout: int = 20) -> str:
    return subprocess.check_output(cmd, text=True, timeout=timeout).strip()


@dataclass(frozen=True)
class Job:
    jobid: str
    user: str
    nodelist: str


def load_state() -> Dict[str, int]:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            return {}
    return {}


def save_state(state: Dict[str, int]) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    tmp.replace(STATE_FILE)


def get_running_gpu_jobs() -> List[Job]:
    out = run([
        "squeue", "-t", "R",
        "--Format=jobid:20,username:20,gres:40,nodelist:80",
        "--noheader",
    ])
    jobs: List[Job] = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        jobid, user, gres, nodelist = parts[0], parts[1], parts[2], parts[3]
        if "gpu" in gres:
            jobs.append(Job(jobid=jobid, user=user, nodelist=nodelist))
    return jobs


def expand_nodelist(nodelist: str) -> List[str]:
    out = run(["scontrol", "show", "hostnames", nodelist])
    return [x.strip() for x in out.splitlines() if x.strip()]


def scontrol_show_job_detail(jobid: str) -> str:
    return run(["scontrol", "-d", "show", "job", jobid])


def _parse_idx_blob(blob: str) -> Set[int]:
    idxs: Set[int] = set()
    for token in blob.split(","):
        token = token.strip()
        if not token:
            continue
        if "-" in token:
            a, b = token.split("-", 1)
            try:
                start = int(a)
                end = int(b)
                for i in range(start, end + 1):
                    idxs.add(i)
            except ValueError:
                pass
        else:
            try:
                idxs.add(int(token))
            except ValueError:
                pass
    return idxs


def parse_allocated_gpus_from_job_gres_block(scontrol_text: str) -> Dict[str, Set[int]]:
    """
    Tailored to your Slurm output, e.g.:

      JOB_GRES=gpu:t4:1
        Nodes=bioinf024 CPU_IDs=... Mem=... GRES=gpu:t4:1(IDX:1)

    Returns: { "bioinf024": {1} }
    """
    per_node: Dict[str, Set[int]] = {}

    # Match the indented node allocation lines under JOB_GRES.
    # We'll just scan all lines for: Nodes=<node> ... (IDX:<blob>)
    pat = re.compile(r"\bNodes=(\S+).*?\(IDX:([0-9,\-]+)\)")
    for line in scontrol_text.splitlines():
        m = pat.search(line)
        if not m:
            continue
        nodes_expr = m.group(1)
        idx_blob = m.group(2)

        try:
            nodes = expand_nodelist(nodes_expr)
        except Exception:
            nodes = [nodes_expr]

        idxs = _parse_idx_blob(idx_blob)
        if not idxs:
            continue

        for n in nodes:
            per_node.setdefault(n, set()).update(idxs)

    return per_node


def get_index_to_uuid(node: str) -> Dict[int, str]:
    out = run(SSH + [node, "nvidia-smi --query-gpu=index,uuid --format=csv,noheader"])
    m: Dict[int, str] = {}
    for line in out.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != 2:
            continue
        try:
            idx = int(parts[0])
        except ValueError:
            continue
        m[idx] = parts[1]
    return m


def get_compute_app_uuids(node: str) -> Set[str]:
    out = run(SSH + [node, "nvidia-smi --query-compute-apps=gpu_uuid --format=csv,noheader"])
    return {line.strip() for line in out.splitlines() if line.strip()}


def get_selected_gpu_stats_by_index(node: str, gpu_indices: Set[int]) -> Dict[int, Tuple[float, float]]:
    out = run(SSH + [node,
        "nvidia-smi --query-gpu=index,utilization.gpu,memory.used "
        "--format=csv,noheader,nounits"
    ])
    stats: Dict[int, Tuple[float, float]] = {}
    for line in out.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != 3:
            continue
        try:
            idx = int(parts[0])
            util = float(parts[1])
            mem = float(parts[2])
        except ValueError:
            continue
        if idx in gpu_indices:
            stats[idx] = (util, mem)
    return stats


def node_is_idle_on_allocated_gpus(node: str, gpu_indices: Set[int]) -> bool:
    """
    Returns True if this *node* should be considered "idle" according to GPU_ACTIVITY_POLICY.

    GPU_ACTIVITY_POLICY:
      - "zero": node is idle only if ZERO allocated GPUs are active
      - "all" : node is idle if NOT ALL allocated GPUs are active  (i.e. any allocated GPU idle)
    """
    if not gpu_indices:
        return False  # fail-safe: can't conclude idle

    # Map index -> uuid (stable)
    idx2uuid = get_index_to_uuid(node)
    allocated = [(i, idx2uuid.get(i)) for i in gpu_indices]
    if any(u is None for _, u in allocated):
        return False  # fail-safe: can't map reliably

    allocated_uuids = {u for _, u in allocated}

    # Signal A: compute processes attached to GPUs (best signal)
    active_uuids = get_compute_app_uuids(node)
    active_by_compute = {i for (i, u) in allocated if u in active_uuids}

    # Signal B: util/mem above threshold (backup signal; helps with edge cases)
    stats = get_selected_gpu_stats_by_index(node, gpu_indices)
    if len(stats) != len(gpu_indices):
        return False  # fail-safe

    active_by_stats = {
        i for i, (util, mem) in stats.items()
        if util > GPU_UTIL_MAX or mem > GPU_MEM_MAX_MIB
    }

    active_allocated = active_by_compute | active_by_stats

    if DEBUG:
        print(
            f"[DEBUG] node={node} allocated={sorted(gpu_indices)} "
            f"active={sorted(active_allocated)} policy={GPU_ACTIVITY_POLICY}"
        )

    if GPU_ACTIVITY_POLICY == "zero":
        # Idle only if none of the allocated GPUs show activity
        return len(active_allocated) == 0

    if GPU_ACTIVITY_POLICY == "all":
        # Idle if not all allocated GPUs are active (underutilization)
        return len(active_allocated) < len(gpu_indices)

    # Unknown policy -> fail-safe
    return False


def job_is_idle(job: Job) -> bool:
    detail = scontrol_show_job_detail(job.jobid)
    per_node_gpus = parse_allocated_gpus_from_job_gres_block(detail)

    if not per_node_gpus:
        if DEBUG:
            print(f"[DEBUG] {job.jobid}: could not parse allocated GPUs; skipping")
        return False

    # Only consider actual nodes the job is on (handles weird formatting safely)
    try:
        job_nodes = set(expand_nodelist(job.nodelist))
    except Exception:
        job_nodes = {job.nodelist}

    relevant = {n: g for n, g in per_node_gpus.items() if n in job_nodes and g}
    if not relevant:
        return False

    results: List[bool] = []
    for node, gidx in relevant.items():
        try:
            results.append(node_is_idle_on_allocated_gpus(node, gidx))
        except Exception:
            results.append(False)
    return all(results) if REQUIRE_ALL_NODES_IDLE else any(results)


def send_job_cancel_email(job_details, scontrol_data):

    if not MAIL_TO or not MAIL_FROM:
        raise ValueError("MAIL_TO or MAIL_FROM missing in properties file")

    hostname = os.uname().nodename

    # Build email message
    msg = EmailMessage()
    msg["To"] = MAIL_TO
    msg["From"] = MAIL_FROM
    msg["Subject"] = f"GPU job cancelled (hpc) - {hostname}"
    msg["Date"] = datetime.now().strftime("%a, %d %b %Y %H:%M:%S %z")

    body = f"""The following job has been cancelled.

Host:        {hostname}
Job details: {job_details}
Time:        {datetime.now()}

{scontrol_data}

"""

    msg.set_content(body)

    # Write to temporary file
    with tempfile.NamedTemporaryFile(mode="w", delete=False) as tmp:
        tmp.write(msg.as_string())
        tmp_path = tmp.name

    try:
        # Send using msmtp
        subprocess.run(["msmtp", "-t"], stdin=open(tmp_path, "r"), check=True)
    finally:
        os.remove(tmp_path)


def main() -> None:
    state = load_state()

    while True:
        jobs = get_running_gpu_jobs()
        running_ids = {j.jobid for j in jobs}

        for job in jobs:
            key = job.jobid
            count = int(state.get(key, 0))

            try:
                idle = job_is_idle(job)
            except Exception:
                idle = False

            if idle:
                count += 1
                if DRY_RUN and count == 1:
                    print(f"[INFO] Job {job.jobid} entered idle tracking (policy={GPU_ACTIVITY_POLICY})")

                state[key] = count

                if count >= IDLE_THRESHOLD_POLLS:
                    msg = f"{'DRYRUN would scancel' if DRY_RUN else 'scancel'} {job.jobid} (GPU-idle for {count * POLL_INTERVAL_S}s)"
                    print(f"[KILL] {msg}")

                    if SEND_MAIL:

                        with tempfile.NamedTemporaryFile(mode="w", delete=False) as tmp:
                            # tmp.write(msg.as_string())
                            tmp_path_2 = tmp.name
                        
                        subprocess.run(["scontrol", "show", "job", "9345718"], stdout=open(tmp_path_2, "w"), check=True)
                        
                        with open(tmp_path_2) as fp:
                          scontrol_data = fp.read()

                        send_job_cancel_email(msg, scontrol_data)
                    if not DRY_RUN:
                        subprocess.run(["scancel", job.jobid], check=False)

                    # reset so we don't spam
                    state[key] = 0

            else:
                state[key] = 0

        # Garbage-collect finished jobs
        for jobid in list(state.keys()):
            if jobid not in running_ids:
                del state[jobid]

        save_state(state)
        time.sleep(POLL_INTERVAL_S)


if __name__ == "__main__":
    main()

