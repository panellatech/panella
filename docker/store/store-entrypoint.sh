#!/bin/sh
set -eu

python /usr/local/share/panella/embedding_preflight.py
exec "$@"
