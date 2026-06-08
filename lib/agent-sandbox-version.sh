# Pinned agent-sandbox release. Bump deliberately.
#
# Refers to a release tag of the kubernetes-sigs/agent-sandbox repo. The
# install script fetches the released `manifest.yaml` (core) and
# `extensions.yaml` (SandboxTemplate/Claim/WarmPool) from
# https://github.com/kubernetes-sigs/agent-sandbox/releases/download/<TAG>/
#
# Releases: https://github.com/kubernetes-sigs/agent-sandbox/releases
AGENT_SANDBOX_VERSION="v0.4.6"

# TODO: switch to a fork (https://github.com/IshiKura-a/agent-sandbox) once
# we need to modify the controller code itself. That route requires:
#   1. build the controller image (cmd/agent-sandbox-controller) and push
#      it to a registry the fleet can pull from
#   2. replace the `ko://...` placeholder in k8s/controller.yaml with the
#      pushed image URI
#   3. point the install script at the fork's raw k8s/* manifests rather
#      than the upstream release manifest.yaml + extensions.yaml
#   4. bump the CR versions in platform/broker/broker.py
#      (SB_VERSION / TMPL_VERSION) to whatever the fork ships
#      (currently v1beta1 on main)
# For now we stay on the upstream release because it ships prebuilt
# controller images and lets us avoid maintaining a fork.
