#!/usr/bin/env python3
"""SLURM job submission wrapper for Snakemake.

Called by Snakemake via --cluster with {cluster.X} placeholders substituted.
Skips -A (account) and -p (partition) when their values are empty or 'placeholder',
so the same config works on HPC systems that don't require a SLURM account.

Any extra sbatch flags can be passed via {cluster.extra_args} in the config YAML
(e.g., extra_args: "--gres=gpu:1 --export=NONE"). Prefer --export=NONE plus explicit
exports over --export=ALL: propagating the full login environment re-exports PATH into
every child sbatch, and for self-perpetuating controllers that feeds an E2BIG PATH-growth
failure (PATH grows ~60B/generation until it blows MAX_ARG_STRLEN). The shared
entry template quotes extra_args and this helper applies shlex.split,
so embedded quoting and whitespace-bearing option values are preserved.

A transient sbatch failure (SLURM controller overload, momentary resource error) is
retried with exponential backoff so it doesn't permanently fail the Snakemake job.

Run tagging: when SYLVAN_RUN_NAME is set in the environment (annotate.sh/filter.sh
export it as the run directory basename), it is appended to the job name
("pasa." -> "pasa.Ahy_C"). The 15-run fleet shares rule names across runs, so the
tag is what makes squeue/sacct output attributable to a specific run and lets
controller guards match children safely.

The shared, shell-quoted Snakemake template lives in cluster_entry.sh. Direct
callers may pass pre-tokenized EXTRA_ARGS; the shared template instead marks a
single serialized value with --extra-args-string. Snakemake appends JOBSCRIPT.
"""

import os
import shlex
import subprocess
import sys
import time

from slurm_time import time_seconds, time_token


SKIP_VALUES = {"", "placeholder"}

# Backoff (seconds) before each retry after a failed sbatch. The initial attempt
# plus one retry per entry gives 4 total tries. Only transient failures (non-zero
# exit or a raised OSError) are retried; a successful submit returns immediately.
RETRY_BACKOFFS = (5, 15, 45)


# Required-value sbatch options. Values must be skipped even when their text
# resembles -t or --partition. Equals/attached forms are a single argv token.
VALUE_OPTIONS = set("""
-A -a -B -b -c -C -D -d -e -i -J -L -M -m -N -n -o -p -q -S -t -w -x
--account --array --batch --bb --bbf --begin --chdir --cluster-constraint
--clusters --comment --constraint --container --container-id --core-spec
--cores-per-socket --cpu-freq --cpus-per-gpu --cpus-per-task --deadline
--delay-boot --dependency --distribution --error --exclude --export
--export-file --extra --extra-node-info --gid --gpu-bind --gpu-freq
--gpus --gpus-per-node --gpus-per-socket --gpus-per-task --gres --gres-flags
--hint --input --job-name --licenses --mail-type --mail-user --mcs-label
--mem --mem-bind --mem-per-cpu --mem-per-gpu --mincpus --network
--nodefile --nodelist --nodes --ntasks --ntasks-per-core --ntasks-per-gpu
--ntasks-per-node --ntasks-per-socket --open-mode --output --partition
--power --priority --profile --qos --reservation --signal --sockets-per-node
--switches --thread-spec --threads-per-core --time --time-min
--tmp --tres-bind --tres-per-task --uid --wait-all-nodes --wckey --wrap
""".split())


def effective_option(args, short, long, default):
    """Inspect the actual sbatch argv in execution order, skipping option values."""
    value = default
    i = 1 if args and args[0] == 'sbatch' else 0
    while i < len(args):
        arg = args[i]
        if arg == '--':
            break
        if arg in VALUE_OPTIONS:
            if i + 1 >= len(args):
                raise ValueError(f'Missing value for sbatch option {arg}')
            if arg in (short, long):
                value = args[i + 1]
            i += 2
            continue
        if arg.startswith(long + '='):
            value = arg.split('=', 1)[1]
        elif arg.startswith(short) and arg != short:
            value = arg[len(short):]
        i += 1
    return value


def check_partition_time(partition, requested):
    """Reject a known PartitionTimeLimit before creating a pending job."""
    seconds = time_seconds(time_token(requested))
    if partition.lower() in SKIP_VALUES:
        print("WARNING: partition walltime cap not checked: no explicit partition was selected", file=sys.stderr)
        return
    try:
        info = subprocess.run(
            ["sinfo", "-h", "-p", partition, "-o", "%P|%l"],
            capture_output=True, text=True, timeout=10,
        )
        if info.returncode:
            raise ValueError("sinfo did not return partition limits")
        limits = {}
        for line in info.stdout.splitlines():
            name, sep, limit = line.strip().partition("|")
            if sep:
                limits[name.rstrip("*")] = time_seconds(limit)
        selected = partition.split(",")
        if not all(name in limits for name in selected):
            raise ValueError("not all selected partitions returned a limit")
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        print(f"WARNING: could not verify partition walltime: {exc}", file=sys.stderr)
        return
    allowed = any(limits[name] is None or
                  (seconds is not None and seconds <= limits[name])
                  for name in selected)
    if not allowed:
        raise ValueError(
            f"PartitionTimeLimit: requested time {requested} exceeds every selected "
            f"partition limit ({partition}). Change the time in the effective cluster "
            "configuration (SYLVAN_CLUSTER_CONFIG), or choose a suitable partition. "
            "No job was submitted; the requested time was not changed."
        )


def main():
    if len(sys.argv) < 11:
        print(
            "Usage: cluster_submit.py NODES MEMORY NCPUS NAME ACCOUNT PARTITION "
            "TIME OUTPUT ERROR [EXTRA_ARGS...] JOBSCRIPT",
            file=sys.stderr,
        )
        sys.exit(1)

    nodes = sys.argv[1]
    memory = sys.argv[2]
    ncpus = sys.argv[3]
    name = sys.argv[4]
    # Run tagging: append SYLVAN_RUN_NAME (exported by annotate.sh/filter.sh as
    # the run dir basename) so fleet child jobs carry their run in -J, e.g.
    # "pasa." -> "pasa.Ahy_C" / "psiClass_bin.binid=Ath_Chr1" -> "...Chr1.Ahy_C".
    run_tag = os.environ.get("SYLVAN_RUN_NAME", "").strip()
    if run_tag:
        name = name.rstrip(".") + "." + run_tag
    account = sys.argv[5]
    partition = sys.argv[6]
    time_limit = sys.argv[7]
    output = sys.argv[8]
    error = sys.argv[9]
    # Snakemake appends the job script as the last positional argument.
    # Everything between error and the job script is extra_args or the
    # explicitly marked serialized field from the shared entry template.
    jobscript = sys.argv[-1]
    extra_args = sys.argv[10:-1]
    # The shared entry template quotes the whole extra_args field. Keep direct
    # callers with already separated flags compatible as well.
    if extra_args and extra_args[0] == '--extra-args-string':
        if len(extra_args) != 2:
            print('ERROR: expected one quoted extra_args value', file=sys.stderr)
            sys.exit(2)
        try:
            extra_args = shlex.split(extra_args[1])
        except ValueError as exc:
            print(f'ERROR: invalid extra_args quoting: {exc}', file=sys.stderr)
            sys.exit(2)

    cmd = [
        "sbatch",
        "-N", nodes,
        "--mem=" + memory,
        "--cpus-per-task=" + ncpus,
        "-J", name,
        "--parsable",
        "-t", time_limit,
        "-o", output,
        "-e", error,
    ]

    if account.lower() not in SKIP_VALUES:
        cmd.extend(["-A", account])
    if partition.lower() not in SKIP_VALUES:
        cmd.extend(["-p", partition])

    # Preserve already-tokenized values, including an explicitly empty value.
    cmd.extend(extra_args)

    # Parse options before the jobscript, which is never an option value.
    try:
        effective_time = effective_option(cmd, "-t", "--time", time_limit)
        effective_partition = effective_option(cmd, "-p", "--partition", partition)
        print(f"cluster_submit: partition={effective_partition or '<default>'} "
              f"time={effective_time} job={name} jobscript={os.path.abspath(jobscript)}", file=sys.stderr)
        check_partition_time(effective_partition, effective_time)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(2)

    cmd.append(jobscript)

    # Attempt the submission, retrying transient failures with exponential backoff
    # (initial try + one retry per RETRY_BACKOFFS entry). Snakemake treats any
    # non-zero exit from this script as a permanent submission failure, so a brief
    # controller hiccup must not be allowed to kill the job outright.
    result = None
    for attempt, backoff in enumerate((0,) + RETRY_BACKOFFS):
        if backoff:
            time.sleep(backoff)
        try:
            result = subprocess.run(cmd, capture_output=True, text=True)
        except OSError as exc:
            # e.g. sbatch not found on PATH, or a transient fork/resource error.
            sys.stderr.write(
                f"cluster_submit: sbatch invocation failed "
                f"(attempt {attempt + 1}/{len(RETRY_BACKOFFS) + 1}): {exc}\n"
            )
            result = None
            continue
        if result.returncode == 0:
            break
        sys.stderr.write(
            f"cluster_submit: sbatch exited {result.returncode} "
            f"(attempt {attempt + 1}/{len(RETRY_BACKOFFS) + 1})\n"
        )

    if result is None:
        # Every attempt raised before sbatch produced a result.
        sys.exit(1)

    sys.stdout.write(result.stdout)
    if result.stderr:
        sys.stderr.write(result.stderr)
    sys.exit(result.returncode)


if __name__ == "__main__":
    main()
