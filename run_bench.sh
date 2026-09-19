#!/bin/bash
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PARENT_DIR="$(dirname "$SCRIPT_DIR")"

export WATCHER_RUNTIME_STATE_ROOT="$PARENT_DIR/.watcherobot_state"
export WATCHER_RUNTIME_INSTANCE_ROOT="$PARENT_DIR/.watcherobot_instance"
export SDK_BENCH_PORT="60604"

source "$PARENT_DIR/.venv/bin/activate"

# Ensure daemon is running
watcherobot daemon start >/dev/null 2>&1

cd "$SCRIPT_DIR"
exec watcherobot app run
