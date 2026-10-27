#!/bin/zsh
set -eu
gateway_root=${FUDANGATEWAY_ATRUST_HOME:-"$HOME/Library/Application Support/FudanGateway/atrust"}
gateway_label="gui/$(id -u)/com.biogod.fudan-gateway.atrust"
action=${1:-status}
case "$action" in
  status)
    if [[ -f "$gateway_root/status.json" ]]; then
      /bin/cat "$gateway_root/status.json"
    else
      print 'Service has not been started yet.'
    fi
    /usr/sbin/lsof -nP -iTCP:1080 -sTCP:LISTEN || true
    ;;
  login)
    export FUDANGATEWAY_ATRUST_HOME="$gateway_root"
    "$gateway_root/bin/fudan-login"
    if [[ -f "$gateway_root/client-data.json" ]]; then
      "$0" start
    fi
    ;;
  start)
    if ! /bin/launchctl kickstart "$gateway_label"; then
      /bin/launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.biogod.fudan-gateway.atrust.plist"
    fi
    ;;
  check)
    recoveryVersion=$(/usr/bin/plutil -extract version raw -o - "$gateway_root/status.json" 2>/dev/null || true)
    if [[ "$recoveryVersion" == '1.2' ]] && /bin/launchctl kill SIGUSR1 "$gateway_label" 2>/dev/null; then
      :
    else
      "$gateway_root/bin/fudan-supervisor" -base "$gateway_root" -check
    fi
    ;;
  stop)
    /bin/launchctl bootout "$gateway_label"
    ;;
  *)
    print -u2 'Usage: fudan-gateway.command {status|login|start|stop|check}'
    exit 2
    ;;
esac
