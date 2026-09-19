# 002 — Sonos: SonosNet off, soundbars back on Ethernet

**Status:** Planned (decided by Tom 2026-09-19; blocked only on Tom being home — the Sonos app
cannot reach the system over VPN, it needs his phone on the home Wi-Fi)
**Size:** Small (about an hour with Tom present; no code)
**Raised:** 2026-09-19, during the first spoken music test of the Primary Bedroom voice box

## Problem

Voice-requested music in the Primary Bedroom started, stopped after 10–20 s and skipped, over and
over. Music Assistant logged `ERROR_LSE`, `ERROR_BUFFERING`, `ERROR_LOST_CONNECTION` and
"Player Primary Bedroom disconnected prematurely from stream". It is not a voice defect: the
bedroom Beam (`media_player.primary_bedroom`, 192.168.0.6) is **wireless on SonosNet** with
marginal links (34–41; above 45 is good) to the wired Amps and 2,000–3,000 PHY errors per second,
the worst in the house together with its Sub Mini. Moving SonosNet from channel 11 to channel 1
(Tom, 2026-09-19; Zigbee is on channel 15, 2.4 GHz airtime is 44–66 % on every AP) did **not**
help — it is distance and walls, not the channel. The kitchen Amp is wired and plays smoothly.

Years ago Tom disabled the soundbars' switch ports because wired Sonos units caused network loops:
every wired SonosNet-capable unit bridges Ethernet to the 2.4 GHz mesh, and Sonos's legacy 802.1D
spanning tree (path cost 10 for 100 Mbit) disagrees with UniFi's RSTP (200000).

## Decision (Tom, 2026-09-19)

**Turn SonosNet off for the whole system and wire the soundbars again.** Since Sonos app 85
(27 May 2026) there is a system-wide wizard: *Settings → System → Network → SonosNet → Disable
SonosNet*. Sonos staff describe the effect as: a wired soundbar "will stop bridging SonosNet out
to your other Sonos players (it'll still talk to its own surrounds and Sub)" — the surround/Sub
link is a private 5 GHz link, not part of SonosNet. Sonos documents "wire one or more products, the
rest stay on home Wi-Fi" as supported with SonosNet off, and Ubiquiti's Sonos guidance says the
same (disable SonosNet, wire the devices, same switch where possible). Sources were search-result
snippets plus `github.com/IngmarStein/unifi-sonos-doc`; the vendor sites are blocked by the dev
pod's egress allowlist, and no first-hand "I did this and the loops stopped" report was found —
hence the staged procedure.

Two rules that must not be broken:

- **Never** use the old per-device *Disable Wi-Fi* on a soundbar: it kills the radio its surrounds
  and Sub depend on. Only the system-wide SonosNet wizard.
- Do not wire bonded satellites (Era 300 surrounds, Subs). It gains nothing (their audio rides the
  soundbar's 5 GHz link regardless) and it is the one topology nobody could rule out as a loop.

## Current state (surveyed read-only 2026-09-19, all units firmware 97.1-80312, 192.168.0.x, Default VLAN)

| Unit | IP | Today | Notes |
|---|---|---|---|
| Primary Bedroom Beam | .6 | SonosNet wireless, Ethernet dead | master of Sub Mini .77; the stuttering one |
| Pink Room Beam | .24 | SonosNet wireless, Ethernet dead | standalone |
| Blue Room Beam | .81 | SonosNet wireless, Ethernet dead | standalone; weakest mesh links in the house (28–34) |
| White Room Beam | .231 | SonosNet wireless, Ethernet dead | standalone |
| Living Room Arc | .232 | SonosNet wireless, Ethernet dead | master of Era 300 .69 / .209 and Subs .100 / .216 |
| Movie Room Port | .70 | SonosNet wireless, Ethernet dead | standalone |
| Shed SYMFONISK | .154 | home Wi-Fi (HNET+, 5 GHz) | proves the system already holds the Wi-Fi credentials |
| Amps: Back Yard .206, Front Porch .215, Kitchen .234, Primary Bathroom .88, Study .71, Kids' Bathroom .132 | | wired, bridging SonosNet | **USW Pro Max 16 PoE ports 1–6**, spanning tree off per port, zero STP state changes; Back Yard is the Sonos STP root |
| Pool Amp | .108 | wired, no radio | Shed Flex 2.5G port 7 |

Disabled switch ports: **Switch Pro Max 48 PoE ports 3, 5, 6 and 7** — the only administratively
disabled ports on that switch (`port_table[].enabled == false`, override `forward: "disabled"`,
no network, port security on with an empty MAC list). The controller kept no MAC history for them,
so **which soundbar is on which port is unknown** until each links up. Six units have dead
Ethernet but only four ports are disabled: two of them are unplugged or on another switch (ports
26, 30 and 45 of the Pro Max 48 are enabled on Default with no link).

UniFi Wi-Fi settings on `HNET+` are already right for Sonos and must be left alone: multicast
enhancement off, 2.4 GHz minimum rate 1 Mbps, PMF optional, no client isolation, no proxy ARP,
802.11r off. Never enable the "optimize" / Wi-Fi AI / airtime-fairness features on it. Storm
control and loop protection are off on every switch port today.

## Procedure

Who does what (Tom's ruling): **Tom makes the switch-port changes in the UniFi app**; the agent
gives exact settings, verifies and monitors. The `mcp-unifi` port tools cannot do it
(haynes-ops issue #2984); calling the UniFi API from inside the mcp-unifi pod was offered and not
chosen.

1. **Tom, at home:** run the Disable SonosNet wizard. Expect every Sonos to drop for a minute or
   two. Arc and Beams are 2.4 GHz-only on Wi-Fi, so they land on `HNET+` 2.4 GHz until wired; one
   that fails to join simply stays offline until its port is enabled.
2. **Agent:** confirm on all 19 units before any port is touched. From inside the HA pod
   (`kubectl exec -n home-automation deploy/home-assistant -c app -- curl -s -m 5 …`):
   `http://<ip>:1400/status/wireless` → `<SonosNetDisabled>1` and a `ConnectionTypeString` of
   Ethernet / WiFi / Home Theater (today: `0`, "SonosNet (Ethernet)" / "SonosNet (wireless)");
   and an Amp's `http://<ip>:1400/status/proc/ath_rincon/status` must no longer list mesh peers.
   Baseline for comparison (2026-09-19): access ports receive under 4 broadcast+multicast packets
   per second, uplinks 12–60 (`rate(unpoller_device_port_receive_broadcast_total[…]) + …multicast…`).
3. **Tom, one port at a time, on the agent's go** (Devices → Switch Pro Max 48 PoE → Port Manager):
   port enabled; Native VLAN/Network **Default**; Tagged VLAN Management **Block All**; Advanced →
   Manual: **Spanning Tree off** (as on the six Amp ports), **Loop Protection on**, **Storm
   Control on** for broadcast, multicast and unknown unicast at **500 pps** each (1 % if the app
   only offers percent), Port Isolation off, **MAC restriction / port security off** (it is on
   today with an empty allow-list and would block the soundbar). Declare the work first
   (`declare-activity start … --scope network,unifi,sonos,music-assistant,home-automation`).
4. **Agent, 10 minutes per port:** which soundbar's `eth0` comes alive (that identifies the port),
   broadcast/multicast rates, `mac_table_count` on the new port (1–3 is normal; climbing past ~5
   means bridging is back), `stp_state_change_count` on ports nobody touched (raw record via
   `get_device_by_mac`, saved to a file, read with `jq`; it contains `x_authkey` — never paste it).
   Abort = Tom disables the port again. Ignore the pre-existing noise on Pro Max 48 port 13 and
   Livingroom Flex port 4. Then ports 5, 6, 7.
5. **Afterwards:** find the two dead-Ethernet units without a disabled port; DHCP reservations for
   the newly wired units; re-test bedroom music as the satellite
   (`scripts/voice-bench/run.sh bench.py "MODE=pipe … DEVICE_ID=9140746b067691b4aeb5a66c38a642db …"`)
   and watch the Music Assistant log for a full minute; re-check *About My System → ConnectionType*
   after Sonos firmware updates (the old per-device setting used to revert; no such report for the
   new wizard, but nobody has confirmed it survives updates either).

## Open questions

- Which soundbar is on which of ports 3/5/6/7, and where the other two cables go.
- Whether to also retro-fit storm control and loop protection on the six Amp ports.
- IGMP snooping on the Default network is off; sources mostly recommend on for Sonos. Not part of
  this item: change it separately, on its own, if at all.
