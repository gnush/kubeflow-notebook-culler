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

Deploy the culler as a Kubernetes CronJob using the provided manifests in the `k8s/` directory. Uses the `kubeflow` namespace.

**Prerequisite**: Prometheus metrics endpoint accessible from the cluster

**Customization**:
In `k8s/crontab.yaml`:
- Modify the `schedule` to change the execution frequency
- Set `PROMETHEUS_URL` to your Prometheus endpoint
- Update the `image` tag to use a different version
- Adjust environment variables in the CronJob spec to tune thresholds and behavior

#### Manual

```bash
kubectl apply -f k8s/service-account.yaml
kubectl apply -f k8s/rbac.yaml
kubectl apply -f k8s/crontab.yaml
```

**To enable actual culling** (disable dry-run):
```bash
kubectl set env cronjob/kubeflow-notebook-culler DRY_RUN=false -n kubeflow
```

#### Fleet

To deploy with Fleet, include the following manifests in your Fleet bundle and apply the bundle to the target cluster:
- `k8s/service-account.yaml`
- `k8s/rbac.yaml`
- `k8s/crontab.yaml`

#### Flux

To deploy with Flux, include the manifests in an existing Flux-managed repository (or create a `Kustomization` that references this repository) and ensure the following manifests are applied:

- `k8s/service-account.yaml`
- `k8s/rbac.yaml`
- `k8s/crontab.yaml`


```yaml
apiVersion: kustomize.toolkit.fluxcd.io/v1
kind: Kustomization
metadata:
  name: kubeflow-notebook-culler
  namespace: flux-system
spec:
  interval: 1h
  path: ./k8s
  prune: true
  sourceRef:
    kind: GitRepository
    name: <your-git-repo>
  healthChecks:
    - apiVersion: batch/v1
      kind: CronJob
      name: kubeflow-notebook-culler
      namespace: kubeflow
```

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
