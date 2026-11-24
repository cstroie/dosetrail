#!/bin/sh
# Migrate and seed (both idempotent) before the web service starts, then run the command.
# The configuration is /etc/dozimetrix/dozimetrix.toml (mounted).
set -e
if [ "$1" = "serve" ]; then
    dozimetrix migrate
    dozimetrix seed
fi
exec dozimetrix "$@"
