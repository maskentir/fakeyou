#!/bin/sh
set -eu

mkdir -p "$XDG_RUNTIME_DIR"
chmod 700 "$XDG_RUNTIME_DIR"
pulseaudio --daemonize=yes --exit-idle-time=-1 --use-pid-file=yes --log-target=stderr -nF /etc/fakeyou/default.pa

attempts=0
until pactl info >/dev/null 2>&1; do
  attempts=$((attempts + 1))
  if [ "$attempts" -ge 50 ]; then
    echo "fakeyou-entrypoint: pulseaudio did not become ready" >&2
    exit 1
  fi
  sleep 0.2
done

child=0
stop() {
  if [ "$child" -ne 0 ]; then
    kill -TERM "$child" 2>/dev/null || true
  fi
}
trap stop TERM INT
/entrypoint.sh "$@" &
child=$!
if wait "$child"; then
  status=0
else
  status=$?
fi
child=0
trap - TERM INT
pulseaudio --kill >/dev/null 2>&1 || true
exit "$status"
