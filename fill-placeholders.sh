#!/bin/sh
# Fill the policy-page placeholders, then delete this script.
# Usage: ./fill-placeholders.sh "Aug 16, 2026" "Clover Leaf Ward Primary" "you@example.com"
set -eu
[ $# -eq 3 ] || { echo "usage: $0 <effective-date> <contact-name> <contact-email>" >&2; exit 1; }
for f in docs/*.html; do
  sed -i '' -e "s/FILL_IN_DATE/$1/g" -e "s/FILL_IN_CONTACT_NAME/$2/g" -e "s/FILL_IN_CONTACT_EMAIL/$3/g" "$f"
done
echo "Filled. Remaining placeholders:"
grep -rno "FILL_IN_[A-Z_]*" docs/ || echo "  none"
