# Shared by every Slurm entry script. Snakemake's :q quotes nonempty values;
# the adjacent empty literal keeps even an empty field as one shell argument.
CLUSTER_CMD="python3 bin/cluster_submit.py ''{cluster.nodes:q} ''{cluster.memory:q} ''{cluster.ncpus:q} ''{cluster.name:q} ''{cluster.account:q} ''{cluster.partition:q} ''{cluster.time:q} ''{cluster.output:q} ''{cluster.error:q} --extra-args-string ''{cluster.extra_args:q}"

sylvan_snakemake() {
    python3 "$(dirname "${BASH_SOURCE[0]}")/cluster_launch.py" "$@"
}
