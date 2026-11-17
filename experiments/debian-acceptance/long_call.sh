#!/bin/sh
# ADR-023 amendment acceptance: long confined calls on the real helper (see long_call.py).
# Runs as root on a Debian 13 dev host with the small-host kit's confined workers installed
# (deploy/small-host/README.md, section 4, including the `coding` profile and the
# `code-session` worker). It spends no model usage: the harness is a sleeping command.
#
#   sudo sh experiments/debian-acceptance/long_call.sh [WORK_DIR] [-- long_call.py options]
#
# coding_run.sh installs the fixture, the coding file and the registry alias for the run
# only, and restores the owner's files on exit, also after an interrupted run.
set -eu
exec sh "$(cd "$(dirname "$0")" && pwd)/coding_run.sh" long_call.py "$@"
