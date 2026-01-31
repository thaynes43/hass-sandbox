# Home Assistant YAML Sandbox

This repo was created becuase I stopped managing my YAML configs for Home Assistant directly and trusted the UI to keep all that in order. But then Cursor happened. And now I want at least some automations / scenes / cards / sensors YAML in the same repo so it can be used as context to spin up others. 

> Right now I am just copy pasting into this repo Home Assistant, iterating on things, then saving back into Home Assistant via it's YAML editor UI. This feels pretty good so far because I don't need to worry about screwing too much up but it's obviously flawed in how quickly things can get out of sync. Longer term I may write a script that pulls certain things from my /config folder into here. 

The driver for using Cursor for some automations & scripts was because gpt-5.2 was crushin each one in isolation but once I added depenencies across scripts and states I needed something with wider context. 

## Automations & Scripts

There's a handful of automations and scripts in here. Some were originally made with the UI and now here so I don't miss small updates and others got wild. 

This repo started as a folder with the scripts in `scripts\inovelli\generic` to work out a few kinks between suspending the occupancy basaed lighting (or putting it "on hold"). Each switch had a toggle to enable / disable the automations and pramas that contributed to this but I wanted a "hold all" automation on top of that. The switch click hold needed to take precedent over the "hold all" so I introduced two states of being "on hold". Things got more complex from there but has been a breeze to manage w/ Cursor and GPT-5.2.

## Cards

I found mmWave sensors to take a bit of tuning their config to work out a few kinks. Before I installed the inovelli switches I was doing this a dump of entity history and current states. I got a bit carried away here but want to upgrade  my other dashboards and use bubble cards so took the oppertunity to dig in.

### Occupancy Based Lighting Popup

I use a button to open a popup bubble card that shows the basics of what I want to tweak and monitor a zone of lights controlled by motion & presence sensors. This has been very useful when a zone turns on unexpectedly. I can quickly see which sensor popped it and make some quick adjustments like setting an interference zone.

[`img/rumpus-room-occpancy-home.png`](img/rumpus-room-occpancy-home.png)

### Advanced Settings Popup

The button at the top of each popup lets you navigte back and forth. This card is a bit more verbose and gets into settings you wouldn't be changing much. It's nice to make small changes and monitor the zone until things are rock solid. 

[`img/rumpus-room-occpancy-advanced.png`](img/rumpus-room-occpancy-advanced.png)