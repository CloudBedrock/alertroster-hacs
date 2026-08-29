# Shared settings for the dev-loop scripts. Override any of these in the
# environment rather than editing this file.
: "${HA_HOST:=ubuntu-dev}"          # ssh target running the HA container
: "${HA_CONTAINER:=homeassistant}"  # container name on that host
: "${HA_SRC:=/opt/homeassistant/src/alertroster}"  # bind-mount source there
: "${HA_URL:=http://ubuntu-dev:8123}"
