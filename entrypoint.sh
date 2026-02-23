#!/bin/bash
set -e
export PYTHONUNBUFFERED=1

if [ -n "$KALSHI_PRIVATE_KEY" ]; then
    printf '%b\n' "$KALSHI_PRIVATE_KEY" > /app/kalshi_key.pem
    chmod 600 /app/kalshi_key.pem
    export KALSHI_PRIVATE_KEY_PATH=/app/kalshi_key.pem
fi

CONV_FLAG=""
if [ -n "${MIN_CONVICTION:-}" ]; then
    CONV_FLAG="--min-conviction ${MIN_CONVICTION}"
fi

SL_FLAG=""
if [ "${DISABLE_STOP_LOSS:-}" = "true" ]; then
    SL_FLAG="--no-stop-loss"
fi

exec python3 dashboard.py \
  --mode "${BOT_MODE:-paper}" \
  --contracts "${CONTRACTS:-5}" \
  --port 5052 \
  --poll "${POLL_INTERVAL:-15}" \
  $CONV_FLAG \
  $SL_FLAG
