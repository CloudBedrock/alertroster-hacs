#!/usr/bin/env bash
# List AlertRoster receiver stations on the LAN and say whether each answers.
#
# Three things this exists to stop you assuming:
#   * the port is NOT always 4747 -- read it off the announcement (the `om`
#     station runs on 4798)
#   * a station announces on every interface it has, so one station is many
#     mDNS records; they are collapsed to one row per name+port here
#   * there is no unauthenticated endpoint yet -- GET /v1/discover is a 404 as
#     of 2026-08-28 (REQUIREMENTS.md §5 item 1). A live station answers
#     401 invalid_credentials, so 401 here means reachable, not broken.
set -euo pipefail

command -v avahi-browse >/dev/null || { echo "avahi-browse not found (install avahi)" >&2; exit 1; }

records=$(avahi-browse -rtp _alertroster-receiver._tcp 2>/dev/null \
  | awk -F';' '$1=="=" && $3=="IPv4" {print $4"\t"$9"\t"$8}' | sort -u)

[ -z "$records" ] && { echo "No stations announcing _alertroster-receiver._tcp on this LAN."; exit 0; }

printf '%-12s %-18s %-6s %s\n' NAME ADDRESS PORT '/v1/status'

printf '%s\n' "$records" | cut -f1,2 | sort -u | while IFS=$'\t' read -r name port; do
  addr=""; code="000"
  # Try each announced address; the first one that answers is the useful one.
  for a in $(printf '%s\n' "$records" | awk -F'\t' -v n="$name" -v p="$port" '$1==n && $2==p {print $3}'); do
    addr="${addr:-$a}"
    c=$(curl -sS -o /dev/null -m 3 -w '%{http_code}' "http://$a:$port/v1/status" 2>/dev/null || true)
    if [ "$c" != "000" ]; then addr="$a"; code="$c"; break; fi
  done
  case "$code" in
    401) verdict="401 reachable (expected -- needs a token)" ;;
    000) verdict="unreachable -- is 'Accept sources from the LAN' on?" ;;
    *)   verdict="http $code" ;;
  esac
  printf '%-12s %-18s %-6s %s\n' "$name" "$addr" "$port" "$verdict"
done
