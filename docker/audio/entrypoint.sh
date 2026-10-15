#!/bin/sh
set -eu

child=0
cleanup() {
  status=$?
  trap - EXIT
  pulseaudio --kill >/dev/null 2>&1 || true
  printf '{"event":"worker_exit","exit_code":%s}\n' "$status" >&2
  exit "$status"
}
trap cleanup EXIT
printf '{"event":"worker_start"}\n' >&2

mkdir -p "$XDG_RUNTIME_DIR"
chmod 700 "$XDG_RUNTIME_DIR"
pulseaudio --daemonize=yes --exit-idle-time=-1 --use-pid-file=yes --log-target=stderr -nF /etc/fakeyou/default.pa

attempts=0
until pactl info >/dev/null 2>&1; do
  attempts=$((attempts + 1))
  if [ "$attempts" -ge 50 ]; then
    echo "fakeyou-entrypoint: pulseaudio did not become ready" >&2
    exit 3
  fi
  sleep 0.2
done

info=$(pactl info 2>/dev/null || true)
if ! printf '%s\n' "$info" | grep -q "Default Sink: meeting_audio"; then
  echo "fakeyou-entrypoint: default sink is not meeting_audio" >&2
  exit 3
fi
if ! printf '%s\n' "$info" | grep -q "Default Source: agent_mic"; then
  echo "fakeyou-entrypoint: default source is not agent_mic" >&2
  exit 3
fi
sinks=$(pactl list short sinks 2>/dev/null || true)
check_sink() {
  name=$1
  spec=$2
  if ! printf '%s\n' "$sinks" | grep -q "$name"; then
    echo "fakeyou-entrypoint: expected PulseAudio sink '$name' is missing" >&2
    exit 3
  fi
  if ! printf '%s\n' "$sinks" | grep "$name" | grep -q "$spec"; then
    echo "fakeyou-entrypoint: PulseAudio sink '$name' is not $spec" >&2
    exit 3
  fi
}
check_sink meeting_audio "16000Hz"
check_sink agent_output "24000Hz"

stop() {
  if [ "$child" -ne 0 ]; then
    kill -TERM "$child" 2>/dev/null || true
  fi
}
trap stop TERM INT
umask 077
/entrypoint.sh "$@" &
child=$!
while :; do
  status=0
  wait "$child" || status=$?
  if [ "$status" -eq 0 ] || ! kill -0 "$child" 2>/dev/null; then
    break
  fi
done
child=0
trap - TERM INT
exit "$status"
