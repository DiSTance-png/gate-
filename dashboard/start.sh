#!/bin/bash
# Start script for Quant Trading Dashboard & Sync Daemon

PID_FILE="/app/working/workspaces/default/dashboard/dashboard.pid"
SYNC_PID_FILE="/app/working/workspaces/default/dashboard/sync.pid"
LOG_FILE="/app/working/workspaces/default/logs/dashboard.log"
PORT=8080

PYTHON_BIN="/app/venv/bin/python3"
if [ ! -f "$PYTHON_BIN" ]; then
    PYTHON_BIN="python3"
fi

mkdir -p /app/working/workspaces/default/logs

# Ensure openpyxl is installed
$PYTHON_BIN -c "import openpyxl" 2>/dev/null || /app/venv/bin/pip install openpyxl >/dev/null 2>&1 || true

# 1. Start Web Sync Daemon
if [ -f "$SYNC_PID_FILE" ]; then
    SPID=$(cat "$SYNC_PID_FILE")
    if ! kill -0 "$SPID" 2>/dev/null; then
        rm -f "$SYNC_PID_FILE"
    fi
fi

if [ ! -f "$SYNC_PID_FILE" ]; then
    nohup $PYTHON_BIN /app/working/workspaces/default/scripts/daemon_web_sync.py > /dev/null 2>&1 &
    echo $! > "$SYNC_PID_FILE"
    echo "✅ Web data sync daemon started"
fi

# 2. Start FastAPI Server
if [ -f "$PID_FILE" ]; then
    PID=$(cat "$PID_FILE")
    if kill -0 "$PID" 2>/dev/null; then
        echo "Dashboard is already running (PID: $PID) on port $PORT"
        exit 0
    else
        rm -f "$PID_FILE"
    fi
fi

echo "Starting OKX Quant Dashboard Web Monitor on http://0.0.0.0:$PORT ..."
nohup $PYTHON_BIN -m uvicorn dashboard.app:app --host 0.0.0.0 --port $PORT > "$LOG_FILE" 2>&1 &
PID=$!
echo $PID > "$PID_FILE"
sleep 1.5

if kill -0 "$PID" 2>/dev/null; then
    echo "✅ Dashboard successfully started (PID: $PID) on port $PORT"
else
    echo "❌ Failed to start dashboard. Check $LOG_FILE"
    cat "$LOG_FILE"
    exit 1
fi
