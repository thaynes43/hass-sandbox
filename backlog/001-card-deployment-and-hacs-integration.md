# 001 — Custom card deployment revamp / HACS integration

**Status:** Proposed
**Size:** Major (multi-session revamp; needs a plan in `.agents/plans/` before starting)
**Raised:** 2026-07-30, while adding the filter pause feature to `immich-fetcher-card.js`

## Problem

Custom Lovelace cards are source-controlled in this repo
(`appdaemon/apps/immich_fetcher/immich-fetcher-card.js`,
`appdaemon/apps/photo_frame_viewer/photo-frame-viewer-card.js`,
`appdaemon/apps/photo_frame_viewer/photo-display-card.js`, …) but deployment is
entirely manual:

1. Copy the JS file into the Home Assistant pod at
   `/config/www/photo-frame/<card>.js` (requires shell/`kubectl cp` access to
   the pod — which agents don't always have).
2. Bump the `?v=N` cachebuster on the Lovelace resource via ha-mcp
   (`ha_config_set_dashboard_resource`; resource IDs recorded in
   `appdaemon/apps/immich_fetcher/immich-fetcher-todo.md` and
   `.agents/playbooks/cache-busting-playbook.md`).
3. Hard-refresh every client (desktop, iOS Companion, UniFi Connect displays).

This is tedious, easy to get wrong (stale cache masquerades as a broken card),
leaves no version traceability between what's deployed and what's in git, and —
unlike the AppDaemon apps, which deploy automatically via the Docker image /
Flux pipeline on merge to `main` — has no CI/CD story at all.

## Goal

Merging a card change to `main` should result in an updatable, versioned card in
HA — ideally with HACS's native "update available" button — with cache busting
handled automatically and no manual pod access.

## Candidate approaches (to be evaluated in the plan)

### A. HACS custom repository (the "community store update button")

Package the cards as a HACS-compliant frontend repository:

- HACS `plugin` (dashboard) repos need a `hacs.json`, a released JS artifact
  (`dist/` or GitHub release asset via `zip_release`), and semver GitHub
  releases. HACS then owns install/update/cache-busting (it appends its own
  `hacstag` param — no manual `?v=N`).
- Open questions:
  - One repo per card vs. one `haynes-cards` bundle repo (HACS is
    one-artifact-per-repo; a bundle would need the cards merged into a single
    JS module or a small loader).
  - Split cards out of hass-sandbox, or keep source here and publish via CI to
    a release-only repo? (Keeping source next to the AppDaemon apps that
    provision the relay scripts has real value — the card and app are one
    feature.)
  - Whether to eventually publish to the HACS default store or stay a custom
    repository (custom repo already gets the update button).

### B. GitOps asset sync into the HA config volume (no HACS)

A CI job or in-cluster CronJob/initContainer that syncs released card assets
from GHCR/GitHub releases into `/config/www/` on the HA PVC, then bumps the
Lovelace resource version via the HA REST/WebSocket API (same call ha-mcp
makes). Fits the existing haynes-ops Flux model; no HACS UI, but fully
automated. Could reuse the AppDaemon image build tag so cards version with the
apps.

### C. Full custom integration

Ship a proper HA custom integration (installable via HACS as `integration`)
that registers its frontend modules itself (`async_register_built_in_panel` /
`frontend.async_register_extra_js_url` style, as integrations like Mushroom do).
Biggest lift, but removes the Lovelace-resource bookkeeping entirely and could
absorb some of the relay-script plumbing. Likely the end state of a broader V2
that reconsiders the AppDaemon-app + card architecture per feature.

## Sketch of a likely V1→V2 path

1. Short term (unblocks automation without restructuring): approach **B** —
   CI publishes card artifacts, a small in-cluster job syncs them and bumps the
   resource version. Manual step disappears.
2. Medium term: approach **A** for the mature cards (photo frame suite) so
   updates surface in the HACS UI like every other frontend module.
3. Long term: fold into approach **C** if/when a V2 rework of the photo-frame
   feature (fetcher + viewer + cards as one integration) happens.

## Constraints / notes

- Dev-env agents have no `kubectl exec` (OPERATOR-tier ServiceAccount); any
  automation must not depend on shelling into the HA pod.
- The cache-busting playbook (`.agents/playbooks/cache-busting-playbook.md`)
  and the resource-ID table in `immich-fetcher-todo.md` become obsolete once
  this lands — retire them as part of the work.
- Cards must keep working on all three targets (desktop, iOS Companion,
  UniFi Connect Android webview) — whatever pipeline we pick must not change
  how the JS is served in a way that breaks webview caching assumptions.
- Related future work lives in `appdaemon/apps/immich_fetcher/immich-fetcher-todo.md`
  (card UX tasks) — a V2 effort should absorb or re-triage that list.

## Acceptance criteria (draft)

- [ ] Card changes merged to `main` reach HA without anyone copying files into
      the pod.
- [ ] Clients pick up new card versions without manual `?v=N` bumps.
- [ ] Deployed card version is traceable to a git tag/release.
- [ ] Rollback is a one-step action (HACS downgrade or artifact re-pin).
