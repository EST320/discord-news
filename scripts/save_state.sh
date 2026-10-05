#!/usr/bin/env bash
# Publish tracker state files to the `state` branch.
#
# Usage: bash scripts/save_state.sh <file>...    (file names inside state/)
#
# The branch is kept at a single commit so it never grows. Each save takes the
# tree currently on the remote, replaces only the files named here, wraps the
# result in a parentless commit and swaps it in with a compare-and-swap push.
# If another tracker pushed in the meantime the swap is rejected and the save
# is retried on top of the new remote tree, so trackers never overwrite each
# other's files.
set -euo pipefail

cd "$(dirname "$0")/../state"

export GIT_AUTHOR_NAME="EST"
export GIT_AUTHOR_EMAIL="55035415+EST320@users.noreply.github.com"
export GIT_COMMITTER_NAME="$GIT_AUTHOR_NAME"
export GIT_COMMITTER_EMAIL="$GIT_AUTHOR_EMAIL"

for attempt in 1 2 3 4 5; do
  git fetch --quiet --depth=1 origin state
  base=$(git rev-parse FETCH_HEAD)

  git read-tree "$base"
  git add -- "$@"
  tree=$(git write-tree)

  if [ "$tree" = "$(git rev-parse "$base^{tree}")" ]; then
    echo "State unchanged, nothing to save."
    exit 0
  fi

  commit=$(git commit-tree "$tree" -m "Tracker state" -m "Last updated by: ${GITHUB_WORKFLOW:-manual run}")
  if git push --quiet --force-with-lease="state:$base" origin "$commit:refs/heads/state"; then
    echo "State saved: $*"
    exit 0
  fi

  echo "State branch moved during save (attempt $attempt), retrying."
  sleep $((RANDOM % 5 + 1))
done

echo "Could not save state after 5 attempts." >&2
exit 1
