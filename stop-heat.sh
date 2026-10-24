#!/bin/bash
# Stop the room heater

PIDFILE="/tmp/room-heater.pid"

if [ ! -f "$PIDFILE" ]; then
    echo "❌ Heater is not running (no PID file)"
    exit 1
fi

PID=$(cat "$PIDFILE")

if kill -0 "$PID" 2>/dev/null; then
    # Kill child python processes first, before they get orphaned
    pkill -P "$PID" 2>/dev/null
    # Paused (stopped) processes only act on the kill once resumed
    pkill -CONT -P "$PID" 2>/dev/null
    kill "$PID" 2>/dev/null
    rm -f "$PIDFILE"
    echo "✅ Heater stopped"
else
    echo "❌ Heater process $PID not found (already stopped?)"
    rm -f "$PIDFILE"
    exit 1
fi
