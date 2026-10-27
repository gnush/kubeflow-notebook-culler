from enum import IntEnum
import os
import time
from datetime import datetime, timezone
from typing import NamedTuple
import requests
from kubernetes import client, config
from kubernetes.client.rest import ApiException

# --- Configuration Constants ---
PROMETHEUS_URL = os.getenv("PROMETHEUS_URL")
if not PROMETHEUS_URL:
  raise SystemExit("PROMETHEUS_URL is required")
IDLE_TIMEFRAME = os.getenv("IDLE_TIMEFRAME", "24h")                             # Total time frame for idle detection (Prometheus duration format)
IDLE_WINDOW = os.getenv("IDLE_WINDOW", "15m")                                   # Sliding window for idle detection (Prometheus duration format)
NEW_GRACE_PERIOD = os.getenv("NEW_GRACE_PERIOD", "24h")                         # Safety buffer for new and restarted notebooks (Prometheus duration format)
RESTART_GRACE_PERIOD = os.getenv("RESTART_GRACE_PERIOD", "6h")                  # Safety buffer for new and restarted notebooks (Prometheus duration format)
GPU_AGGRESIVE = os.getenv("GPU_AGGRESIVE", "true").lower() == "true"            # Aggressive idle detection for Notebooks with GPUs (when enabled, notebooks below GPU_THRESHOLD_AGGRESIVE will be culled even if they have other (non-gpu) utilization)
CPU_THRESHOLD = float(os.getenv("CPU_THRESHOLD", "5"))                          # Idle core baseline (total cpu seconds over the idle window, e.g. 60 = 1 core for 1 minute or 2 cores for 30 seconds or 0.1 cores for 10 minutes)
GPU_THRESHOLD = float(os.getenv("GPU_THRESHOLD", "0.001"))                      # hardware engine activity (1.0 = 100%)
GPU_THRESHOLD_AGGRESIVE = float(os.getenv("GPU_THRESHOLD_AGGRESIVE", "0.0"))    # hardware engine activity for aggressive mode (1.0 = 100%)
NETWORK_THRESHOLD = float(os.getenv("NETWORK_THRESHOLD", "10000"))              # network activity (total bytes over IDLE_WINDOW)
FILESYSTEM_THRESHOLD = float(os.getenv("FILESYSTEM_THRESHOLD", "10"))           # filesystem activity (total bytes over IDLE_WINDOW)
DEBUG = os.getenv("DEBUG", "INFO").upper()                                      # Debug level: none, error, action, info
INFO_MULTILINE = os.getenv("INFO_MULTILINE", "false").lower() == "true"         # Info output in multiline format
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"                       # Dry run mode
IGNORE_NAMESPACES = os.getenv("IGNORE_NAMESPACES", "")                          # Namespaces to ignore (comma-separated)
IGNORE_NOTEBOOKS = os.getenv("IGNORE_NOTEBOOKS", "")                            # Notebooks to ignore (format: namespace:notebook,namespace:notebook,...)

class IgnoreNotebook(NamedTuple):
  namespace: str
  notebook: str

ignored_namespaces = {
    namespace.strip()
    for namespace in IGNORE_NAMESPACES.split(",")
    if namespace.strip()
}

ignored_notebooks = {
    IgnoreNotebook(namespace.strip(), notebook.strip())
    for item in IGNORE_NOTEBOOKS.split(",")
    if item.strip()
    for notebook, namespace in [item.split(":", 1)]
}

class DebugLevel(IntEnum):
  NONE = 0
  ERROR = 1
  ACTION = 2
  INFO = 3

try:
  DEBUG_LEVEL = DebugLevel[DEBUG]
except KeyError:
  DEBUG_LEVEL = DebugLevel.INFO

def parse_duration_to_minutes(duration_str: str) -> int:
  """Parse Prometheus duration string (e.g., '60m', '1h', '2d') to minutes."""
  duration_str = duration_str.strip()
  if duration_str.endswith('s'):
    return int(duration_str[:-1]) / 60
  elif duration_str.endswith('m'):
    return int(duration_str[:-1])
  elif duration_str.endswith('h'):
    return int(duration_str[:-1]) * 60
  elif duration_str.endswith('d'):
    return int(duration_str[:-1]) * 60 * 24
  else:
    # Assume minutes if no unit specified
    return int(duration_str)

def convert_seconds_to(seconds: float, unit: str) -> float:
  """Convert seconds to the specified unit."""
  match unit:
    case "s": return seconds
    case "m": return seconds / 60
    case "h": return seconds / (60 * 60)
    case "d": return seconds / (24 * 60 * 60)
    case   _: return seconds

# Sentinel Objects
NO_METRIC = object()
QUERY_ERROR = object()

# Initialize K8s Client
try:
  config.load_incluster_config()
except config.ConfigException:
  config.load_kube_config()

core_api = client.CoreV1Api()
custom_api = client.CustomObjectsApi()

def debug_message(
  *strs: str,
  level: DebugLevel,
  header: str | None = None,
  multilineHeaderSeparator: str = ":\n",
  multilineItemSeparator: str = "\n",
  singlelineItemSeparator: str = ", ",
  singlelineHeaderSeparator: str = " -> "
  ) -> None:
    if strs and DEBUG_LEVEL >= level:
      item_separator = multilineItemSeparator if INFO_MULTILINE else singlelineItemSeparator
      stats = item_separator.join(strs)

      if header is None:
        print(stats)
      else:
        header_separator = multilineHeaderSeparator if INFO_MULTILINE else singlelineHeaderSeparator
        print(f"{header}{header_separator}{stats}")

def query_prometheus(query: str) -> float | object:
  """Queries Prometheus and returns a float value, or NO_METRIC if no metrics exist."""
  try:
    response = requests.get(f"{PROMETHEUS_URL}/api/v1/query", params={"query": query}, timeout=10)
    response.raise_for_status()
    results = response.json().get("data", {}).get("result", [])
    if results:
      return float(results[0]["value"][1])
    return NO_METRIC # if no metrics are returned (e.g., no GPU attached)
  except Exception as e:
    debug_message(f"Error querying Prometheus: {e}", level=DebugLevel.ERROR)
    return QUERY_ERROR

def is_ignored(namespace: str, notebook: str) -> bool:  
  """Checks if a notebook is in the ignore list."""
  return namespace in ignored_namespaces or IgnoreNotebook(notebook, namespace) in ignored_notebooks

def is_notebook_idle(name: str, namespace: str) -> bool:
  """
  Checks if a Kubeflow Notebook is idle based on CPU, GPU, Network, and Filesystem usage.
  
  Args:
    name: The name of the Kubeflow Notebook.
    namespace: The namespace of the Kubeflow Notebook.
  
  Returns:
    True if the Notebook is idle, False otherwise.
  """
  pod_name = f"{name}-0"  # Kubeflow Notebook pods always use the -0 suffix
  ns = f'namespace="{namespace}"'
  pod = f'pod="{pod_name}"'
  container = 'container!~"istio-.*"'
  
  # 1. Query CPU (Excluding proxy noise)
  cpu_query = f'sum(max_over_time(increase(container_cpu_usage_seconds_total{{{ns}, {pod}, {container}}}[{IDLE_WINDOW}])[{IDLE_TIMEFRAME}:{IDLE_WINDOW}]))'
  cpu_usage = query_prometheus(cpu_query)
  
  # 2. Query GPU/MIG (A100/H100/B300 hardware engine active ratio)
  # max_over_time returns the maximum value over the time window (if the GPU was active at any point, conservative)
  # consider switching to avg_over_time for more aggressive culling
  gpu_query = f'sum(max_over_time(DCGM_FI_PROF_GR_ENGINE_ACTIVE{{{ns}, {pod}}}[{IDLE_TIMEFRAME}]))'
  gpu_usage = query_prometheus(gpu_query)

  # 3. Query Network (Bytes received and sent)
  network_query = (
    'max_over_time('
      'sum('
        f'increase(container_network_receive_bytes_total{{{ns}, {pod}}}[{IDLE_WINDOW}])'
        ' + '
        f'increase(container_network_transmit_bytes_total{{{ns}, {pod}}}[{IDLE_WINDOW}])'
      f')[{IDLE_TIMEFRAME}:{IDLE_WINDOW}]'
    ')'
  )
  network_usage = query_prometheus(network_query)

  # 4. Query Filesystem IO
  storage_query = (
    'max_over_time('
      '('
        'sum('
          f'increase(container_fs_reads_bytes_total{{{ns}, {pod}, {container}}}[{IDLE_WINDOW}])'
          ' + '
          f'increase(container_fs_writes_bytes_total{{{ns}, {pod}, {container}}}[{IDLE_WINDOW}])'
        ')'
      f')[{IDLE_TIMEFRAME}:{IDLE_WINDOW}]'
    ')'
  )
  storage_usage = query_prometheus(storage_query)
  
  # If we couldn't get metrics, assume not idle
  if (cpu_usage is QUERY_ERROR or cpu_usage is NO_METRIC
      or network_usage is QUERY_ERROR or network_usage is NO_METRIC
      or storage_usage is QUERY_ERROR or storage_usage is NO_METRIC
      or gpu_usage is QUERY_ERROR):
    return False

  time_unit = IDLE_WINDOW[-1:]
  stats = [
    f"CPU: {convert_seconds_to(cpu_usage, time_unit):.3f}{time_unit}/{IDLE_WINDOW}",
    f"CPU (raw): {cpu_usage:.2f}s",
    f"Network: {network_usage:.0f} B/{IDLE_WINDOW}",
    f"FS: {storage_usage:.0f} B/{IDLE_WINDOW}"
  ]
  if gpu_usage != NO_METRIC and gpu_usage is not QUERY_ERROR:
    stats.append(f"GPU Engine: {gpu_usage:.3f}")

  debug_message(
    *stats,
    level=DebugLevel.INFO,
    header=f"Notebook stats for '{name}' in namespace '{namespace}'" if INFO_MULTILINE else f"{namespace}[{name}]"
  )

  if GPU_AGGRESIVE and gpu_usage != NO_METRIC:
    return gpu_usage <= GPU_THRESHOLD_AGGRESIVE

  return (cpu_usage <= CPU_THRESHOLD
          and network_usage <= NETWORK_THRESHOLD 
          and storage_usage <= FILESYSTEM_THRESHOLD
          and (True if gpu_usage is NO_METRIC else gpu_usage <= GPU_THRESHOLD))

def main():
  # Check Prometheus reachability
  try:
    response = requests.get(f"{PROMETHEUS_URL}/api/v1/query", params={"query": "up"}, timeout=10)
    response.raise_for_status()
  except Exception as e:
    debug_message(f"Prometheus is not reachable at {PROMETHEUS_URL}: {e}", level=DebugLevel.ERROR)
    return
  
  # Fetch ONLY Kubeflow Notebook objects
  try:
    notebooks = custom_api.list_cluster_custom_object(
      group="kubeflow.org", version="v1", plural="notebooks"
    )
  except Exception as e:
    if DEBUG_LEVEL >= DebugLevel.ERROR:
      print(f"Failed to list Kubeflow notebooks: {e}")
    return

  now = datetime.now(timezone.utc)
  idle_count = 0
  active_count = 0
  fresh_count = 0

  for notebook in notebooks.get("items", []):
    name = notebook["metadata"]["name"]
    namespace = notebook["metadata"]["namespace"]
    annotations = notebook["metadata"].get("annotations", {})
    
    # Skip if the notebook is not running
    if "kubeflow-resource-stopped" in annotations:
      continue

    active_count += 1

    # Skip notebooks created within the new notebook grace period
    creation_ts = notebook["metadata"]["creationTimestamp"]
    created_time = datetime.fromisoformat(creation_ts)
    age_minutes = (now - created_time).total_seconds() / 60

    if age_minutes < parse_duration_to_minutes(NEW_GRACE_PERIOD):
      fresh_count += 1
      continue

    # Skip notebooks in the ignore list
    if is_ignored(namespace=namespace, notebook=name):
      continue

    # Skip notebooks restarted within the notebook restart grace period
    try:
      pod_name = f"{name}-0"  # Kubeflow Notebook pods always use the -0 suffix
      pod = core_api.read_namespaced_pod(name=pod_name, namespace=namespace)
      start_ts = pod.metadata.creation_timestamp
      age_minutes = (now - start_ts).total_seconds() / 60

      if age_minutes < parse_duration_to_minutes(RESTART_GRACE_PERIOD):
        fresh_count += 1
        continue
    except ApiException as e:
      if e.status == 404:
        # Pod not found, skip this notebook
        # E.g. when a notebook just started and the pod is not yet ready
        #      or when the initialization fails
        continue
      raise

    # Evaluate Resource Metrics
    if is_notebook_idle(name, namespace):
      idle_count += 1
      cull_time = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

      # Honoured by the Kubeflow notebook controller
      patch_body = {
        "metadata": {
          "annotations": {
            "kubeflow-resource-stopped": cull_time
          }
        }
      }
      
      if not DRY_RUN:
        try:
          custom_api.patch_namespaced_custom_object(
              group="kubeflow.org", version="v1", namespace=namespace,
              plural="notebooks", name=name, body=patch_body
          )
          debug_message(f"Idle threshold reached. Applied stop annotation to notebook '{name}' in namespace '{namespace}'", level=DebugLevel.ACTION)
        except client.exceptions.ApiException as e:
          debug_message(f"Idle threshold reached. Failed to patch notebook '{name}' in namespace '{namespace}': {e}", level=DebugLevel.ERROR)
      else:
        debug_message(f"Idle threshold reached. Would apply stop annotation to notebook '{name}' in namespace '{namespace}'", level=DebugLevel.ACTION)

  debug_message(
    *[
      f"Total: {len(notebooks.get('items', []))}",
      f"Active: {active_count}",
      f"Fresh: {fresh_count}",
      f"Idle: {idle_count}",
      f"Idle rate: {(idle_count / active_count) * 100:.2f}%"
    ],
    level=DebugLevel.INFO,
    header=f"Notebooks Summary" if INFO_MULTILINE else None,
    multilineItemSeparator="\n\t",
    multilineHeaderSeparator=":\n\t"
  )

if __name__ == "__main__":
    main()