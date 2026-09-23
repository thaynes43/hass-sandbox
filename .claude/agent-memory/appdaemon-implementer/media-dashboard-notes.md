---
name: media-dashboard-notes
description: media_dashboard_app constraints that are not visible in the code — SerpApi's 250/month quota (never call it live), and third-party listing APIs answering in the exit node's language
metadata:
  type: project
---

# media_dashboard_app — constraints behind the code

## Never make a live SerpApi (or TMDb) call while developing

**Why:** SerpApi is on the **free 250-searches/month** plan and each daily
refresh spends one search *per configured theater* (two today). A handful of
exploratory calls is a meaningful slice of the month. Tom's drivers verify
showtime behaviour on the live pod after deploy, not from a dev session.

**How to apply:** unit tests mock the client; anything that must hit the real
API goes in `appdaemon/tests/integration-tests/` behind an env gate. The app's
own quota guard is the on-disk cache date (`showtime-cache.json` on the /media
PVC) — one fetch per calendar day, `force=True` only for a user-initiated
refresh from the card.

## A search/listings API with no locale params answers in the exit node's language

**Why:** on 2026-09-22 half the live showtime cache came back in Lithuanian
("absoliutus blogis" = Resident Evil, day label "Šiandien") because
`get_showtimes` sent no `hl`/`gl`/`google_domain`. Nothing errored: the titles
simply matched no TMDb title and the unparsed day labels all defaulted to
today, so the row looked plausible and was wrong.

**How to apply:** pin the locale on any third-party search/listings client, and
when a parser has a "fall back to today/default" branch, log a warning naming
the raw value it could not parse — one per batch, not per row. A silent
fallback is how this hid for months. Same shape as the AD `set_state` filtering
trap: the failure is data that looks fine.

Related: [[appdaemon-set-state-semantics]], [[appdaemon-probe-design]]
