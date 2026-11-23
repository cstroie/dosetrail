#!/bin/sh
# Migrate and seed (both idempotent) before the web service starts, then run the command.
# The configuration is /etc/dosimetrix/dosimetrix.toml (mounted).
set -e
if [ "$1" = "serve" ]; then
    dosimetrix migrate
    dosimetrix seed
fi
exec dosimetrix "$@"
