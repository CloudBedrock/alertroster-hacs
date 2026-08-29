# Shared settings for the dev-loop scripts. Override any of these in the
# environment rather than editing this file.
: "${HA_HOST:=ubuntu-dev}"          # ssh target running the HA container
: "${HA_CONTAINER:=homeassistant}"  # container name on that host
: "${HA_SRC:=/opt/homeassistant/src/alertroster}"  # bind-mount source there
: "${HA_URL:=http://ubuntu-dev:8123}"              # how you reach HA from here

# The readiness probe runs ON the rig, so localhost is right here and HA_URL --
# which is how this workstation reaches it -- deliberately is not.
: "${HA_HEALTH_URL:=http://localhost:8123/}"
: "${HA_WAIT_SECONDS:=120}"

# Block until HA answers, or fail loudly. A container that dies during startup
# would otherwise hang the caller forever with nothing on screen.
wait_for_ha() {
  ssh "$HA_HOST" "deadline=\$(( \$(date +%s) + $HA_WAIT_SECONDS ))
    until curl -sS -o /dev/null -m 3 '$HA_HEALTH_URL' 2>/dev/null; do
      if [ \"\$(date +%s)\" -ge \"\$deadline\" ]; then
        echo 'timed out after ${HA_WAIT_SECONDS}s waiting for Home Assistant' >&2
        exit 1
      fi
      sleep 3
    done"
}
