# Cutting a release

HACS shows GitHub **releases**, not tags, and reads `manifest.json` from the released ref.
Bumping the version without publishing a release changes nothing a user can see; publishing a
release without bumping the version ships a build that lies about itself. Both, in that order,
or neither.

The M6 issues this file covers: **AHA-29** (cut v1.0.0), **AHA-28** (brands — closed, see below), **AHA-30**
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

## Brand icons: nothing to do (AHA-28)

**`home-assistant/brands` no longer accepts custom integrations.** Its pull request template
says so outright — "Pull requests for adding new custom components will no longer be accepted" —
and its `custom_integrations/` folder is marked legacy, superseded by the
[Brands Proxy API](https://developers.home-assistant.io/blog/2026/02/24/brands-proxy-api).
A PR there would be closed unread, and the template's "Type of change" list has no box for it.

Nothing needs doing, because the icons are already in the only place that now matters:
`custom_components/alertroster/brand/icon.png` (256x256) and `icon@2x.png` (512x512). Home
Assistant reads those directly and prefers them over the CDN, and HACS's own `Check brands` is
satisfied by that directory — the brands repository is only its *fallback* when the directory is
missing. So this is not a precondition for `hacs/default` either, whatever the earlier version of
this file said.

One consequence to know rather than fix: local brand images need **HA 2026.3**, while `hacs.json`
admits 2025.1. A user between those versions sees the placeholder icon, and there is no longer a
route that would give them a real one.

If you ever do want a PR against an Open Home Foundation repository, read their
[AI policy](https://developers.home-assistant.io/docs/ai_policy) first: autonomous agents are not
allowed to contribute, and a pull request has to be one you have reviewed and can explain in your
own words.

## Then: the HACS default store (AHA-30)

Only after a release exists and both actions are green.

1. Confirm the repo installs as a HACS **custom repository** end to end (AHA-2): HACS → ⋮ →
   Custom repositories → `https://github.com/CloudBedrock/alertroster-hacs`, category
   *Integration* → install → restart → the integration loads.
2. PR against `hacs/default` adding `CloudBedrock/alertroster-hacs` to the `integration` file,
   **alphabetically** — the JSON-sorting check is one of the automated ones. Only the repo owner
   or a major contributor may open it.
3. The rest of the checks: brands (satisfied by the in-repo `brand/` directory), manifest,
   HACS validation, repo activity, ≥1 release, contributor, description/issues/topics.
4. After merge it appears in the next scheduled scan, not immediately.
