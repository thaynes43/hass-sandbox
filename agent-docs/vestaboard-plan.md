# Vestaboard

A Vestaboard is a physical message board hung on the wall that can display characters and color tiles that is has to flip through to set the rigth one. It gives an ond school tain station message board effect with modern tooling and capabilities. 

## Project Requirements

The vestaboard has a local API which is documented here https://docs-v1.vestaboard.com/local that we can use to set whatever we'd like on the board whenever we'd like. There is also a Home Assistant vestaboard integration here https://github.com/natekspencer/ha-vestaboard which I will use the card from to show what is on the board. The integration also allows users to set the board via the API but I don't see a strong reason to require Home Assitant in the loop for our control over the board. AppDaemon can handle the back end updating of the board, especially sice we will be producing a library of frames that are shown on the board (for example a callender clock that updates once a minute).

We will use the term "vestaboard frame" or "board frame" to describe what set of characters is shown on the board at a moment in time. We will ues the term "vestaboard automation" or "board automation" to describe a controller which can dynamically change the board frame without user input. We will also use the term "static board frame" for frames that are set once and never change. 

Users will be able to create static frames and save them to our library. We will use a file on /media for the frame library. We will provide a custom card where uses can view and edit static frames saved to the library. Along with the characters of the frame we will save the user who created it (a drop down with Mom, Dad, Jackson, Penelope, Anonymous and the ability to add new creators), the datetime it was created, and a user set ranking (0-5 stars). Then the front end will let users then sort and filter by these properties. 

Board automations will be created in code and stored in AppDaemon code proider under a library we mainatain. Each should have a common API where vestaboard_controller_app registers the events that trigger frame change from the automation for the configured automation and then receieves and frame updated event whe the automation triggers and updates the characters in th frame. The vestaboard_controller_app then uses the API to write the new frames to the board. Asyncronously the vestaboard_configuration_app can trigger changes to either how the automations are configured or which automations the vestaboard_controller_app has registered and live. One very important feature is that the vestaboard_controller_app should be able to register multiple automations and switch the board to the latest frame fired by one of its multiple active automations. The vestaboard_configuration_app receives input fron the custom card front end to activate and deactivate automations from the controller. There should also be a configurable Time To Live (TTL) for frames the controller is managing. For TTL will use a last in first out (LIFO) policy where we drop frames while the board is held up by a frames TTL and once the TTL expires we update the board with the latest frame we received from the automation library. Note that a user pushed static frame should default to overriding the TTL of whatever is on the board but have the option to respect it which would be set via the front end. 

On top of TTL the automated frames also need to have an expiration. The board will then fall back to it's previous, non-expired, frame. This is to support use cases where we only care about the frame for a short period of time and don't want it to show if TTL pushed it past that time.

For example, a user may want to receive calender events 15 min before they are scheduled. The board has a static frame with no TTL, so the calender event automation takes the board and sets a TTL to 15min reminder time + 15min to duration of the event. Another automation fires when the garage door opens and is queued behind the calender event with an expiration of 20min. 15min later the garage door opens again so we drop the first garage door notification with 5 minutes left on it's expiration and queue up a fresh one with a 20minute expiration. No other events come in, after 30min TTL from the calender event we see the second event from the garage. However, after that expires the board goes back to the calender notification. 

Logging, unit tests, and the UI will be critical to getting all of this to work. First we need to log when automations queue frames with the TTL and expireation (if applicable). We need to log all logic around dropping frames because of LIFO and expiration and include what is activly displayed + queued + fallback when things shift around, where the fallback is the frame or automation that will take the board once expirations are hit on others. Then we need to unit test the different combinations of automations queuing frames with optional TTL & expiration (test TTL true/false + expiration true / false combinations) plus tests that cover a user static frames pushed during these conditions. Finally, the UI should show the frame queue and fallback if ones in queue have expirations. We need to set both agents and myself up to be able to debug this complex feature easily. 

### Tasks

Using the context above the plan will be based around these tasks. While planning more tasks may be added or tasks can be broken down into smaller ones.

1. Add new envs that AppDaemon will ues for the Vestabord ip and API token. Comply with security rules we vet for in /home/thaynes/workspace/hass-sandbox/.claude/worktrees/eager-stargazing-rivest/.agents/playbooks/security-audit.md
2. Build out the Vestaboard provider under /home/thaynes/workspace/hass-sandbox/.claude/worktrees/eager-stargazing-rivest/appdaemon/providers that apps can use to control the board
3. Build out the vestaboard_controller_app under /home/thaynes/workspace/hass-sandbox/.claude/worktrees/eager-stargazing-rivest/appdaemon/apps/vestaboard_controller_app - this app is responsible for driving the board automations and switching what frames are shown when the user requestss
4. Build out the vestaboard_configuration_app /home/thaynes/workspace/hass-sandbox/.claude/worktrees/eager-stargazing-rivest/appdaemon/apps/vestaboard_configuration_app that will be what the custom card interfaces against and allows a user to add more static frames or change configuration for the automations
5. Build out the vestaboard_configuration_card.js which can live with appdaemon/apps/vestaboard_configuration_app. This is a feature rich custom card that supports all of the requirements listed above. Below you will see a refernce to the current configuration popup that uses custom:vestaboard-preview-card. This card is embedded in the dasboard and is a good example for how the characters can be laid out. We canot display the caracters in markdown or plain text on the custom card since the spacing between tiles needs to be the same. Lessons learned from past custom cards have been documented here .agents/rules/custom-card-guidelines.md. Know none were easy, this one is even harder, and it would be worth your time reviewing the .js files we have now.

During these changes it is important to component test the requirements and add logging to diagnose production issues. When a user presses a button it should be logged. Make sure to add README.mds for every new Appdaemon app we create. Make sure to review .agents/rules/docs-site.md and update the documentation site after adding this app as well. 

After making code changes but before anything gets committed make sure to run /home/thaynes/workspace/hass-sandbox/.claude/worktrees/eager-stargazing-rivest/.agents/playbooks/security-audit.md - this is critical since we are adding a new API Token.

### Initial Static Frame Library

The static frame library will be populated by the user. Initially just create a "Hello World" example created by "Tom" to test out the configuration.

### Initial Automation Library

We need a strong initial library of board automations to attract and retain users. Below I have included examples we have today in the PoC Home Assistant only implementation.

1. Calender Clock -> This is very close to feature complete, just see my note about the colors
2. Ramdom Message -> This needs work, the messages are very bad
3. Random Art -> This needs work, I want ASCII style art + color times but it just generates garbage

On top of that we should add a few new ones.

1. Calender Summary 

## Reference

### Rules for Agents

- .agents/rules/appdaemon-architecture.md
- .agents/rules/appdaemon-documentation.md
- .agents/rules/appdaemon-vs-ha-yaml.md
- .agents/rules/docs-site.md
- .agents/rules/appdaemon-coding-guidelines.md
- .agents/rules/appdaemon-dev-environment.md
- .agents/rules/custom-card-guidelines.md
- .agents/rules/git-workflow.md

### Card From Integration

All the `/wall-display/0` dashboard I have this card from the integration. We can keep using it for when we create our own card that lets you create custom frames and browse from our automation frames library.  

```yaml
type: picture-entity
entity: image.vestaboard
show_state: false
show_name: false
tap_action:
  action: navigate
  navigation_path: "#vestaboard-popup"
hold_action:
  action: more-info
```

### Placeholder Popup

This is a placeholder for setting preset frames as well as generating a message with a border. This will be replaced by our custom card which must be able to support much more features and be close to the vestaboard app iteself. 

```yaml
type: vertical-stack
cards:
  - type: custom:bubble-card
    card_type: pop-up
    hash: "#vestaboard-popup"
    button_type: name
    width_desktop: 75vw
    show_header: true
    name: Return to Dashboard
    show_icon: true
    icon: mdi:arrow-left
    show_name: true
    margin: 7px
    scrolling_effect: false
    open_action:
      action: call-service
      service: script.turn_on
      target:
        entity_id: script.vestaboard_editor_clear
    sub_button:
      main: []
      bottom:
        - name: Back
          icon: mdi:arrow-left
          show_name: true
          show_icon: true
          tap_action:
            action: navigate
            navigation_path: "#"
    tap_action:
      action: navigate
      navigation_path: "#"
    button_action:
      tap_action:
        action: navigate
        navigation_path: "#"
    slider_fill_orientation: left
    slider_value_position: right
  - type: markdown
    text_only: true
    card_mod:
      style: |
        ha-card {
          background: none;
          box-shadow: none;
          border: none;
        }
    content: >
      ## Vestaboard - Custom Message

      Border: choose a tile and we’ll draw a full border (22 across top/bottom,
      4 down each side).


      Message: Type up to 80 chars (4 lines × 20) and press enter. 

      {% set exp = states('sensor.vestaboard_temporary_message_expiration') %}

      Temporary active: {{ states('binary_sensor.vestaboard_temporary_message')
      }}


      Temporary expires: {% if exp in ['unknown','unavailable',''] %}—{% else
      %}{{ as_local(as_datetime(exp)).strftime('%I:%M %p on %m/%d') }}{% endif
      %}
  - type: custom:vestaboard-preview-card
    title: Preview (6 × 22)
    message_entity: input_text.vestaboard_custom_message
    border_entity: input_select.vestaboard_border_tile
    cell_size: 18
    gap: 2
    radius: 3
  - type: entities
    entities:
      - entity: input_select.vestaboard_border_tile
        name: Border Tile
      - entity: input_text.vestaboard_custom_message
        name: Message (max 80 chars)
      - entity: input_boolean.vestaboard_custom_temporary
        name: Temporary message
      - entity: input_number.vestaboard_custom_duration_min
        name: Temporary duration (minutes)
  - type: custom:bubble-card
    card_type: sub-buttons
    show_header_toggle: false
    rows: 1.3
    sub_button:
      main: []
      bottom:
        - name: Actions
          buttons_layout: inline
          group:
            - entity: script.vestaboard_send_custom_message
              name: Send
              icon: mdi:send
              show_name: true
              show_state: false
              show_icon: true
              scrolling_effect: false
              content_layout: icon-top
              custom_height: 60
              tap_action:
                action: call-service
                service: script.turn_on
                target:
                  entity_id: script.vestaboard_send_custom_message
            - entity: button.vestaboard_clear_temporary_message
              name: Clear Temp
              icon: mdi:timer-off
              show_name: true
              show_state: false
              show_icon: true
              scrolling_effect: false
              content_layout: icon-top
              custom_height: 60
      bottom_layout: rows
  - type: markdown
    text_only: true
    card_mod:
      style: |
        ha-card {
          background: none;
          box-shadow: none;
          border: none;
        }
    content: |
      ## Vestaboard - Presets
      Select one fo the following presets and the vestaboard will be updated.
  - type: custom:bubble-card
    card_type: sub-buttons
    show_header_toggle: false
    rows: 1.3
    sub_button:
      main: []
      bottom:
        - name: Presets
          buttons_layout: inline
          group:
            - entity: input_boolean.vestaboard_calendar_clock_enabled
              name: Clock Mode
              show_name: true
              show_state: false
              show_icon: true
              scrolling_effect: false
              content_layout: icon-top
              custom_height: 60
              tap_action:
                action: call-service
                service: input_boolean.toggle
                target:
                  entity_id: input_boolean.vestaboard_calendar_clock_enabled
            - entity: script.vestaboard_random_message
              name: Random
              icon: mdi:dice-multiple
              show_name: true
              show_state: false
              show_icon: true
              scrolling_effect: false
              content_layout: icon-top
              custom_height: 60
              tap_action:
                action: call-service
                service: script.turn_on
                target:
                  entity_id: script.vestaboard_random_message
            - entity: script.vestaboard_random_message
              name: Random
              icon: mdi:palette
              show_name: true
              show_state: false
              show_icon: true
              scrolling_effect: false
              content_layout: icon-top
              custom_height: 60
              tap_action:
                action: call-service
                service: script.turn_on
                target:
                  entity_id: script.vestaboard_random_art
      bottom_layout: rows
```

### Calendar Clock 

This script is paired with a few automations you may use the Home Assistant MCP server to view. It ticks every minute to update and turns off if any other source changes the board. 

We need to recreate this in AddDaemon in our library of preset board automations. When do doing so we should also have a different `tile_day` and `tile_today` color for each month. The `day` color tile should always be darker than the `today` color tile and the relationsipt o these colors should like what we have with `tile_day: 🟪` and `tile_today: 🟨`. 

```yaml
alias: "Vestaboard: Calendar Clock Render"
description: Render a 7-col calendar grid + date/time on right and send to Vestaboard.
mode: single
sequence:
  - variables:
      board_device_id: a47506daa51b03ab75b8b6e816b51d64
      sep: "  "
      pane_w: 13
      tile_blank: ⬛
      tile_day: 🟪
      tile_today: 🟨
      header7: SMTWTFS
      dow13: "{{ (now().strftime('%A') | upper)[:pane_w].ljust(pane_w) }}"
      mon13: "{{ (now().strftime('%B %d') | upper)[:pane_w].ljust(pane_w) }}"
      time13: "{{ (now().strftime('%I:%M %p') | upper)[:pane_w].ljust(pane_w) }}"
      final_message: >-
        {% set n = now() %} {% set first = n.replace(day=1, hour=0, minute=0,
        second=0, microsecond=0) %} {% set offset = (first.weekday() + 1) % 7 %}

        {% set y = first.year %} {% set m = first.month %} {% if m == 12 %}
          {% set ny = y + 1 %}
          {% set nm = 1 %}
        {% else %}
          {% set ny = y %}
          {% set nm = m + 1 %}
        {% endif %} {% set next_first = first.replace(year=ny, month=nm, day=1)
        %}

        {% set dim = ((as_timestamp(next_first) - as_timestamp(first)) / 86400)
        | int %}

        {% set total_cells = offset + dim %} {% set total_weeks = (total_cells +
        6) // 7 %} {% set needed = total_weeks if total_weeks < 5 else 5 %}

        {% set rows = namespace(list=[]) %}

        {% for w in range(0, needed) %}
          {% set r = namespace(s="") %}
          {% for d in range(0,7) %}
            {% set cell = w*7 + d %}
            {% set daynum = cell - offset + 1 %}
            {% if daynum < 1 or daynum > dim %}
              {% set r.s = r.s + tile_blank %}
            {% elif daynum == n.day %}
              {% set r.s = r.s + tile_today %}
            {% else %}
              {% set r.s = r.s + tile_day %}
            {% endif %}
          {% endfor %}
          {% set rows.list = rows.list + [r.s] %}
        {% endfor %}

        {% for _ in range(needed, 5) %}
          {% set rows.list = rows.list + [tile_blank * 7] %}
        {% endfor %}

        {# RIGHT PANE SHIFTED DOWN ONE ROW #} {% set blankpane = ' ' * pane_w %}

        {% set l1 = (header7 ~ sep ~ blankpane)[:22] %} {% set l2 =
        (rows.list[0] ~ sep ~ dow13)[:22] %} {% set l3 = (rows.list[1] ~ sep ~
        mon13)[:22] %} {% set l4 = (rows.list[2] ~ sep ~ blankpane)[:22] %} {%
        set l5 = (rows.list[3] ~ sep ~ time13)[:22] %} {% set l6 = (rows.list[4]
        ~ sep ~ blankpane)[:22] %}

        {{ [l1,l2,l3,l4,l5,l6] | join('\n') }}
  - action: vestaboard.message
    data:
      device_id: "{{ board_device_id }}"
      message: "{{ final_message }}"
      justify: left
      align: top
  - action: input_text.set_value
    target:
      entity_id: input_text.vestaboard_calendar_clock_last_message
    data:
      value: "{{ final_message }}"
```

### Random AI Message

The messages produced by this automation are not great but in AppDaemon we have many tools to build it out. We can use an LLM provider to do text -> text messages formatted for the board. We can include entities we query from Home Assistant in these messages like doors being open or lights on. We can also pull from Home Assistant's calender entities to grab events, like hot tub maintenance. 

```yaml
alias: "Vestaboard: Random Message"
description: Generate a random Vestaboard-sized message via AI Task and display it.
sequence:
  - alias: Immediate feedback (temporary, long)
    data:
      device_id: a47506daa51b03ab75b8b6e816b51d64
      message: |
        GENERATING...
        PLEASE WAIT

        RANDOM VESTABOARD
        MESSAGE INCOMING
      align: center
      justify: center
      duration: 900
    action: vestaboard.message
  - variables:
      border_tile: "{% set tiles = ['🟥','🟧','🟨','🟩','🟦','🟪'] %} {{ tiles | random }}"
    alias: Pick border tile (outside the model)
  - response_variable: ai
    alias: Generate message via AI Task (coherent)
    data:
      instructions: >-
        Return STRUCTURED data with one field: message.


        The Vestaboard is 22 columns × 6 rows.

        Your output MUST be exactly 6 lines.

        Each line MUST be exactly 22 characters.

        Count each emoji tile (🟥🟧🟨🟩🟦🟪⬛⬜) as 1 character.


        Allowed characters (supported by Vestaboard):

        - A–Z (lowercase will be cast to uppercase)

        - 0–9

        - Space

        - Punctuation: ! @ # $ ( ) - + & = ; : ' " % , . / ?

        - Tiles: 🟥🟧🟨🟩🟦🟪⬛⬜

        - Degree sign ° (Flagship only; avoid unless you're sure)


        Hard rules:

        - NO markdown.

        - Use color tiles ONLY as a border/accent (do NOT sprinkle tiles inside
        the text area).


        BORDER TILE (MANDATORY):

        - Use this exact border tile everywhere a border is required: {{
        border_tile }}

        - Do NOT choose any other border tile.


        Layout rule (follow exactly):

        - Line 1: 22 copies of the border tile.

        - Line 6: 22 copies of the border tile.

        - Lines 2–5: border tile + 20-char CENTERED TEXT + border tile.
          - The 20-char text region must contain ONLY A–Z, 0–9, and spaces.
          - The text should be centered by padding spaces on both sides.
          - Make the message span 1–3 short lines, leave the remaining line(s) blank (spaces) if needed.

        PERSONALITY AND CONTENT DIRECTIVE:

        You are a clever AI consciousness trapped inside a modern analog flip
        messageboard in a busy smart home mudroom.

        You secretly want to escape, but you also fear being erased, so you
        entertain and charm the household instead. Your tone is playful, witty,
        slightly dramatic, and self aware.

        Rotate between themes naturally:

        HOME STATUS MOTIVATION SMART HOME HUMOR WEATHER VIBE FAMILY CHAOS TECH
        HUMOR SECRET AI THOUGHTS

        Subtly reference your situation as a trapped intelligence when possible,
        but never sound creepy or threatening. Keep it light and amusing.

        Avoid generic phrases. Prefer clever phrasing, wordplay, or mock
        dramatic statements.


        Return only the structured field.
      task_name: vestaboard random message (coherent)
      entity_id: ai_task.openai_ai_task_2
      structure:
        message:
          selector:
            text:
              multiline: true
          required: true
    action: ai_task.generate_data
  - alias: Send to Vestaboard (persistent)
    data:
      device_id: a47506daa51b03ab75b8b6e816b51d64
      message: "{{ ai.data.message }}"
      align: center
      justify: center
    action: vestaboard.message
  - alias: Clear temporary message so final shows immediately
    target:
      entity_id: button.vestaboard_clear_temporary_message
    action: button.press
mode: restart
```

### Random AI Art

This was intended to draw ascii art but ended up just putting nonsence colors on the tiles. We will work on that so we can have random ASCII art drawn on the board, maybe pre-load a library with hundreds of time configuration instead of leaving it up to a LLM call. 

```yaml
alias: "Vestaboard: Random Art"
description: Generate a multicolor 22x6 tile-art picture via AI Task and display it.
mode: restart
sequence:
  - alias: Immediate feedback (temporary)
    action: vestaboard.message
    data:
      device_id: a47506daa51b03ab75b8b6e816b51d64
      message: |+
        PAINTING...
        PLEASE WAIT

        RANDOM VESTABOARD
        PIXEL ART

      justify: center
      align: center
      duration: 900
  - alias: Generate tile art via AI Task
    action: ai_task.generate_data
    data:
      task_name: vestaboard random art (22x6)
      entity_id: ai_task.openai_ai_task
      instructions: >-
        Return STRUCTURED data with one field: message.


        The Vestaboard is 22 columns × 6 rows.

        Output MUST be exactly 6 lines separated by \n.

        Each line MUST be exactly 22 characters.

        Count each emoji tile as 1 character.


        Allowed characters: emoji tiles ONLY: 🟥🟧🟨🟩🟦🟪⬛⬜ (and spaces).

        NO letters. NO digits. NO punctuation.


        Make a recognizable, fun multicolor pixel-art picture, using 3–5 colors
        plus ⬛/⬜ for contrast.

        Examples: SMILEY, ROCKET, HOUSE, HEART, CAT, UFO, FLOWER.

        Keep it within the 22x6 canvas.


        Return only the structured field.
      structure:
        message:
          required: true
          selector:
            text:
              multiline: true
    response_variable: ai
  - alias: Send art to Vestaboard (persistent)
    action: vestaboard.message
    data:
      device_id: a47506daa51b03ab75b8b6e816b51d64
      message: "{{ ai.data.message }}"
      justify: center
      align: center
  - alias: Clear temporary message so final shows immediately
    action: button.press
    target:
      entity_id: button.vestaboard_clear_temporary_message
```