#!/bin/bash
set -e

mkdir -p /data/.hermes/sessions
mkdir -p /data/.hermes/skills
mkdir -p /data/.hermes/workspace
mkdir -p /data/.hermes/pairing

# Default terminal backend to local unless already configured
ENV_FILE=/data/.hermes/.env
if [ ! -f "$ENV_FILE" ] || ! grep -q "^TERMINAL_ENV=" "$ENV_FILE"; then
    echo "TERMINAL_ENV=local" >> "$ENV_FILE"
fi

exec python /app/server.py
