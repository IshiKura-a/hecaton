# Pinned monitoring stack versions. Bump deliberately.
#
# kube-prometheus-stack bundles Prometheus + Grafana + node-exporter
# + kube-state-metrics + alertmanager (we disable the last via
# platform/monitoring/values.yaml). Chart releases:
#   https://github.com/prometheus-community/helm-charts/releases?q=kube-prometheus-stack
KUBE_PROM_STACK_VERSION="66.3.0"

# NVIDIA GPU metrics. We pull the DaemonSet manifest at this tag from
# the upstream repo and apply it directly (helm chart needs Helm 3.7+
# and adds nothing we use). Tags:
#   https://github.com/NVIDIA/dcgm-exporter/releases
DCGM_EXPORTER_VERSION="3.3.9-3.6.1-ubuntu22.04"

# AMD GPU metrics. amd-smi-exporter from the rocm-k8s repository.
# Tags: https://github.com/ROCm/k8s-device-plugin/releases
AMD_SMI_EXPORTER_VERSION="v1.3.0"
