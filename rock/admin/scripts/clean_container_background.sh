#!/bin/bash

# Watchdog: poll until the actor process disappears, then stop the container.
# Runs as the foreground process of the Popen call in SandboxActor so the
# Python side can terminate it via process.kill() during stop/restart.
PID=$1
CONTAINER_NAME=$2
echo "ray actor pid is $PID"

while [ -e /proc/$PID ]; do
    sleep 1
done

docker stop $CONTAINER_NAME
