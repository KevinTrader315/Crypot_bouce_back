#!/bin/bash
set -e
export PYTHONUNBUFFERED=1

if [ -n "$KALSHI_PRIVATE_KEY" ]; then
    printf '%b\n' "$KALSHI_PRIVATE_KEY" > /app/kalshi_key.pem
    chmod 600 /app/kalshi_key.pem
    export KALSHI_PRIVATE_KEY_PATH=/app/kalshi_key.pem
fi

DOLLARS_FLAG="--trade-dollars ${TRADE_DOLLARS:-15}"
CONV_FLAG="--min-conviction ${MIN_CONVICTION:-4}"

KILL_FLAG=""
if [ -n "${KILL_HOURS:-}" ]; then
    KILL_FLAG="--kill-hours ${KILL_HOURS}"
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
  $DOLLARS_FLAG \
  $CONV_FLAG \
  $SL_FLAG \
  $KILL_FLAG
