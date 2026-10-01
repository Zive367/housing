#!/usr/bin/env bash
# One-time setup on a fresh Ubuntu server. From the repository folder:  sudo ./scripts/install.sh
set -euo pipefail
cd "$(dirname "$0")/.."

if ! command -v docker >/dev/null; then
  echo "Installing Docker..."
  curl -fsSL https://get.docker.com | sh
fi

mkdir -p secrets/sessions data
# Docker would create directories for missing mount sources, so create empty files first.
touch .env config.yaml
chmod 600 .env

echo "Building the bot image (a few minutes the first time)..."
docker compose build

if [ ! -s .env ] || [ ! -s config.yaml ]; then
  echo "Answer the setup questions:"
  docker compose run --rm -v "$PWD:/work" -w /work housing-bot python -m housing_bot init
fi

if [ ! -f secrets/google-service-account.json ]; then
  echo
  echo "Copy your Google service account key to $(pwd)/secrets/google-service-account.json, then run this script again."
  exit 1
fi

echo "Checking every connection..."
if ! docker compose run --rm housing-bot python -m housing_bot check; then
  echo
  echo "Fix the FAIL lines above (edit .env or config.yaml), then run this script again."
  exit 1
fi

docker compose up -d
echo
echo "The bot is running in DRY-RUN mode (writes messages, sends nothing)."
echo "  Logs:        docker compose logs -f"
echo "  Go live:     set 'dry_run: false' in config.yaml, then: docker compose restart"
