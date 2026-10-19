# Our own kiosk APK for the UniFi Connect displays

Status: Proposed (Tom, 2026-10-18: backlog it; short term the kitchen display uses browser audio).
Size: Medium (a new small Android repo with CI, plus a dashboard hook).

Links: spike [#260](https://github.com/thaynes43/hass-sandbox/issues/260) (kitchen Connect
display as an oven-timer display with chimes), [#255](https://github.com/thaynes43/hass-sandbox/issues/255)
(the oven's own timer can't be shown remotely),
[#259](https://github.com/thaynes43/hass-sandbox/issues/259) (the Voice PE named-timer approach).

## Problem

The two UniFi Connect Display SE 21 screens (Android 9, API 28) show the HA dashboards in
UniFi Connect's **web mode**. That works, but HA can't drive the device:

- **Sound needs a tap.** If the web mode's WebView requires a user gesture for media,
  the page can't chime until someone touches the screen after each page load. The HA
  frontend reloads itself after every HA update (a new frontend service worker takes
  over), so a timer chime can go silent without warning. #260 measures this.
- **No remote control.** HA can't wake the screen, load a URL, reload the page, or speak.
- **The HA Companion app is harder to maintain** (Tom). It was tried before: the stale
  mobile_app devices `unifi_display for arm64` and `... upstairs` (HA app 2026.1.5, full
  flavor) date from that trial. The full flavor gets no push on a device without Google
  services unless "Persistent connection" is set to Always, which is why a notify call
  to it returned HTTP 500 ("not connected to local push notifications").
- **Fully Kiosk Browser** (HA `fully_kiosk` integration) would cover this, but it needs a
  paid Plus license per device, it's closed source, and nobody has confirmed that it
  runs inside UniFi Connect's Android App mode.

## Goal

A minimal app of our own that UniFi Connect runs as the display's app. It shows the HA
dashboard full-screen, can always play sound, and takes a few commands from HA.

## Sketch

- Kotlin, `minSdk 28`, ABIs `arm64-v8a` and `armeabi-v7a`, with no Google Play services
  dependency.
- One full-screen `WebView` on the dashboard URL: `setMediaPlaybackRequiresUserGesture(false)`,
  DOM storage on (the HA frontend keeps its auth tokens in `localStorage`), screen kept on.
  Recreate the WebView if its renderer dies, and reload on network recovery.
- **Command hook.** Pick one:
  - (a) The app opens its own HA websocket with a long-lived token entered once in the
    app's settings (never baked into the APK). It subscribes to a `connect_kiosk_command`
    event with a `display` target and handles `chime`, `tts` (Android `TextToSpeech`),
    `wake`, `load_url` and `reload`. There is no inbound port. HA fires the event from
    scripts and automations.
  - (b) A small LAN HTTP API (for example NanoHTTPD) with a shared secret, called by
    HA through `rest_command`. It's simpler, but it opens an inbound port on the display.
  - Either way, a `window.ConnectKiosk` JavaScript bridge (`addJavascriptInterface`)
    lets dashboard cards call native `chime()` and `speak()` directly.
- CI on GitHub Actions: the hosted Ubuntu runners include the Android SDK, so
  `./gradlew assembleRelease` plus `apksigner` with a keystore from repo secrets builds
  the APK. CI attaches it to a GitHub release. Never build it in the dev-env pod (CPU budget).
- Its own repo with the Claude Code PR reviewer, per the pod's new-repo rules.

## Constraints we already know

From Ubiquiti's help article "UniFi Connect – Using Android Apps on your UC Display":
- The app must support Android 9.0 and `armeabi-v7a`/`arm64-v8a`.
- Google Mobile Services are not available.
- The app may not launch other apps or stop the current app, and may not read external
  storage or other apps' data.
- The Storage, Contacts, Sensors, SMS, Phone and Camera permission groups are blocked.
  Location and Microphone are enabled by default.
- Install: the display's settings in UniFi Connect → Display Mode **Android App** →
  Manage Apps → upload the APK → select it. The display then reboots and installs it.
  These steps come from a third-party guide and haven't been verified here.

## Open questions

1. Does Android App mode start a custom APK at boot and restart it if it crashes? The
   HA app trial suggests installing one works, but nobody has checked auto-start.
2. What's the update path? Re-uploading every version through UniFi Connect is manual.
   ADB over the network is probably off. In-app self-update needs
   `REQUEST_INSTALL_PACKAGES` and a confirmation prompt, which may be blocked.
3. How old is the system WebView? The display can't decode HEVC (see the
   g6-hevc-connect-display notes), which suggests a WebView older than Chrome 136.
   The #260 test logs its exact user agent. Is it new enough for the HA frontend over time?
4. Volume: does the UniFi Connect volume setting govern the app's audio stream, or
   can the app set `STREAM_MUSIC`/`STREAM_ALARM` itself?
5. Who holds the signing key, and how is it stored and backed up?
6. Is this for the kitchen display only, or both?
7. What's the rollback? Switch the display back to Web mode in UniFi Connect, and
   confirm that switch is quick.

## What it would give us over Fully Kiosk

No license and no closed third-party app. Exactly the features we use, versioned in git
with CI. A JavaScript bridge so our own cards can trigger native audio and TTS. The cost
is that we own it, although churn should be low on a device frozen at Android 9.
