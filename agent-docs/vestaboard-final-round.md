# Final Round Debug

From GitHub Issue [#32](https://github.com/thaynes43/hass-sandbox/issues/32)

## Fixed

- Name in weird spot
- Name needs to be a reuqired field
- Have to library should say it saved 
- We allow dupe names
- If you edit from the library, you can push the updated but after the editor is as if someone just made a new frame. You need to be able to say you are done editing and have the editor clear. 
- Changing to text editor clears your painting
- Check if text from message grid comes over 
- Move button too many words just should say move 
- Quick save button is a different style, should have feedback it saved
- Fix names of calendar automations
- AI art is usually smooshed to the left 
- keyboard goes away on touch but button not pressed. Need to tap twice 

## Open Bugs

- MessagesFromLibrary & ArtFrom Library configs shift the entire Vestaboard+ list of items. This has been fixed for all other automatins
- "no auto drop" quickly changes back to "no exipry"
- "drops in x" quickly chages back to just the countdown time

## COMPLETED Enhancements

- Sleep Timer so the board doesn't tick overnight 
- Clear grid button on Text editor
- Persist which automations are enabled and disabled so they don't all end up enabled on startup
- Rename Paint and Text to Art and Messages 

## Refactor

- Each automation can be configured as an AppDaemon app.

There could be generic base automation apps we'd configure for different types.

## Ideas

- New automation type that is scheduled by time and can force push to the board. Use this for 7:30-8:30 Weather notification.
- Sundown automation that is dynamic time forced push. Goodnight house.
- Sports! A lot we can do with ESPNs live structured data.
- Tap into Home Assistant to report on things, maybe generic entity / helper-based automations
- Build out random AI messages to be more meaningful. Randomly pick a theme or smart home question to answer.
- Find quote catalog or API and do random quotes. 

## Test Plan

- Test using the AI Art Generator. This should let people generate tile patterns etc without committing them to the board of library. If they get a good one they can then save it. We should see if we can edit on top of what was generated.
- Test persistance. Change things and reboot the app. 
- Click all the buttons
- Write out test plans for interleaving automations and make sure that all behaves as expected. We need to test what happens if an automation triggers multiple times while another has the frame due to it's TTL.
