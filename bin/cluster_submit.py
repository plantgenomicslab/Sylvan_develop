#!/usr/bin/env python3
"""SLURM job submission wrapper for Snakemake.

Called by Snakemake via --cluster with {cluster.X} placeholders substituted.
Skips -A (account) and -p (partition) when their values are empty or 'placeholder',
so the same config works on HPC systems that don't require a SLURM account.

Any extra sbatch flags can be passed via {cluster.extra_args} in the config YAML
(e.g., extra_args: "--gres=gpu:1 --export=NONE"). Prefer --export=NONE plus explicit
exports over --export=ALL: propagating the full login environment re-exports PATH into
every child sbatch, and for self-perpetuating controllers that feeds an E2BIG PATH-growth
failure (PATH grows ~60B/generation until it blows MAX_ARG_STRLEN). The shell performs
word-splitting on extra_args before this script receives them, so multi-flag values work
naturally.

A transient sbatch failure (SLURM controller overload, momentary resource error) is
retried with exponential backoff so it doesn't permanently fail the Snakemake job.

Run tagging: when SYLVAN_RUN_NAME is set in the environment (annotate.sh/filter.sh
export it as the run directory basename), it is appended to the job name
("pasa." -> "pasa.Ahy_C"). The 15-run fleet shares rule names across runs, so the
tag is what makes squeue/sacct output attributable to a specific run and lets
controller guards match children safely.

Usage in entry scripts (Snakemake appends the jobscript path as the final argument):
  --cluster "python3 bin/cluster_submit.py {cluster.nodes} {cluster.memory} \
    {cluster.ncpus} {cluster.name} {cluster.account} {cluster.partition} \
    {cluster.time} {cluster.output} {cluster.error} {cluster.extra_args}"
"""

import os
import subprocess
import sys
import time


SKIP_VALUES = {"", "placeholder"}

# Backoff (seconds) before each retry after a failed sbatch. The initial attempt
# plus one retry per entry gives 4 total tries. Only transient failures (non-zero
# exit or a raised OSError) are retried; a successful submit returns immediately.
RETRY_BACKOFFS = (5, 15, 45)


def time_seconds(value):
    """Parse the documented SLURM time forms; None denotes an unlimited request."""
    value = str(value).strip()
    if value.upper() in {"INFINITE", "UNLIMITED", "0"}:
        return None
    days = 0
    if "-" in value:
        day, value = value.split("-", 1)
        days = int(day)
        parts = [int(x) for x in value.split(":")]
        if not 1 <= len(parts) <= 3:
            raise ValueError("invalid day-prefixed time")
        parts += [0] * (3 - len(parts))
        return days * 86400 + parts[0] * 3600 + parts[1] * 60 + parts[2]
    parts = [int(x) for x in value.split(":")]
    if len(parts) == 1:
        return parts[0] * 60
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    raise ValueError("invalid SLURM time")


def effective_option(args, short, long, default):
    """Return the last command-line occurrence, including extra sbatch flags."""
    value = default
    for i, arg in enumerate(args):
        if arg in (short, long) and i + 1 < len(args):
            value = args[i + 1]
        elif arg.startswith(long + "="):
            value = arg.split("=", 1)[1]
    return value


def check_partition_time(partition, requested):
    """Reject a known PartitionTimeLimit before creating a pending job."""
    if partition.lower() in SKIP_VALUES:
        return
    try:
        seconds = time_seconds(requested)
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
    # Warn if time_limit looks like a sexagesimal artifact (large integer)
    if time_limit.isdigit() and int(time_limit) > 1000:
        print(
            f"WARNING: time_limit '{time_limit}' looks like a YAML 1.1 sexagesimal artifact. "
            f"Quote time values in your config (e.g., time: \"3-00:00:00\").",
            file=sys.stderr,
        )
    output = sys.argv[8]
    error = sys.argv[9]
    # Snakemake appends the job script as the last positional argument.
    # Everything between error and the job script is extra_args
    # (already word-split by the shell).
    jobscript = sys.argv[-1]
    extra_args = sys.argv[10:-1]

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

    # Append any extra sbatch flags (e.g., --gres=gpu:1).
    # These are already word-split by the shell, so append directly.
    for arg in extra_args:
        if arg.strip():
            cmd.append(arg)

    cmd.append(jobscript)

    effective_time = effective_option(cmd, "-t", "--time", time_limit)
    effective_partition = effective_option(cmd, "-p", "--partition", partition)
    print(f"cluster_submit: partition={effective_partition or '<default>'} "
          f"time={effective_time} job={name}", file=sys.stderr)
    try:
        check_partition_time(effective_partition, effective_time)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(2)

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
