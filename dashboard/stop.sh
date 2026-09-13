#!/bin/bash
# Stop script for Quant Trading Dashboard & Sync Daemon

PID_FILE="/app/working/workspaces/default/dashboard/dashboard.pid"
SYNC_PID_FILE="/app/working/workspaces/default/dashboard/sync.pid"

if [ -f "$SYNC_PID_FILE" ]; then
    SPID=$(cat "$SYNC_PID_FILE")
    if kill -0 "$SPID" 2>/dev/null; then
        echo "Stopping Sync Daemon (PID: $SPID) ..."
        kill "$SPID"
        sleep 0.5
        kill -9 "$SPID" 2>/dev/null || true
    fi
    rm -f "$SYNC_PID_FILE"
    echo "✅ Sync daemon stopped."
fi

if [ -f "$PID_FILE" ]; then
    PID=$(cat "$PID_FILE")
    if kill -0 "$PID" 2>/dev/null; then
        echo "Stopping Dashboard (PID: $PID) ..."
        kill "$PID"
        sleep 0.5
        kill -9 "$PID" 2>/dev/null || true
    fi
    rm -f "$PID_FILE"
    echo "✅ Dashboard stopped."
fi
