#!/bin/sh
# Put the real Twilio number on the public opt-in page.
# Usage: ./set-number.sh "(555) 123-4567"
set -eu
[ $# -eq 1 ] || { echo "usage: $0 <phone number as it should be displayed>" >&2; exit 1; }
sed -i '' -e "s/TEXT_NUMBER/$1/g" docs/join.html
echo "Set. Remaining placeholders:"
grep -o "TEXT_NUMBER" docs/join.html || echo "  none"
