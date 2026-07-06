#!/usr/bin/env bash
set -euo pipefail

# Every commit reachable from HEAD must carry a neutral maintainer identity. This is the
# git-HISTORY half of the identity posture (check-rendered-identity.sh covers rendered
# artifacts): a GitHub-UI merge stamps the pressing user as author (squash) or
# "GitHub <noreply@github.com>" as committer (merge commit), which is exactly how a real
# maintainer identity leaks into public history (it happened once on #12).
#
# What this gate is: a fast-catch BACKSTOP, not a pre-merge button block. The discipline is to
# merge locally under the neutral identity and push; this job ENFORCES that discipline by making
# any violation loud:
#   - on push to a branch (incl. main): scans the pushed history — a GitHub-authored merge/squash
#     commit that lands on main turns main RED immediately, so it is caught and amended before it
#     is forgotten (the #12 save).
#   - on pull_request: scans the PR's OWN commits (its head, not GitHub's synthetic merge commit),
#     so a contributor who committed under a personal identity is caught before merge.
# It cannot turn the GitHub merge BUTTON red: that merge/squash commit does not exist until the
# button is clicked, so no PR check can pre-empt it — the push-to-main scan is the backstop, and
# repo policy (merge locally / disable UI merges) is the prevention.
#
# The allowlist is public-safe: it names only the neutral project identity and GitHub's own
# bot plumbing. It must never grow a personal name/email — that would leak the identity it
# exists to keep out.

allowed_author_re='^(Panella Maintainers <noreply@panella\.tech>|dependabot\[bot\] <[0-9+]*dependabot\[bot\]@users\.noreply\.github\.com>)$'
allowed_committer_re='^(Panella Maintainers <noreply@panella\.tech>|GitHub <noreply@github\.com>)$'

bad=0
while IFS=$'\x1f' read -r sha author committer; do
  if ! printf '%s' "${author}" | grep -Eq "${allowed_author_re}"; then
    echo "git_identity=fail commit ${sha} author not allowlisted: ${author}" >&2
    bad=1
  fi
  if printf '%s' "${committer}" | grep -Eq '^GitHub <noreply@github\.com>$'; then
    # GitHub-as-committer is only legitimate plumbing for dependabot-authored commits.
    if ! printf '%s' "${author}" | grep -Eq 'dependabot\[bot\]'; then
      echo "git_identity=fail commit ${sha} committed via GitHub UI/API (committer ${committer}, author ${author}) — merge locally under the neutral identity instead" >&2
      bad=1
    fi
  elif ! printf '%s' "${committer}" | grep -Eq "${allowed_committer_re}"; then
    echo "git_identity=fail commit ${sha} committer not allowlisted: ${committer}" >&2
    bad=1
  fi
done < <(git log --format=$'%h\x1f%an <%ae>\x1f%cn <%ce>' HEAD)

if [ "${bad}" -ne 0 ]; then
  exit 1
fi

count="$(git rev-list --count HEAD)"
echo "git_identity=pass all ${count} commits carry allowlisted author+committer identities"
