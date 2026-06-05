#!/bin/bash
# Load environment variables and run the alert script
set -a
source "$(dirname "$0")/.env"
set +a
cd "$(dirname "$0")"
python mon100_alert.py
