# hecaton dev iteration. See dev/iter.sh + dev/infra.sh for orchestrators.
#
# Day-to-day (app):
#   make dev                       deploy scaffolds + broker; no smoke
#   make dev host=Mi300X           also rsync to Mi300X and run smoke
#   make dev host=Mi300X smoke=run_<scaffold>.py
#                                  pick which smoke script to run
#                                  (default: run_r2egym.py)
#
# Day-to-day (infra):
#   make infra                     re-run changed cluster phases
#                                  (monitoring-core/dashboards/exporters,
#                                   device-plugins, agent-sandbox, sandboxes)
#   ONLY=monitoring-dashboards make infra
#                                  single phase (e.g. dashboard JSON
#                                  reload, ~1s, skips the slow helm step)
#   FORCE_MONITORING_DASHBOARDS=1 make infra
#                                  bypass hash cache
#
# Surgical (app):
#   make dev-scaffold              only stage scaffolds to fleet
#   make dev-broker                only rebuild + redeploy broker
#   make dev-trainer-image host=…  only rebuild trainer base image on host
#   make dev-smoke host=… [smoke=…]
#                                  only run smoke on host (image must exist)
#
# Release (production):
#   make release                   push HEAD on main, wait for CI to
#                                  build ghcr.io broker image, pin
#                                  .env to the new tag, redeploy broker.
#
# Flags (env, not Make vars):
#   SKIP_{SCAFFOLD,BROKER,TRAINER_IMAGE,SMOKE}=1   skip a dev phase
#   FORCE_{SCAFFOLD,BROKER,TRAINER_IMAGE}=1        rebuild even if cache hit
#   SKIP_{MONITORING,DEVICE_PLUGINS,...}=1         skip an infra phase
#   FORCE_{MONITORING,DEVICE_PLUGINS,...}=1        rebuild infra cache hit
#
# Examples:
#   FORCE_BROKER=1 make dev                        bypass broker hash cache
#   SKIP_BROKER=1 make dev host=Mi300X             scaffold + smoke only
#   FORCE_MONITORING=1 make infra                  re-apply Grafana dashboards

.PHONY: dev dev-scaffold dev-broker dev-trainer-image dev-smoke infra release help

_args = $(if $(host),host=$(host)) $(if $(smoke),smoke=$(smoke))

dev:
	@bash dev/iter.sh $(_args)

dev-scaffold:
	@ONLY=scaffold bash dev/iter.sh

dev-broker:
	@ONLY=broker bash dev/iter.sh

dev-trainer-image:
	@ONLY=trainer-image bash dev/iter.sh $(_args)

dev-smoke:
	@ONLY=smoke bash dev/iter.sh $(_args)

infra:
	@bash dev/infra.sh

release:
	@bash dev/release.sh

help:
	@sed -n '1,/^$$/p' Makefile | sed 's/^# \{0,1\}//'
