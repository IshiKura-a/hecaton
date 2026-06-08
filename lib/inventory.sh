# Helpers for reading config/hosts.yaml without a YAML dependency.
#
# Layout assumption (kept deliberately simple, parsed with awk):
#
#   hosts:
#     - name: control-plane-1
#       ssh_host: gpu-host-01    # alias in ~/.ssh/config
#       role: server
#       gpu_count: 4             # optional, caps GPUs exposed to k8s
#     - name: worker-1
#       ...
#
# Functions:
#   inventory_path           - prints absolute path to hosts.yaml or dies
#   inventory_hosts          - prints one host name per line
#   inventory_field NAME KEY - prints the value of `key:` under host `name`
#
# We avoid yq/python so bootstrap scripts have no Python prerequisite.

# shellcheck source=common.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

inventory_path() {
  local f="$HECATON_ROOT/config/hosts.yaml"
  [[ -f "$f" ]] || die "missing $f (copy config/examples/hosts.yaml)"
  printf '%s\n' "$f"
}

inventory_hosts() {
  local f
  f="$(inventory_path)"
  awk '
    /^[[:space:]]*-[[:space:]]*name:[[:space:]]*/ {
      sub(/^[[:space:]]*-[[:space:]]*name:[[:space:]]*/, "");
      sub(/[[:space:]]*(#.*)?$/, "");
      print
    }
  ' "$f"
}

inventory_field() {
  local want_name="$1" want_key="$2" f
  f="$(inventory_path)"
  awk -v want_name="$want_name" -v want_key="$want_key" '
    /^[[:space:]]*-[[:space:]]*name:[[:space:]]*/ {
      cur=$0
      sub(/^[[:space:]]*-[[:space:]]*name:[[:space:]]*/, "", cur)
      sub(/[[:space:]]*(#.*)?$/, "", cur)
      in_match = (cur == want_name)
      next
    }
    in_match && $0 ~ "^[[:space:]]+" want_key ":" {
      line=$0
      sub("^[[:space:]]+" want_key ":[[:space:]]*", "", line)
      sub(/[[:space:]]*(#.*)?$/, "", line)
      print line
      exit
    }
  ' "$f"
}
