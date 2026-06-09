# Vestaboard Fixes & Enhancements

We are in the middle of a large project which was initially kicked off by this promp .claude/worktrees/eager-stargazing-rivest/agent-docs/vestaboard-plan.md. Don't take it as gospel though as things have evolved. 

The apps described there were implemented here:

- .claude/worktrees/eager-stargazing-rivest/appdaemon/apps/vestaboard_configuration_app
- .claude/worktrees/eager-stargazing-rivest/appdaemon/apps/vestaboard_controller_app

## Known Issues

1. .claude/worktrees/eager-stargazing-rivest/appdaemon/apps/vestaboard_configuration_app/vestaboard-configuration-card.js displays what currently on the board. However, it does not take white space in account and justifies everything towards the left with no spacing. You can use black tiles to represent white space or empty grid boxes.
2. ttl_s=None blocks cross-source pushes — When calendar_clock (no TTL) is displayed, calendar_summary frames queue behind it because _ttl_expired() returns False for ttl_s=None. Design decision needed: should None TTL mean "hold forever" or "no protection"? Likely fix: treat ttl_s=None as "no TTL protection" soany new frame can replace it.
3. When you check Auto Expire on the manually pushed static editor the textbox to enter how long to expire doesn't appear. When you click on something else it appears. 
4. There is an artifical delay between when the app starts up and when the card populates with information and the current board state is drawn. This is minor but annoying for testing. 
5. Queue Status says pending automations have expired. They may have erroneously expired or maybe the status is wrong.
6. The Automations check boxes stil say off even when the automations are checked. Maybe this is a different state and they are not running, in that case we should say they are enabled and when they wil push a frame next.
7. Art generation does not work, this is logged:

2026-03-12 00:04:17.581505 INFO vestaboard_configuration_dev: Received command: 'generate_art' payload_len=28
2026-03-12 00:04:17.585772 INFO vestaboard_configuration_dev: Forwarded to controller: generate_art
2026-03-12 00:04:17.587138 INFO vestaboard_controller_dev: Command received: 'generate_art'
2026-03-12 00:04:17.587791 INFO vestaboard_configuration_dev: generate_art subject='A white house' forwarded to controller
2026-03-12 00:04:17.587963 WARNING vestaboard_controller_dev: Unknown command: 'generate_art'

We are suppose to maintain a library of random art that uses can add or remove from. The library should store all art unless a user delets it. Users should be able to star art and messages (0-5) in the library and the stared art or messsage is what is randomly selected for the random art / random message automation. We currently have a generic Library that has both text and art. We can break that into two sections, one for people storing static messages the want to send to the board and the other for people storing either art they made with the Editor or randomly generated. 

## Enhancements 

1. Queue Status should also include upcoming automations that are planned to push frames to the board and how long until they do.
2. On the front end we should call "Automations" something fun, like "Vestaboard Plus". We should make this look more like an asset store where users can purchase the automations to run on their vestaboard. 
3. Expiry should not be a time but a "Should Expire" checkbox where the automation is removed from the board if there is a fallback after the TTL. If not the automation stays until something else is pushed.
4. Automations only allow users to configure TTL and Expiry. These should have default values depending on what the automation is, but we also need rich config per automation so these work right. For CalenderClock TTL is forever, never expire, which is implicit right now by not being set. For RandomMessage (Plase rename to MessagesFromLibrary), RandomArt (Plase rename to ArtFromLibrary), and AIArtGenerator (please rename to ArtGeneratedByAI) we'd want a shorter TTL that is allowed to expire so it doesn't stick around forever. We also need more config for random message so we can set how often random messages are promoted to the board. We can set this as a range and randomly pick a time in the range for each tick of the automation. RandomMessage (MessagesFromLibrary) and RandomArt (ArtFromLibrary) should also have a min stars configuration on the UI so users can select how many stars something from the library has to have to be eligible for random board frame selection. 
5. We should be able to configure in app.yaml multiple CalenderSummary automations with one home assistant calender per. These should have a configurable time that is used as the time before the event takes place that the automation starts pushing the summary frame to the board. The sumary should stay on the board until TTL. We should then have configurable time to rotate events where wait to push the same event to the vestaboard some duration after the TTL. So say Christmas was 10 days away, we could set the time before event to be 10 days, TTL to 30min, and then have it push to the board every 12 hours until the event has passed. If calender events conflict and are at the same time we should simply push the first and queue the others behind the TTL of the first.
6. The Vestaboard Plus page should have previews of each automation the user can select from the asset store eseq UI. We can use the grid UI element we have for the other previous and what is on the board and have a startup routiene where each automation provides the vestaboard_configuration_app what it wants it's preview to be. 

## Design Goals

This card is intended to be used on a 1920x1080 touchscreen and also on a phone so please make sure all of the UI elements are touch screen friendly and large enough that a person could use their finger and not a mouse pointer. 
The UI needs room to grow so likely pagenation as we will continue to create assets for the store and users will continue to create Text and Art frames.

## Implementation & Test

There are may bugs here which may be given to woker agents to go off and figure out. Opus 4.6 agents should be used for all debugging and planning modles. Sonnet may be used for raw implementation against a well defined plan but Opus should implement and front end UI changes as they are the most complecated and the most time consuming for me if done incorrectly.

Unit tests and logging must be updated as changes are made for this plan. Any time a bug is fixed a regression test should be added to prevent it from happening again. Finally, after the implementation of this plan is complete an Opus agent should review the requirements of the plan, the backend implementation, and the frontend buttons and write a manual test plan for me to run through against the front end. I will then report back the result and we can iterate on fixes. 

## References 

- .agents/rules/appdaemon-architecture.md
- .agents/rules/appdaemon-dev-environment.md
- .agents/rules/appdaemon-coding-guidelines.md

For the front end agent this one is critical. Other agents don't need it:

- .agents/rules/custom-card-guidelines.md