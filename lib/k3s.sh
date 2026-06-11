# Shared k3s helpers for bootstrap scripts.

# shellcheck source=common.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"
# shellcheck source=inventory.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/inventory.sh"

K3S_DEFAULT_DATA_DIR="/var/lib/rancher/k3s"

k3s_data_dir_for_host() {
  local root
  root="$(inventory_disk_root "$1")"
  if [[ "$root" == "/" ]]; then
    printf '%s\n' "$K3S_DEFAULT_DATA_DIR"
  else
    printf '%s\n' "$root/hecaton/k3s"
  fi
}

k3s_remote_helpers() {
  cat <<'REMOTE_HELPERS'
k3s_configured_data_dir() {
  local svc="$1" text dir
  text="$(systemctl cat "$svc" 2>/dev/null || true; cat "/etc/systemd/system/$svc.service.env" 2>/dev/null || true)"
  dir=""
  if [[ "$text" =~ K3S_DATA_DIR=([^[:space:]]+) ]]; then
    dir="${BASH_REMATCH[1]}"
  elif [[ "$text" =~ --data-dir=([^[:space:]]+) ]]; then
    dir="${BASH_REMATCH[1]}"
  elif [[ "$text" =~ --data-dir[[:space:]]+([^[:space:]]+) ]]; then
    dir="${BASH_REMATCH[1]}"
  fi
  dir="${dir%\"}"; dir="${dir#\"}"
  dir="${dir%\'}"; dir="${dir#\'}"
  printf '%s\n' "${dir:-/var/lib/rancher/k3s}"
}

k3s_validate_disk_root() {
  local disk_root="$1"
  if [[ "$disk_root" != /* ]]; then
    echo "disk_root must be absolute, got '$disk_root'" >&2
    exit 1
  fi
  if [[ "$disk_root" != "/" && ! -d "$disk_root" ]]; then
    echo "disk_root does not exist on host: $disk_root" >&2
    exit 1
  fi
}

k3s_requested_data_dir() {
  local svc="$1" current="${2:-}" disk_root want_data_dir actual_data_dir
  disk_root="${DISK_ROOT_REMOTE:-/}"
  want_data_dir="${K3S_DATA_DIR_REMOTE:-/var/lib/rancher/k3s}"
  k3s_validate_disk_root "$disk_root"

  if [[ -n "$current" && "$disk_root" != "/" ]]; then
    actual_data_dir="$(k3s_configured_data_dir "$svc")"
    if [[ "$actual_data_dir" != "$want_data_dir" ]]; then
      cat >&2 <<EOF
k3s is already installed with data dir $actual_data_dir,
but hosts.yaml requests disk_root=$disk_root -> $want_data_dir.
Automatic migration is not supported. Drain/reinstall this node or migrate the k3s data dir manually.
EOF
      exit 1
    fi
  fi

  printf '%s\n' "$want_data_dir"
}

k3s_data_dir_arg() {
  local want_data_dir="$1"
  if [[ "$want_data_dir" == "/var/lib/rancher/k3s" ]]; then
    return 0
  fi
  sudo mkdir -p "$want_data_dir"
  printf '%s\n' "--data-dir=$want_data_dir"
}
REMOTE_HELPERS
}
