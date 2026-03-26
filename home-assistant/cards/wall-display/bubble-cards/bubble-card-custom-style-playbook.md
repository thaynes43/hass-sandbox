# Bubble Card Custom Style Playbook

This folder uses Bubble Card with a dark Frosted Glass look for the wall display.

The goal is not to make every card identical. The goal is to keep a shared visual system:

- dark translucent surfaces
- soft inner highlight
- subtle bright border
- blur on the card shell
- slightly brighter text and icons
- restrained accent gradients per action area

Use this playbook when styling more cards in:

- `home-assistant/cards/wall-display/bubble-cards/`

Reference examples already validated in this repo:

- `home-assistant/cards/wall-display/bubble-cards/media-player.yaml`
- `home-assistant/cards/wall-display/bubble-cards/wall-display-garage-doors-button.yaml`
- `home-assistant/cards/wall-display/bubble-cards/wall-display-locks-button.yaml`
- `home-assistant/cards/wall-display/dock-bar/dock-bar-subbuttons.yaml`

## Visual Direction

This work is based on the dark mode Frosted Glass theme and the Bubble Card styling hooks.

The current working palette is:

- shell background: `rgba(28, 29, 33, 0.32)`
- shell border: `1px solid rgba(255, 255, 255, 0.14)`
- inner highlight: `inset 0 1px 0 rgba(255, 255, 255, 0.18)`
- soft inner haze: `inset 0 0 18px rgba(255, 255, 255, 0.04)`
- outer shadow: `0 14px 40px rgba(0, 0, 0, 0.28)`
- text: `rgba(240, 243, 255, 0.92)`
- secondary text: `rgba(203, 214, 234, 0.78)`
- icon background: `rgba(255, 255, 255, 0.06)`
- neutral control background: `rgba(255, 255, 255, 0.05)`

Blur treatment:

- `backdrop-filter: blur(14px) saturate(1.2)`
- `-webkit-backdrop-filter: blur(14px) saturate(1.2)`

Border radii currently in use:

- main shell: `28px`
- action chip / sub-button: `20px` to `22px`
- media transport buttons: `18px`

## Safe Pattern

For most Bubble Cards, apply styles in two layers:

1. Bubble variables on `ha-card`
2. Direct CSS overrides for the rendered internal classes

Do both when possible. Variables are the cleaner path, but direct selectors are still needed for visual consistency across card types.

## Reusable Shell

Start from this shell block and adjust only when needed:

```yaml
styles: |
  ha-card {
    --bubble-border-radius: 28px !important;
    --bubble-box-shadow:
      0 14px 40px rgba(0, 0, 0, 0.28),
      inset 0 1px 0 rgba(255, 255, 255, 0.18),
      inset 0 0 18px rgba(255, 255, 255, 0.04) !important;
    --bubble-border: 1px solid rgba(255, 255, 255, 0.14) !important;
    --bubble-main-background-color: rgba(28, 29, 33, 0.32) !important;
    backdrop-filter: blur(14px) saturate(1.2) !important;
    -webkit-backdrop-filter: blur(14px) saturate(1.2) !important;
  }

  .bubble-button-card-container {
    background: rgba(28, 29, 33, 0.32) !important;
    border: 1px solid rgba(255, 255, 255, 0.14) !important;
    border-radius: 28px !important;
    box-shadow:
      0 14px 40px rgba(0, 0, 0, 0.28),
      inset 0 1px 0 rgba(255, 255, 255, 0.18),
      inset 0 0 18px rgba(255, 255, 255, 0.04) !important;
    backdrop-filter: blur(14px) saturate(1.2) !important;
    -webkit-backdrop-filter: blur(14px) saturate(1.2) !important;
    overflow: hidden;
  }
```

## Text And Icon Treatment

These selectors are safe and currently working:

```yaml
  .bubble-name,
  .bubble-state,
  .bubble-attribute {
    color: rgba(240, 243, 255, 0.92) !important;
    text-shadow: 0 1px 10px rgba(0, 0, 0, 0.18);
  }

  .bubble-name {
    font-weight: 600 !important;
    letter-spacing: 0.01em;
  }

  .bubble-attribute {
    color: rgba(203, 214, 234, 0.78) !important;
    font-size: 0.83rem !important;
  }

  .bubble-icon,
  .bubble-sub-button-icon {
    color: rgba(245, 247, 255, 0.94) !important;
  }

  .bubble-icon-container {
    background: rgba(255, 255, 255, 0.06) !important;
    box-shadow:
      inset 0 1px 0 rgba(255, 255, 255, 0.12),
      0 8px 20px rgba(0, 0, 0, 0.16) !important;
  }
```

## Sub-Buttons Pattern

For `card_type: sub-buttons`, this pattern works well:

```yaml
  .bubble-sub-button {
    border-radius: 20px !important;
    border: 1px solid rgba(255, 255, 255, 0.08) !important;
    box-shadow:
      inset 0 1px 0 rgba(255, 255, 255, 0.12),
      0 8px 20px rgba(0, 0, 0, 0.16) !important;
    transition:
      transform 180ms ease,
      background-color 180ms ease,
      border-color 180ms ease,
      box-shadow 180ms ease !important;
  }

  .bubble-sub-button:hover {
    transform: translateY(-1px);
    border-color: rgba(255, 255, 255, 0.14) !important;
    box-shadow:
      inset 0 1px 0 rgba(255, 255, 255, 0.16),
      0 10px 24px rgba(0, 0, 0, 0.22) !important;
  }
```

Then tint individual chips with `.bubble-sub-button-1`, `.bubble-sub-button-2`, and so on.

Example:

```yaml
  .bubble-sub-button-1 {
    background: linear-gradient(180deg, rgba(76, 119, 98, 0.38), rgba(34, 45, 40, 0.56)) !important;
  }
```

Use muted dark gradients, not saturated neon colors. This dashboard is a calm wall panel, not a control room.

## Media Player Pattern

For `card_type: media-player`, use the media-player-specific Bubble variables in addition to the shared shell:

```yaml
  ha-card {
    --bubble-media-player-main-background-color: rgba(28, 29, 33, 0.32) !important;
    --bubble-media-player-border-radius: 28px !important;
    --bubble-media-player-buttons-border-radius: 18px !important;
    --bubble-media-player-slider-background-color: rgba(255, 255, 255, 0.08) !important;
    --bubble-media-player-icon-background-color: rgba(255, 255, 255, 0.06) !important;
    --bubble-media-player-box-shadow:
      0 14px 40px rgba(0, 0, 0, 0.28),
      inset 0 1px 0 rgba(255, 255, 255, 0.18),
      inset 0 0 18px rgba(255, 255, 255, 0.04) !important;
  }
```

Also style the transport controls directly:

```yaml
  .bubble-button-background {
    background: rgba(255, 255, 255, 0.05) !important;
    border: 1px solid rgba(255, 255, 255, 0.08) !important;
    box-shadow:
      inset 0 1px 0 rgba(255, 255, 255, 0.08),
      0 8px 20px rgba(0, 0, 0, 0.16) !important;
  }

  .bubble-range-fill,
  .bubble-progress-fill,
  .bubble-volume-fill {
    background: linear-gradient(90deg, rgba(171, 198, 255, 0.78), rgba(121, 161, 232, 0.92)) !important;
    box-shadow: 0 0 18px rgba(133, 171, 238, 0.28) !important;
  }
```

Important constraint for the wall-display media player:

- prefer Bubble variables on `ha-card`
- avoid direct layout styling of `.bubble-button-card-container`
- avoid custom `.card-content` padding
- avoid direct `.bubble-sub-button` box model overrides unless a visual bug requires them

On this dashboard, those direct selectors caused the media card to visually collide with the card below it. The minimal variable-based version preserved the look without breaking spacing.

Additional note:

- top chips such as `Kitchen Sonos` can regain their glass appearance safely through Bubble config like `show_background: true`
- prefer that over forcing the look through direct container CSS

### Current Media Player Conventions

Use a clear action label for the launcher chip:

- `Launch Music Assistant`

If the card has dead space near the title area, prefer using the built-in Bubble fields before inventing custom DOM manipulation:

```yaml
show_attribute: true
attribute: friendly_name
```

This is safer than trying to set inner text from inside the `styles` block.

## Summary Button Pattern

For `card_type: button` cards that act like compact dashboard launchers, use the same frosted shell but keep the vertical rhythm tight.

Good candidates:

- garage door summary buttons
- lock summary buttons
- any wall-display card that sits beside another top-row summary card

Recommended baseline:

```yaml
show_state: true
rows: 1
```

Then compress through internal spacing first, not by immediately changing the grid footprint.

Useful selectors:

```yaml
  .card-content {
    padding: 4px 8px 6px !important;
  }

  .bubble-name {
    font-weight: 600 !important;
    letter-spacing: 0.01em;
    font-size: 0.96rem !important;
  }

  .bubble-state {
    font-size: 0.79rem !important;
    line-height: 1.1 !important;
  }
```

This matters when two cards live side by side or stack tightly and should feel like the same component family.

Final rule for compact wall-display Bubble buttons:

- prefer `ha-card` Bubble variables for shell/background treatment
- keep direct CSS focused on text, icon, and state color treatment
- treat `.bubble-button-card-container`, `.card-content`, and `.bubble-sub-button` as high-risk selectors on compact summary cards

For the wall-display `Garage Doors`, `Locks`, and `media-player` cards, direct container-level overrides created size drift, false spacing fixes, or visual overlap with neighboring cards. The stable solution was to back those cards down to variable-driven styling and keep direct CSS mostly color-only.

## State-Reactive Chips

For Bubble `button` cards with `sub_button.main` status chips, use the chip background to reflect status immediately.

Current validated pattern:

- `closed` or secure states: cool blue-violet glass
- transitional states like `opening`, `closing`, `locking`, `unlocking`: cooler blue motion state
- warning states like `open` or insecure: warmer amber-brown glass

Example pattern:

```yaml
  .bubble-sub-button-1 {
    background: ${(() => {
      const s = hass.states['cover.some_door']?.state;
      if (s === 'closed') return 'linear-gradient(180deg, rgba(92, 98, 133, 0.34), rgba(36, 40, 58, 0.56))';
      if (s === 'closing' || s === 'opening') return 'linear-gradient(180deg, rgba(86, 112, 154, 0.38), rgba(34, 42, 58, 0.56))';
      if (s === 'open') return 'linear-gradient(180deg, rgba(138, 98, 76, 0.4), rgba(56, 39, 31, 0.58))';
      return 'rgba(255, 255, 255, 0.05)';
    })()} !important;
  }
```

If two chips represent the same semantic state, keep them the same color. Do not create distinct colors for Tesla vs Wagoneer or similar labels unless the color is encoding meaning rather than identity.

## Single State Summary Buttons

For aggregate status entities like `input_text.wall_display_entry_locks_status`, tint the full card shell based on the summary string.

Current validated semantics:

- secure / all ok / locked: cool green-secure tone
- pending / locking / unlocking: cool blue transitional tone
- anything else: warm alert tone

This works well for cards that summarize multiple devices but only expose one state line.

Pattern:

```yaml
  .bubble-button-card-container {
    background: ${(() => {
      const state = (hass.states['input_text.some_status']?.state || '').toLowerCase();
      if (state.includes('all ok') || state.includes('locked') || state.includes('secure')) {
        return 'linear-gradient(180deg, rgba(58, 88, 82, 0.34), rgba(28, 29, 33, 0.32))';
      }
      if (state.includes('pending') || state.includes('locking') || state.includes('unlocking')) {
        return 'linear-gradient(180deg, rgba(74, 89, 122, 0.34), rgba(28, 29, 33, 0.32))';
      }
      return 'linear-gradient(180deg, rgba(120, 86, 72, 0.34), rgba(41, 32, 31, 0.38))';
    })()} !important;
  }
```

## Top-Row Matching

When two cards live together at the top of the wall display and serve similar navigational roles, match them by:

1. using the same shell treatment
2. keeping similar internal padding
3. keeping title and state typography close in scale
4. avoiding unnecessary differences in height

If one card starts overlapping the next card below it, do not immediately make it larger. First try:

- reducing `.card-content` padding
- slightly reducing title and state font size
- reducing line-height on the state text

Only increase `rows` when the content genuinely cannot fit.

After the final iteration, the more important lesson was:

- matching neighboring cards is easier when they share the same styling strategy

If one compact Bubble card uses direct container overrides and the one beside it does not, they tend to drift apart in perceived size even if their `rows` values are close.

## What Failed

These are known bad or unreliable approaches from this work:

1. Trying to create horizontal spacing in the dock by shrinking per-button width and adding margins.
   Result: Bubble Card reflowed the row and shifted buttons instead of creating clean gaps.

2. Injecting DOM mutations inside the `styles` block, for example:

```yaml
${card.querySelector('.bubble-attribute').innerText = ...}
```

Result: Bubble Card stopped applying the style block and the card fell back to default styling.

3. Assuming generic selectors alone will style media-player transport buttons.
   Result: the card shell changed, but the actual playback controls stayed on their defaults until `.bubble-button-background` and media-player variables were used.

4. Solving card overlap by permanently increasing height before tightening internals.
   Result: the locks button no longer overlapped, but it stopped matching the garage button beside it. Matching top-row cards should be compressed internally first, then only slightly resized if needed.

5. Styling the wall-display media player with direct container-level layout overrides.
   Result: direct rules on `.bubble-button-card-container`, `.card-content`, and `.bubble-sub-button` caused the media card to crash visually into the card below it. For this media-player card, the safe approach is variable-driven styling plus minimal color overrides.

6. Using one layout strategy for locks and another for garage summary cards.
   Result: even when the cards looked similar, they ended up reading as different sizes. Converging both cards on the same variable-driven approach fixed that mismatch more reliably than tweaking `rows` alone.

## Workflow For New Cards

When styling another Bubble Card in this folder:

1. Read the existing YAML first.
2. Identify the `card_type`.
3. Start with the shared frosted shell.
4. Add only the selectors that match the current card type.
5. Prefer built-in Bubble fields like `show_attribute`, `attribute`, `sub_button`, and documented variables over CSS hacks.
6. Validate YAML after edits.
7. Expect at least one visual iteration in Home Assistant.

## Validation

After edits, validate YAML locally:

```bash
python3 - <<'PY'
import yaml
with open('home-assistant/cards/wall-display/bubble-cards/<file>.yaml', 'r', encoding='utf-8') as f:
    yaml.safe_load(f)
print('YAML OK')
PY
```

## Update This Playbook

This file is intentionally short and practical. Update it when we learn:

- a reliable spacing mechanism for Bubble sub-buttons
- card-type-specific selectors for other Bubble cards
- better theme variable reuse from the Frosted Glass theme
- any selectors that behave differently on the installed Bubble Card version
