#!/usr/bin/env bash
set -u

APP_SERVICE="${APP_SERVICE:-fault-hub.service}"
PROXY_SERVICE="${PROXY_SERVICE:-caddy.service}"
REPORTER_SERVICE="${REPORTER_SERVICE:-fault-hub-reporter.service}"
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:8080/api/health}"
services=("$APP_SERVICE" "$PROXY_SERVICE" "$REPORTER_SERVICE")

start_missing_services() {
  for service in "${services[@]}"; do
    if ! systemctl is-active --quiet "$service"; then
      logger -t ops-hub-recovery "Starting $service"
      systemctl start "$service" || true
    fi
  done
}

stack_is_healthy() {
  for service in "${services[@]}"; do
    systemctl is-active --quiet "$service" || return 1
  done
  curl -fsS --max-time 3 "$HEALTH_URL" >/dev/null 2>&1
}

logger -t ops-hub-recovery "Boot recovery started"
start_missing_services

for attempt in $(seq 1 45); do
  if stack_is_healthy; then
    logger -t ops-hub-recovery "Complete stack healthy after $attempt checks"
    exit 0
  fi
  sleep 2
done

logger -t ops-hub-recovery "Stack unhealthy; restarting backend, Caddy and reporter"
systemctl restart "$APP_SERVICE" || true
systemctl restart "$PROXY_SERVICE" || true
systemctl restart "$REPORTER_SERVICE" || true

for attempt in $(seq 1 45); do
  if stack_is_healthy; then
    logger -t ops-hub-recovery "Complete stack recovered after $attempt checks"
    exit 0
  fi
  sleep 2
done

logger -t ops-hub-recovery "Complete stack recovery failed"
exit 1
