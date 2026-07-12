# Release Runbook

This runbook is the operator register for image and Python package releases. Pre-flip commands are safe now; real PyPI and public GHCR steps are flip-day only.

## 1. Pre-Flip Test

1. Trigger the private GHCR image workflow from `main`.

   ```bash
   gh workflow run release-images.yml --ref main
   gh run list --workflow release-images.yml --limit 1
   gh run watch <run-id>
   ```

   Expected outcome: two private GHCR packages are updated, `ghcr.io/panellatech/panella-store` and `ghcr.io/panellatech/panella-app`. Dispatch tags are only `sha-<12>` and `test-<run_id>`; no version tag and no `latest` tag are published.

2. Authenticate before verifying private GHCR packages.

   ```bash
   gh auth status
   gh auth token | docker login ghcr.io -u "$(gh api user --jq .login)" --password-stdin
   ```

3. Resolve the pushed digests. Pre-flip `workflow_dispatch` builds are unsigned by design, so
   verify images by digest only; digest-pinning is the pre-flip integrity guarantee.

   ```bash
   SHA_TAG="sha-$(git rev-parse --short=12 HEAD)"
   STORE_DIGEST="$(docker buildx imagetools inspect ghcr.io/panellatech/panella-store:${SHA_TAG} --format '{{json .Manifest.Digest}}' | tr -d '"')"
   APP_DIGEST="$(docker buildx imagetools inspect ghcr.io/panellatech/panella-app:${SHA_TAG} --format '{{json .Manifest.Digest}}' | tr -d '"')"
   ```

   Record the resolved `@sha256` image references for downstream digest-pinning. There is no
   pre-flip cosign signature or Rekor entry to verify.

4. Trigger the TestPyPI workflow from `main`.

   ```bash
   gh workflow run release-pypi.yml --ref main -f target=testpypi
   gh run list --workflow release-pypi.yml --limit 1
   gh run watch <run-id>
   ```

   Expected outcome: the package build passes `check-wheel-contents.py` and `twine check`, then publishes or skips an existing duplicate on TestPyPI. Check `https://test.pypi.org/project/panella/0.2.0/`.

   The pre-flip TestPyPI publish pins `attestations: false`, so — like the image dispatch — it writes **no** public Rekor entry while the repo is private. PEP 740 attestations (Sigstore keyless → Rekor) come online only with the flip-day real-PyPI publish, as provenance for public users.

## 2. One-Time Human Setup

1. Create the GitHub environments.

   GitHub repo -> Settings -> Environments -> New environment -> `testpypi` -> Deployment branches and tags: selected branches/tags that match the intended refs -> Required reviewers: add the repository owner account -> Save protection rules.

   Repeat for environment `pypi`.

2. Add the TestPyPI trusted publisher.

   TestPyPI -> Account settings -> Publishing -> Add a new pending publisher:

   ```text
   PyPI project name: panella
   Owner: panellatech
   Repository name: panella
   Workflow name: release-pypi.yml
   Environment name: testpypi
   ```

3. Add the real PyPI trusted publisher.

   PyPI -> Account settings -> Publishing -> Add a new pending publisher:

   ```text
   PyPI project name: panella
   Owner: panellatech
   Repository name: panella
   Workflow name: release-pypi.yml
   Environment name: pypi
   ```

4. Verify environment approval actually pauses.

   ```bash
   gh workflow run release-pypi.yml --ref main -f target=testpypi
   gh run list --workflow release-pypi.yml --limit 1
   ```

   Open the run in GitHub Actions and confirm the `publish` job waits for environment approval before publishing. **On private repos under free org plans, environment protection may silently not enforce; if the run does not pause, the code-level real-PyPI guard is the only real interlock until the repo is public.**

## 3. Flip-Day Sequence

Order is load-bearing.

1. Merge the flip-day PR that removes the `Real PyPI flip-day interlock` step from `.github/workflows/release-pypi.yml`.

2. Tag the exact merge commit after the guard removal is on `main`.

   ```bash
   git switch main
   git pull --ff-only
   VERSION="X.Y.Z"
   git tag -a "v${VERSION}" -m "v${VERSION}"
   git push origin "v${VERSION}"
   ```

   Do not tag before the guard-removal merge. A dispatch runs the workflow file as of the dispatched ref; tagging first bakes the guard into the tag and deadlocks real PyPI.

3. Wait for `release-images.yml` to publish and sign images from the tag.

   ```bash
   gh run list --workflow release-images.yml --limit 1
   gh run watch <run-id>
   gh run download <run-id> -n compose-pinned
   # Placeholder is interpolation-only: the pinned file keeps the required
   # ${PANELLA_API_KEY:?} vars, and `config` fails without a value or .env.
   PANELLA_API_KEY=verify-placeholder docker compose -f compose.pinned.yml config
   ```

4. Dispatch real PyPI from the tag ref and approve the `pypi` environment.

   ```bash
   gh workflow run release-pypi.yml --ref "v${VERSION}" -f target=pypi
   gh run list --workflow release-pypi.yml --limit 1
   gh run watch <run-id>
   ```

5. Verify the package and signatures.

   ```bash
   python -m venv /tmp/panella-release-verify
   /tmp/panella-release-verify/bin/python -m pip install -U pip
   /tmp/panella-release-verify/bin/python -m pip install "panella==${VERSION}"
   /tmp/panella-release-verify/bin/panella --help
   ```

   Then run the post-flip verification register below.

6. Pin digests into downstream deployment from `compose.pinned.yml`, then make GHCR packages public.

   GitHub repo -> Packages -> `panella-store` -> Package settings -> Change visibility -> Public.

   Repeat for `panella-app`.

## 4. Verify Commands

### 4.1 Pre-Flip Register

Use this register while GHCR packages are private. Pre-flip `workflow_dispatch` builds are unsigned
by design and never write to Rekor, so there is no pre-flip signature to verify. Verify images by
digest only; digest-pinning is the pre-flip integrity guarantee.

```bash
gh auth status
gh auth token | docker login ghcr.io -u "$(gh api user --jq .login)" --password-stdin
SHA_TAG="sha-$(git rev-parse --short=12 HEAD)"
STORE_DIGEST="$(docker buildx imagetools inspect ghcr.io/panellatech/panella-store:${SHA_TAG} --format '{{json .Manifest.Digest}}' | tr -d '"')"
APP_DIGEST="$(docker buildx imagetools inspect ghcr.io/panellatech/panella-app:${SHA_TAG} --format '{{json .Manifest.Digest}}' | tr -d '"')"
# Placeholder is interpolation-only (required ${PANELLA_API_KEY:?} vars in the pinned file).
PANELLA_API_KEY=verify-placeholder docker compose -f compose.pinned.yml config
```

Cosign signatures and Rekor transparency come online with the first real tag release (`v<version>`);
verification then pins the exact certificate identity `@refs/tags/v<version>` (not a regex).

### 4.2 Post-Flip Register

Use this register as an unauthenticated third party after GHCR packages are public.

```bash
docker logout ghcr.io || true
VERSION="X.Y.Z"
STORE_DIGEST="$(docker buildx imagetools inspect ghcr.io/panellatech/panella-store:v${VERSION} --format '{{json .Manifest.Digest}}' | tr -d '"')"
APP_DIGEST="$(docker buildx imagetools inspect ghcr.io/panellatech/panella-app:v${VERSION} --format '{{json .Manifest.Digest}}' | tr -d '"')"
CERT_IDENTITY="https://github.com/panellatech/panella/.github/workflows/release-images.yml@refs/tags/v${VERSION}"
cosign verify --certificate-oidc-issuer https://token.actions.githubusercontent.com --certificate-identity "${CERT_IDENTITY}" "ghcr.io/panellatech/panella-store@${STORE_DIGEST}"
cosign verify --certificate-oidc-issuer https://token.actions.githubusercontent.com --certificate-identity "${CERT_IDENTITY}" "ghcr.io/panellatech/panella-app@${APP_DIGEST}"
/tmp/panella-release-verify/bin/python -m pip install "panella==${VERSION}"
```

Verification pins the EXACT release-tag identity (`--certificate-identity ...@refs/tags/v${VERSION}`), not a regex — a mutable GHCR tag repointed to a digest signed from any other ref/tag fails verification. The digest checked by `cosign verify` must exactly match the digest pinned into `compose.pinned.yml`.
