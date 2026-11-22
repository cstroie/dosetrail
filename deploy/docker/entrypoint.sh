#!/bin/sh
# Migrate and seed (both idempotent) before the web service starts, then run the command.
# DOSETRAIL_SKIP_MIGRATE=1 skips it (the receiver / qr / his containers set it).
set -e
if [ "$1" = "serve" ] && [ -z "$DOSETRAIL_SKIP_MIGRATE" ]; then
    dosetrail migrate
    dosetrail seed
fi
exec dosetrail "$@"
