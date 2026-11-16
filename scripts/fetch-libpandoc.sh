#!/usr/bin/env bash
# Download files of libpandoc's release into a directory, and check each:
# against the release's SHA256SUMS (that it arrived whole, and isn't half
# replaced: `continuous` changes asset by asset), and its build provenance
# attestation (that libpandoc's CI workflow built it). A release without
# SHA256SUMS (libpandoc's before 1.12) fails.
#
#   scripts/fetch-libpandoc.sh DIR FILE...
#
# $LIBPANDOC_REPO (default ickc/libpandoc) and $LIBPANDOC_RELEASE (default
# continuous); needs gh, with $GH_TOKEN in CI.
set -euo pipefail
dir=$1
shift
repo=${LIBPANDOC_REPO:-ickc/libpandoc}
release=${LIBPANDOC_RELEASE:-continuous}
mkdir -p "$dir"
patterns=()
for f in "$@"; do patterns+=(-p "$f"); done
gh release download "$release" -R "$repo" -D "$dir" --clobber "${patterns[@]}" -p SHA256SUMS
cd "$dir"
for f in "$@"; do
	want=$(awk -v f="$f" '$2 == f || $2 == "*" f { print $1 }' SHA256SUMS)
	got=$({ sha256sum "$f" 2>/dev/null || shasum -a 256 "$f"; } | cut -d ' ' -f 1)
	if [[ -z $want ]]; then
		echo "$f is not in $release's SHA256SUMS" >&2
		exit 1
	elif [[ $got != "$want" ]]; then
		echo "$f: SHA-256 $got, but SHA256SUMS says $want (the release may be being replaced: run again in a few minutes)" >&2
		exit 1
	fi
	gh attestation verify "$f" -R "$repo" --signer-workflow "$repo/.github/workflows/ci.yml" >/dev/null
	echo "$f: SHA-256 and attestation checked"
done
