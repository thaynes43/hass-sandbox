# Dock-Bar Readme

The dock bar is at the bottom of the Wall Display dashboard. It opens popups which are created using this runbook: /Users/thaynes/src/labspace/hass-sandbox/home-assistant/cards/wall-display/dock-bar/dock-bar-popup-runbook.md.

## Popup to Area Map

This map covers every Home Assistant area on the `Exterior`, `First Floor`, and `Second Floor` floors. It excludes all `Basement` areas and unassigned "Other areas" (`Dracut`, `Unknown`).

Except for `primary-popup`, the card counts below are planned first-pass targets for building each popup.

### shed-popup

Areas pulled in:
- Shed

Area count:
- 1

Popup card count:
- 11

Markdown summary count:
- Sensor/status entities shown in the top markdown summary: 5

### front-yard-popup

Areas pulled in:
- Front Yard
- Front Porch

Area count:
- 2

Popup card count:
- 10

Markdown summary count:
- Sensor/status entities shown in the top markdown summary: 4

### back-yard-popup

Areas pulled in:
- Back Yard

Area count:
- 1

Popup card count:
- 10

Markdown summary count:
- Sensor/status entities shown in the top markdown summary: 4

### driveway-popup

Areas pulled in:
- Driveway
- Garage

Area count:
- 2

Popup card count:
- 10

Markdown summary count:
- Sensor/status entities shown in the top markdown summary: 4

### entrance-popup

Areas pulled in:
- Entrance
- Mudroom
- First Floor Bathroom

Area count:
- 3

Popup card count:
- 16

Markdown summary count:
- Sensor/status entities shown in the top markdown summary: 6

### kitchen-popup

Areas pulled in:
- Kitchen
- Dining Room

Area count:
- 2

Popup card count:
- 17

Markdown summary count:
- Sensor/status entities shown in the top markdown summary: 7

### livingroom-popup

Areas pulled in:
- Livingroom

Area count:
- 1

Popup card count:
- 14

Markdown summary count:
- Sensor/status entities shown in the top markdown summary: 5

### foyer-popup

Areas pulled in:
- Foyer
- Upstairs Foyer
- Study

Area count:
- 3

Popup card count:
- 16

Markdown summary count:
- Sensor/status entities shown in the top markdown summary: 6

### primary-popup

Areas pulled in:
- Primary Bedroom
- Primary Bathroom
- Primary Hallway
- Primary Closet

Area count:
- 4

Popup card count:
- 20

Markdown summary count:
- Sensor/status entities shown in the `Primary Suite` markdown heading: 5

### cloffice-popup

Areas pulled in:
- Primary Cloffice

Area count:
- 1

Popup card count:
- 5

Markdown summary count:
- Sensor/status entities shown in the top markdown summary: 2

### kids-popup

Areas pulled in:
- Blue Room
- Pink Room

Area count:
- 2

Popup card count:
- 16

Markdown summary count:
- Sensor/status entities shown in the top markdown summary: 5

### bbb-popup

Areas pulled in:
- Laundry Room
- White Room
- Kids Bathroom

Area count:
- 3

Popup card count:
- 24

Markdown summary count:
- Sensor/status entities shown in the top markdown summary: 9

## Coverage Check

- Total mapped popups: 12
- Total mapped non-Basement, non-Other areas: 25
- Excluded Basement areas: Basement Bathroom, Basement Hallway, Basement Staircase, Concessions, Movie Room, Rumpus Room, Server Room, Storage Room
- Excluded Other areas: Dracut, Unknown
