# Notebook Culler

[![ci](https://github.com/gnush/kubeflow-notebook-culler/actions/workflows/ci.yml/badge.svg)](https://github.com/gnush/kubeflow-notebook-culler/actions/workflows/ci.yml)

Stops idle Kubeflow notebooks by monitoring CPU, GPU, network, and filesystem usage metrics via Prometheus.

## Purpose

This script automatically identifies and stops idle Kubeflow notebooks to optimize cluster resource utilization. It:

- Queries Prometheus for CPU, GPU, network, and filesystem metrics using a sliding window approach
- Searches for peak utilization over a configurable timeframe to avoid culling notebooks with sporadic activity
- Evaluates notebook activity against configurable thresholds
- Skips notebooks that are already stopped, recently created, or recently restarted
- Applies the `kubeflow-resource-stopped` annotation to idle notebooks
- Outputs debug information including total notebooks, active count, fresh count, idle count, and idle rate

## Usage

Run the script in a Kubernetes environment with access to:
- Kubeflow Notebook CRDs (cluster-scoped)
- Prometheus metrics endpoint
- Kubernetes API (for patching notebook annotations)

The script is designed to run as a periodic cron job or Kubernetes CronJob to continuously cull idle notebooks.

### Execute Dry-Run Locally

`PROMETHEUS_URL` is required and must point at a Prometheus that scrapes the
notebook pods. Examples, depending on how it is deployed:

| Setup | URL |
|-------|-----|
| kube-prometheus-stack in namespace `monitoring` | `http://prometheus-k8s.monitoring.svc:9090` |
| Rancher Monitoring (`cattle-monitoring-system`) | `http://rancher-monitoring-prometheus.cattle-monitoring-system.svc:9090` |
| via port-forward | `http://localhost:9090` |

```bash
kubectl -n monitoring port-forward svc/prometheus-k8s 9090:9090
PROMETHEUS_URL=http://localhost:9090 DRY_RUN=true python3 culler.py
```

### Deploy

The repo root is a kustomize base: ServiceAccount, ClusterRole and
ClusterRoleBinding, and the CronJob, all in namespace `kubeflow`. Prometheus
must be reachable from the cluster.

```bash
kubectl apply -k .
```

From a release tag:

```bash
kubectl apply -k 'github.com/gnush/kubeflow-notebook-culler?ref=<version>'
```

The base has `DRY_RUN=true` and a placeholder `PROMETHEUS_URL`. Set both, and
the schedule or thresholds if they differ, in an overlay:

```yaml
resources:
  - github.com/gnush/kubeflow-notebook-culler?ref=<version>
patches:
  - target: {kind: CronJob, name: kubeflow-notebook-culler}
    patch: |-
      apiVersion: batch/v1
      kind: CronJob
      metadata: {name: kubeflow-notebook-culler}
      spec:
        jobTemplate:
          spec:
            template:
              spec:
                containers:
                  - name: culler
                    env:
                      - {name: PROMETHEUS_URL, value: "http://prometheus-k8s.monitoring.svc:9090"}
                      - {name: DRY_RUN, value: "false"}
```

`env` in a patch merges by name, so only the changed variables are listed. A
different image is `kustomize edit set image
ghcr.io/gnush/kubeflow-notebook-culler=<image>`.

#### GitOps

Flux and Argo CD build the root, so `path: .` installs what `apply -k .` does;
pin a release tag in `targetRevision` / `ref.tag`. Fleet lists `paths: [k8s]`
instead of the root. Per-cluster values go through `spec.images` and
`spec.patches` (Argo CD: `source.kustomize.*`), not through this repo: the image
tag is the release.

## Configuration

Configure behavior via environment variables:

| Variable | Description | Default |
|----------|-------------|---------|
| `PROMETHEUS_URL` | **Required.** Prometheus server URL for querying metrics (see [examples](#execute-dry-run-locally)) | — |
| `IDLE_TIMEFRAME` | Total time frame for idle detection (Prometheus duration format: `60m`, `1h`, `2d`, etc.) | `24h` |
| `IDLE_WINDOW` | Sliding window for idle detection (Prometheus duration format: `60m`, `1h`, `2d`, etc.) | `15m` |
| `NEW_GRACE_PERIOD` | Safety buffer for newly created notebooks (Prometheus duration format). Notebooks created within this window are not culled. | `24h` |
| `RESTART_GRACE_PERIOD` | Safety buffer for restarted notebooks (Prometheus duration format). Notebooks restarted within this window are not culled. | `6h` |
| `IGNORE_NAMESPACES` | Namespaces to ignore (comma-separated). Notebooks in these namespaces are skipped. | `` |
| `IGNORE_NOTEBOOKS` | Notebooks to ignore (format: `namespace:notebook,namespace:notebook,...`). Specific notebooks to skip. | `` |
| `CPU_THRESHOLD` | CPU usage below which a notebook is considered idle (total cpu seconds over IDLE_WINDOW, e.g. 60 = 1 core for 1 minute) | `5` |
| `GPU_THRESHOLD` | GPU engine activity ratio below which a notebook is considered idle (1.0 = 100%) | `0.001` |
| `GPU_THRESHOLD_AGGRESIVE` | GPU threshold when GPU_AGGRESIVE is enabled (1.0 = 100%) | `0.0` |
| `GPU_AGGRESIVE` | When enabled, notebooks with GPUs are culled based solely on GPU_THRESHOLD_AGGRESIVE, ignoring other metrics | `true` |
| `NETWORK_THRESHOLD` | Network activity below which a notebook is considered idle (total bytes over IDLE_WINDOW) | `10000` |
| `FILESYSTEM_THRESHOLD` | Filesystem activity below which a notebook is considered idle (total bytes over IDLE_WINDOW) | `10` |
| `DEBUG` | Debug level: `NONE`, `ERROR`, `ACTION`, `INFO` | `INFO` |
| `INFO_MULTILINE` | Info output format: `true` for multiline, `false` for single-line | `false` |
| `DRY_RUN` | Dry run mode: `true` to only log actions without applying them | `false` |

### Metric Details

The script uses a sliding window approach to detect peak utilization over a longer timeframe:

- **IDLE_TIMEFRAME**: The total time period to evaluate (e.g., 24h)
- **IDLE_WINDOW**: The sliding window size within the timeframe (e.g., 15m)
- The script slides the window across the entire timeframe and takes the maximum value found

This ensures that notebooks with any significant activity during the evaluation period are not culled, even if they were idle at the exact moment the script ran.

**CPU**: Uses `container_cpu_usage_seconds_total`, excluding istio-proxy containers. This detects the maximum CPU seconds accumulated in any sliding window over the entire timeframe.

**GPU**: Uses `DCGM_FI_PROF_GR_ENGINE_ACTIVE` (A100/H100/B300 hardware engine active ratio). This detects the maximum GPU engine activity over the entire timeframe. When `GPU_AGGRESIVE` is enabled, notebooks with GPUs are culled based solely on `GPU_THRESHOLD_AGGRESIVE`, ignoring other metrics.

**Network**: Uses `container_network_receive_bytes_total + container_network_transmit_bytes_total` to detect the maximum network activity in any sliding window.

**Filesystem**: Uses `container_fs_reads_bytes_total + container_fs_writes_bytes_total` to detect the maximum filesystem I/O in any sliding window.

- Notebooks without GPU metrics are evaluated based on CPU, network, and filesystem thresholds only
- Notebooks with query errors are conservatively assumed to be not idle
