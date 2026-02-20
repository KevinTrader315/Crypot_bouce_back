#!/bin/bash
set -e
export PYTHONUNBUFFERED=1

if [ -n "$KALSHI_PRIVATE_KEY" ]; then
    printf '%b\n' "$KALSHI_PRIVATE_KEY" > /app/kalshi_key.pem
    chmod 600 /app/kalshi_key.pem
    export KALSHI_PRIVATE_KEY_PATH=/app/kalshi_key.pem
fi

exec python3 dashboard.py \
  --mode "${BOT_MODE:-paper}" \
  --contracts "${CONTRACTS:-5}" \
  --threshold "${MOVE_THRESHOLD:-8.0}" \
  --port 5052 \
  --poll "${POLL_INTERVAL:-15}"
