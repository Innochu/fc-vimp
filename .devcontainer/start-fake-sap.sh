#!/usr/bin/env bash
# Start the fake SAP endpoint in the background if it is not already running.
if ! (exec 3<>/dev/tcp/127.0.0.1/9999) 2>/dev/null; then
  nohup python "$(dirname "$0")/fake_sap.py" >/tmp/fake_sap.log 2>&1 &
  sleep 1
fi
