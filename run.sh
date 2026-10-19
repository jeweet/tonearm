#!/bin/sh
# Run from the source tree without installing.
cd "$(dirname "$0")" && exec python3 -m tonearm "$@"
