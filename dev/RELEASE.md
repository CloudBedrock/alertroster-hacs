# Cutting a release

HACS shows GitHub **releases**, not tags, and reads `manifest.json` from the released ref.
Bumping the version without publishing a release changes nothing a user can see; publishing a
release without bumping the version ships a build that lies about itself. Both, in that order,
or neither.

The M6 issues this file covers: **AHA-29** (cut v1.0.0), **AHA-28** (brands), **AHA-30**
(`hacs/default`).

## Before you cut

Everything here is a precondition, not a nicety — the `hacs/default` PR checks most of it
automatically and the rest is what a first user hits.

- [ ] `pytest`, `ruff check`, `ruff format --check`, `mypy --strict custom_components/alertroster`
      and `mypy tests` all clean locally (AHA-27).
- [ ] `hacs/action` and `hassfest` green on the exact commit you are about to tag, with no
      `ignore`s. `gh run list --branch main --limit 1`.
- [ ] The integration has been exercised against a **real station** running a build with §5
      items 1 and 2 (`GET /v1/discover`, `DELETE /v1/sources/self`) — pair, raise, expire,
      acknowledge, resolve, socket loss and recovery, token revoked → reauth, delete entry →
      row gone on the station. A station answering `404` to `/v1/discover` only tests the
      fallbacks.
- [ ] Diagnostics downloaded from a real device page and grepped for `lat_`. This is the one
      REQUIREMENTS.md §6 promise that cannot be un-shipped once someone pastes a diagnostics
      dump into a public issue.
- [ ] README examples pasted into a real automation and run, not just read (AHA-31).
- [ ] Repo description, topics and issues on (AHA-1) — `gh repo view --json
      description,repositoryTopics,hasIssuesEnabled`.

## Cut it

```sh
# 1. Bump, on a branch, through the normal PR flow.
#    manifest.json "version" is the only place the version lives.
$EDITOR custom_components/alertroster/manifest.json

# 2. After it merges and CI is green on main:
git checkout main && git pull
gh release create v1.0.0 --title v1.0.0 --notes-file dev/release-notes-v1.0.0.md
```

Tag names carry the `v`; `manifest.json` does not (`1.0.0`). HACS shows the last five releases.

## Then: brand icons (AHA-28)

The icons already live in `custom_components/alertroster/brand/`, which is where HACS and
HA ≥ 2026.3 look. The default store and older HA read `home-assistant/brands` instead, so the
same two files go there as well:

Clone it **outside this repo**. `gh repo fork --clone` clones into the working directory, and
run from the repo root that leaves a whole nested checkout of `brands` inside the integration
repo you are about to release:

```sh
ICONS=$(git -C ~/dev/alertroster-hacs rev-parse --show-toplevel)/custom_components/alertroster/brand
cd ~/dev                      # anywhere that is not the integration repo
gh repo fork home-assistant/brands --clone --remote
cd brands
mkdir -p custom_integrations/alertroster
cp "$ICONS/icon.png"     custom_integrations/alertroster/
cp "$ICONS/icon@2x.png"  custom_integrations/alertroster/
# PR against home-assistant/brands. Their CI checks the dimensions: 256x256 and 512x512.
```

Merge this **before** the `hacs/default` PR — brands is one of that PR's automated checks.

## Then: the HACS default store (AHA-30)

Only after a release exists and both actions are green.

1. Confirm the repo installs as a HACS **custom repository** end to end (AHA-2): HACS → ⋮ →
   Custom repositories → `https://github.com/CloudBedrock/alertroster-hacs`, category
   *Integration* → install → restart → the integration loads.
2. PR against `hacs/default` adding `CloudBedrock/alertroster-hacs` to the `integration` file,
   **alphabetically** — the JSON-sorting check is one of the automated ones. Only the repo owner
   or a major contributor may open it.
3. The rest of the checks: brands, manifest, HACS validation, repo activity, ≥1 release,
   contributor, description/issues/topics.
4. After merge it appears in the next scheduled scan, not immediately.
