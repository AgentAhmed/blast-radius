#!/bin/sh
# Start FalkorDB in the background, wait until it answers, then run the app in the foreground.
set -e
redis-server --protected-mode no --port 6379 --bind 127.0.0.1 --dir "$FALKORDB_DATA_PATH" \
  --loadmodule /var/lib/falkordb/bin/falkordb.so $FALKORDB_ARGS --daemonize yes
until redis-cli -p 6379 ping >/dev/null 2>&1; do sleep 0.5; done
exec uvicorn blastradius.api:app --host 0.0.0.0 --port "${PORT:-7860}"
