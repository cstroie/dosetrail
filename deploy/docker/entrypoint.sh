#!/bin/sh
# Migrate and seed (both idempotent) before the web service starts, then run the command.
# DOSIMETRIX_SKIP_MIGRATE=1 skips it (the receiver / qr / his containers set it).
set -e
if [ "$1" = "serve" ] && [ -z "$DOSIMETRIX_SKIP_MIGRATE" ]; then
    dosimetrix migrate
    dosimetrix seed
fi
exec dosimetrix "$@"
