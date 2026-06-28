#!/bin/sh
if [ ! -f /app/.env ]; then
  echo "No .env file found. Copying .env.example to .env..."
  cp /app/.env.example /app/.env
fi
exec "$@"
