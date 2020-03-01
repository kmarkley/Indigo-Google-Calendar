# Google Calendar Indigo Plugin

## What is it?

An indigo plugin that allows triggering a defined number of minutes before or after the start or end time of events on your Google Calendar that contain defined text in the summary or description.

So there!

## Installation

*This is _not_ a plug-and-play plugin.  You'll need to do some things to make it work.*

#### 1. Enable google API and save credentials file

+ Follow step 1 here: https://developers.google.com/calendar/quickstart/python
+ Make note of where you saved the credential file.  You will need this later.

#### 2. Install python modules (in the indigo-approved way)

See here for reference and help: https://forums.indigodomo.com/viewtopic.php?f=107&t=19129&p=145969&hilit=virtualenv#p145969

Should be something like:
```
sudo easy_install pip
sudo pip install --upgrade google-api-python-client google-auth-httplib2 google-auth-oauthlib --ignore-installed six
```

#### 3. Install the plugin
You will get an error:
    + If the error says you need to install modules, go back and fix step 2
    + If the error says you need to copy a file, you're ok for now.

#### 4. Move the credential file downloaded above
+ Rename the file 'google_calendar_client_configuration.json'
+ Move it to the plugin preference folder created when you enabled the plugin

```
/Library/Application Support/Perceptive Automation/Indigo 7.4/Preferences/Plugins/com.morris.google-calendar/
```
(The indigo version number might be different)

#### 5. Authorize access to your google calendar

+ Reload the plugin and hopefully get no errors this time.
+ Select 'Authorize Access' from the plugin menu.  This should open a web browser where you can grant access.

## Use

1. Create a 'Google Calendar' indigo device for each calendar you want to trigger from.
2. Create triggers to fire before/after certain calendar events occur.

## Misc Info

+ Currently calendar devices only download events 7 days before and 30 days after today.  There's no point setting triggers outside this time range.
+ Each trigger should only fire once for a given event. In order to ensure this, and to avoid triggers firing on every event when first created, triggers will ignore anything that should have fired more than an hour previous.
+ I'm not very knowledgeable about python module installations.  If you have trouble with step 2 I'm probably not going to be any help
+ The granularity for triggers is about 1 minute
+ Calendar events are updated every hour.  You can also force an update via a status request action

## To do

+ A trigger for when there are authorization issues, so you can be notified it needs attention
