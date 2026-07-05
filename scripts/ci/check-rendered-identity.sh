#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -ne 1 ]; then
  echo "usage: $0 <rendered-config-dir>" >&2
  exit 2
fi

render_dir="$1"
agents_dir="${render_dir}/agents"
wings_file="${render_dir}/wings.yaml"

if [ ! -d "${agents_dir}" ]; then
  echo "render_identity=fail missing agents dir: ${agents_dir}" >&2
  exit 1
fi

if [ ! -f "${wings_file}" ]; then
  echo "render_identity=fail missing wings file: ${wings_file}" >&2
  exit 1
fi

if ! compgen -G "${agents_dir}/*.yaml" >/dev/null; then
  echo "render_identity=fail no rendered agent yaml files under ${agents_dir}" >&2
  exit 1
fi

files=("${agents_dir}"/*.yaml "${wings_file}")

echo "render_identity: rendered files"
for file in "${files[@]}"; do
  echo "  ${file}"
done

if ! grep -REq '(^|[^[:alnum:]_])owner([^[:alnum:]_]|$)' "${files[@]}"; then
  echo "render_identity=fail generic owner wing/slug is absent" >&2
  exit 1
fi

if ! grep -Rq 't_owner_personal' "${files[@]}"; then
  echo "render_identity=fail generic owner tenant is absent" >&2
  exit 1
fi

# Public tripwire only: no private deny-dictionary belongs in OSS CI, and the tripwire itself must
# NOT hardcode any maintainer-specific term (that would leak the very identity it guards against).
# These GENERIC sentinels catch obvious local-machine / real-person / private-network leakage
# (home + user paths, any email address, RFC-1918 private IPs). The authoritative owner-identity
# scan is the maintainers' private deny-dictionary, run out-of-band before every push.
deny_re='/home/|/Users/|[[:alnum:]_.%+-]+@[[:alnum:].-]+\.[[:alpha:]]{2,}|(^|[^0-9])((10|192\.168|172\.(1[6-9]|2[0-9]|3[0-1]))\.[0-9]{1,3}\.[0-9]{1,3})([^0-9]|$)'
if grep -REin "${deny_re}" "${files[@]}"; then
  echo "render_identity=fail rendered config contains forbidden public identity/path/network sentinel" >&2
  exit 1
fi

echo "render_identity=pass generic owner identity present and public deny-sentinels absent"
