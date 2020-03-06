#! /usr/bin/env python
# -*- coding: utf-8 -*-
####################
# http://www.indigodomo.com

import indigo
from datetime import datetime, timedelta
import pytz
import dateutil.parser
import json
import time
import pickle
import os
import threading
import Queue

try:
    from googleapiclient.discovery import build
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.auth.transport.requests import Request
    MODULES_INSTALLED = True
except ImportError:
    MODULES_INSTALLED = False

###############################################################################
# globals

SCOPES = ['https://www.googleapis.com/auth/calendar.readonly']
CLIENT_CONFIG_FILENAME = 'google_calendar_client_configuration.json'
CREDENTIAL_FILENAME = 'google_calendar_credentials.json'

LOOK_BACK_DAYS = 7
LOOK_AHEAD_DAYS = 30

INITIALIZE_RETRY_MINUTES = 30.0
CALENDAR_LIST_UPDATE_HOURS = 4.0
DOWNLOAD_EVENTS_MINUTES = 60.0
TRIGGER_LOOP_SECONDS = 60.0
TOO_LATE_AFTER_MINUTES = 60.0

################################################################################
class Plugin(indigo.PluginBase):
    #-------------------------------------------------------------------------------
    def __init__(self, pluginId, pluginDisplayName, pluginVersion, pluginPrefs):
        indigo.PluginBase.__init__(self, pluginId, pluginDisplayName, pluginVersion, pluginPrefs)
        credential_dir = os.path.join(indigo.server.getInstallFolderPath(),'Preferences/Plugins/',pluginId)
        if not os.path.exists(credential_dir):
            os.makedirs(credential_dir)
        self.client_config_path = os.path.join(credential_dir, CLIENT_CONFIG_FILENAME)
        self.credentials_path = os.path.join(credential_dir, CREDENTIAL_FILENAME)
        self.credentials = None
        self.calendar_api = None

        self.device_dict = dict()
        self.calendar_dict = dict()
        self.trigger_dict = dict()
        self.fired_trigger_dict = dict()

        self._authorized = False
        self._initialized = False

    #-------------------------------------------------------------------------------
    def __del__(self):
        indigo.PluginBase.__del__(self)

    #-------------------------------------------------------------------------------
    # Start, Stop and Config changes
    #-------------------------------------------------------------------------------
    def startup(self):
        self.debug = self.pluginPrefs.get('debug_logging',False)
        if self.debug:
            self.logger.debug('Debug logging enabled')

        if not MODULES_INSTALLED:
            self.stopPlugin('Install the Google API Client python modules before using this plugin.  See README for details.', isError=True)
        elif not os.path.exists(self.client_config_path):
            self.stopPlugin('Copy the client configuration file to plugin directory before using this plugin.  See README for details.', isError=True)

        temp_dict = json.loads(self.pluginPrefs.get('firedTriggers','{}'))
        for key,value in temp_dict.items():
            self.fired_trigger_dict[int(key)] = value

    #-------------------------------------------------------------------------------
    def shutdown(self):
        self.pluginPrefs['debug_logging'] = self.debug
        self.pluginPrefs['firedTriggers'] = json.dumps(self.fired_trigger_dict)

    #-------------------------------------------------------------------------------
    def closedPrefsConfigUi (self, valuesDict, userCancelled):
        if not userCancelled:
            self.debug = valuesDict.get('debug_logging',False)
            if self.debug:
                self.logger.debug('Debug logging enabled')

    #-------------------------------------------------------------------------------
    def validatePluginConfigUi(self, valuesDict, typeId, triggerId):
        errorsDict = indigo.Dict()

        if len(errorsDict) > 0:
            return (False, valuesDict, errorsDict)
        else:
            return (True, valuesDict)

    #-------------------------------------------------------------------------------
    def runConcurrentThread(self):
        next_initialize_retry = 0
        next_calendar_list_update = 0
        try:
            while True:
                loop_time = time.time()

                if not self.initialized:
                    # confirm API initialization
                    if loop_time > next_initialize_retry:
                        if not self.initialize_api_client():
                            # only schedule retry if initialation failed
                            next_initialize_retry = loop_time + INITIALIZE_RETRY_MINUTES*60

                if self.initialized:
                    # update list of calendars
                    if loop_time > next_calendar_list_update:
                        self.get_calendars()
                        next_calendar_list_update = loop_time + CALENDAR_LIST_UPDATE_HOURS*60*60

                    # update calendar devices
                    for instance_id, device_instance in self.device_dict.items():
                        if loop_time > device_instance.last_update + DOWNLOAD_EVENTS_MINUTES*60:
                            device_instance.update()
                            self.reloadTriggerCalendar(instance_id)

                # evaluate triggers
                for trigger in self.trigger_dict.values():
                    trigger.queue_evaluation()

                self.sleep(TRIGGER_LOOP_SECONDS - (time.time() - loop_time))

        except self.StopThread:
            pass

    #-------------------------------------------------------------------------------
    def toggle_debug(self):
        if self.debug:
            self.logger.debug('Debug logging disabled')
            self.debug = False
        else:
            self.debug = True
            self.logger.debug('Debug logging enabled')

    #-------------------------------------------------------------------------------
    # device methods
    #-------------------------------------------------------------------------------
    def deviceStartComm(self, device):
        if device.configured:
            if device.deviceTypeId == 'GoogleCalendar':
                self.device_dict[device.id] = GoogleCalendarDevice(device, self.get_events, self.logger)

    #-------------------------------------------------------------------------------
    def deviceStopComm(self, device):
        if device.id in self.device_dict:
            del self.device_dict[device.id]

    #-------------------------------------------------------------------------------
    def validateDeviceConfigUi(self, valuesDict, typeId, devId):
        errorsDict = indigo.Dict()

        if not valuesDict.get('calendarID'):
            errorsDict['calendarID'] = 'Required'

        if len(errorsDict) > 0:
            return (False, valuesDict, errorsDict)
        else:
            valuesDict['address'] = valuesDict['calendarID']
            valuesDict['calendarName'] = self.calendar_dict.get(valuesDict['calendarID'],'')
            return (True, valuesDict)

    #-------------------------------------------------------------------------------
    # device config callback
    def list_calendars(self, filter=None, valuesDict=None, typeId='', targetId=0):
        return [(key,value) for key,value in self.calendar_dict.items()]

    #-------------------------------------------------------------------------------
    # trigger methods
    #-------------------------------------------------------------------------------
    def triggerStartProcessing(self, trigger):
        self.trigger_dict[trigger.id] = GoogleCalendarTrigger(trigger, self.fired_trigger_dict.get(trigger.id,[]), self.logger)
        # start the thread
        self.trigger_dict[trigger.id].start()

    #-------------------------------------------------------------------------------
    def triggerStopProcessing(self, trigger):
        if trigger.id in self.trigger_dict:
            instance = self.trigger_dict[trigger.id]
            self.fired_trigger_dict[trigger.id] = instance.fired_trigger_list
            instance.cancel()
            while instance.is_alive():
                time.sleep(0.1)
            del self.trigger_dict[trigger.id]

    #-------------------------------------------------------------------------------
    def validateEventConfigUi(self, valuesDict, typeId, triggerId):
        errorsDict = indigo.Dict()

        if not valuesDict.get('calendarID',''):
            errorsDict['calendarID'] = 'Required'
        try:
            float(valuesDict.get('timeCount','0'))
        except:
            errorsDict['timeCount'] = 'Must be a number'

        if len(errorsDict) > 0:
            return (False, valuesDict, errorsDict)
        else:
            valuesDict['searchWords'] = valuesDict['searchWords'].lower()
            if float(valuesDict.get('timeCount','0')) == 0.0:
                time_desc = u'at {}'.format(valuesDict['timeField'])
            else:
                time_logic = 'before' if float(valuesDict['timeCount']) >= 0.0 else 'after'
                time_desc = u'{}min {} {}'.format(abs(float(valuesDict['timeCount'])),time_logic,valuesDict['timeField'])
            if valuesDict['searchWords']:
                event_desc = u'events with "{}" in {}'.format(valuesDict['searchWords'],valuesDict['searchField'])
            else:
                event_desc = u'all events'
            calendar_name = indigo.devices[int(valuesDict['calendarID'])].name
            valuesDict['description'] = u'{} of {} from device "{}"'.format(time_desc,event_desc,calendar_name)
            return (True, valuesDict)

    #-------------------------------------------------------------------------------
    def reloadTriggerCalendar(self, device_id=None):
        for instance in self.trigger_dict.values():
            if (device_id == None) or (device_id == instance.calendar_id):
                instance.reload_calendar = True

    #-------------------------------------------------------------------------------
    # action control
    #-------------------------------------------------------------------------------
    def actionControlUniversal(self, action, device):
        instance = self.device_dict[device.id]

        # STATUS REQUEST
        if action.deviceAction == indigo.kUniversalAction.RequestStatus:
            self.logger.info('"{}" status update'.format(device.name))
            instance.update()
            self.reloadTriggerCalendar(device.id)
        # UNKNOWN
        else:
            self.logger.debug(u'"{}" {} request ignored'.format(dev.name, action.deviceAction))

    #-------------------------------------------------------------------------------
    # Google API credentials
    #-------------------------------------------------------------------------------
    def initialize_api_client(self):
        try:
            self.get_credentials()
            self.calendar_api = build('calendar', 'v3', credentials=self.credentials)
            self.initialized = True
        except Exception as e:
            self.logger.error('Google API client failed to initialize')
            self.logger.debug(u'{}: {}'.format(type(e),e))
            self.initialized = False
        return self.initialized

    #-------------------------------------------------------------------------------
    def get_credentials(self):
        if os.path.exists(self.credentials_path):
            with open(self.credentials_path, 'rb') as token:
                self.credentials = pickle.load(token)
        try:
            if self.credentials and self.credentials.valid:
                self.logger.info('Google API credentials valid')
                self.authorized = True
            elif self.credentials and self.credentials.expired and self.credentials.refresh_token:
                self.credentials.refresh(Request())
                self.logger.info('Google API credentials refreshed')
                self.save_credentials()
                self.authorized = True
            else:
                self.authorized = False
                self.logger.error('Complete Oauth flow by selecting "Authorize Access" from plugin menu.')
        except Exception as e:
            self.logger.error(u'Error obtaining Google API credentials')
            self.authorized = False
            self.logger.debug(u'{}: {}'.format(type(e),e))


    #-------------------------------------------------------------------------------
    def complete_oauth_flow(self):
        if not self.authorized:
            try:
                flow = InstalledAppFlow.from_client_secrets_file(self.client_config_path, SCOPES)
                self.credentials = flow.run_local_server(port=0)
                self.logger.info('Google API Oauth flow completed')
                self.save_credentials()
                self.authorized = True
            except Exception as e:
                self.logger.error('Google API Oauth flow failed')
                self.logger.debug(u'{}: {}'.format(type(e),e))
        else:
            self.logger.info('Google API Oauth flow not needed')

    #-------------------------------------------------------------------------------
    def save_credentials(self):
        with open(self.credentials_path, 'wb') as token:
            pickle.dump(self.credentials, token)
        self.logger.info('Google API credentials stored in {}'.format(self.credentials_path))

    #-------------------------------------------------------------------------------
    def get_calendars(self):
        try:
            page_token = None
            calendar_ids = []
            while True:
                calendar_list = self.calendar_api.calendarList().list(pageToken=page_token).execute()
                for calendar_list_entry in calendar_list['items']:
                    self.calendar_dict[calendar_list_entry['id']] = calendar_list_entry['summary']
                page_token = calendar_list.get('nextPageToken')
                if not page_token:
                    break
            self.logger.info(u'Downloaded list of {} available calendars'.format(len(self.calendar_dict)))
        except Exception as e:
            self.logger.error(u'Failed to download list of available calendars')
            self.logger.debug(u'{}: {}'.format(type(e),e))
            self.initialized = False

    #-------------------------------------------------------------------------------
    def get_events(self, calendar_id, look_back, look_ahead):
        try:
            return self.calendar_api.events().list(calendarId=calendar_id,
                                                   timeMin=look_back,
                                                   timeMax=look_ahead,
                                                   singleEvents=True,
                                                   orderBy='startTime').execute()
        except Exception as e:
            self.logger.error(u'Failed to download events')
            self.logger.debug(u'{}: {}'.format(type(e),e))
            self.initialized = False

    #-------------------------------------------------------------------------------
    def _authorized_get(self):
        return self._authorized
    def _authorized_set(self, value):
        if value != self._authorized:
            self._authorized = value
            if value:
                self.logger.info(u'Google API access authorized')
            else:
                self.logger.error(u'Google API access not authorized')
    authorized = property(_authorized_get, _authorized_set)

    #-------------------------------------------------------------------------------
    def _initialized_get(self):
        return self._initialized
    def _initialized_set(self, value):
        if value != self._initialized:
            self._initialized = value
            if value:
                self.logger.info(u'Google API access initialized')
            else:
                self.logger.error(u'Google API access not initialized')
    initialized = property(_initialized_get, _initialized_set)

################################################################################
# Classes
################################################################################
class GoogleCalendarDevice(object):

    #-------------------------------------------------------------------------------
    def __init__(self, device, get_events, logger):
        self.device = device
        self.states = device.states
        if self.states['event_data']:
            self.events = json.loads(self.states['event_data'])
        else:
            self.events = dict()

        self.get_events = get_events
        self.logger = logger

        self.calendar_id = device.pluginProps['calendarID']
        self.calendar_name = device.pluginProps['calendarName']

        self.last_update = 0

    #-------------------------------------------------------------------------------
    def update(self):
        try:
            now = datetime.utcnow()
            look_back  = (now - timedelta(days=LOOK_BACK_DAYS )).isoformat() + 'Z' # 'Z' indicates UTC time
            look_ahead = (now + timedelta(days=LOOK_AHEAD_DAYS)).isoformat() + 'Z' # 'Z' indicates UTC time
            events_result = self.get_events(self.calendar_id, look_back, look_ahead)

            # update event data
            id_list = list()
            for event in events_result.get('items', []):
                event_id = event['id']
                id_list.append(event_id)
                if not event_id in self.events:
                    self.events[event_id] = dict()
                self.events[event_id]['start']       = event['start'].get('dateTime', event['start'].get('date'))
                self.events[event_id]['end']         = event['end'].get('dateTime', event['end'].get('date'))
                self.events[event_id]['summary']     = event.get('summary','').lower()
                self.events[event_id]['description'] = event.get('description','').lower()
                self.events[event_id]['status']      = event.get('status','')
                self.events[event_id]['kind']        = event.get('kind','')
                self.events[event_id]['htmlLink']    = event.get('htmlLink','')
                self.events[event_id]['updated']     = event.get('updated','')
                self.events[event_id]['iCalUID']     = event.get('iCalUID','')

            # remove events no longer in feed
            for event_id in self.events.keys():
                if not event_id in id_list:
                    del self.events[event_id]

            self.states['event_data']    = json.dumps(self.events)
            self.states['event_count']   = len(self.events)
            self.states['last_download'] = datetime.now().isoformat()
            self.states['online']        = True
            self.states['onOffState']    = True
            self.logger.info(u'Downloaded {} events from calendar "{}" for device "{}"'.format(len(self.events),self.calendar_name, self.device.name))
        except Exception as e:
            self.states['online']        = False
            self.states['onOffState']    = False
            self.logger.error(u'Failed to download events from calendar "{}" for device "{}"'.format(self.calendar_name, self.device.name))
            self.logger.debug(u'{}: {}'.format(type(e),e))
        self.device.updateStatesOnServer([{'key':key,'value':value} for key,value in self.states.items()])
        self.last_update = time.time()

################################################################################
class GoogleCalendarTrigger(threading.Thread):

    #-------------------------------------------------------------------------------
    def __init__(self, trigger, fired_trigger_list, logger):
        super(GoogleCalendarTrigger, self).__init__()
        self.daemon       = True
        self.cancelled    = False
        self.queue        = Queue.Queue()

        self.trigger      = trigger
        self.id           = trigger.id
        self.name         = trigger.name

        self.calendar_id  = int(trigger.pluginProps['calendarID'])
        self.search_words = trigger.pluginProps.get('searchWords',u'')
        self.search_field = trigger.pluginProps.get('searchField',u'')
        self.time_count   = int(trigger.pluginProps.get('timeCount','0'))
        self.time_field   = trigger.pluginProps.get('timeField',u'')

        self.logger       = logger

        self.fired_trigger_list = fired_trigger_list
        self._events = dict()
        self.reload_calendar = True

    #-------------------------------------------------------------------------------
    def run(self):
        self.logger.debug(u'"{}" thread started'.format(self.name))
        while not self.cancelled:
            try:
                task = self.queue.get(True,5)
                if task == 'evaluate':
                    self.do_evaluation()
                elif task == 'cancel':
                    self.cancelled = True
                else:
                    self.logger.error(u'"{}" unrecognized task "{}"'.format(self.name,task))
            except Queue.Empty:
                pass
            except Exception as e:
                msg = u'"{}" thread error \n{}'.format(self.name, e)
                if self.plugin.debug:
                    self.logger.exception(msg)
                else:
                    self.logger.error(msg)
        else:
            self.logger.debug(u'"{}" thread cancelled'.format(self.name))

    #-------------------------------------------------------------------------------
    def cancel(self):
        """End this thread"""
        self.queue.put('cancel')

    #-------------------------------------------------------------------------------
    def queue_evaluation(self):
        self.queue.put('evaluate')

    #-------------------------------------------------------------------------------
    def do_evaluation(self):
        # now = datetime.now()
        #https://stackoverflow.com/questions/4530069/how-do-i-get-a-value-of-datetime-today-in-python-that-is-timezone-aware/4530166#4530166
        now = datetime.now(pytz.utc)
        ct_pending = ct_matched = ct_too_late = ct_fired = 0
        for event_id,event in self.events.items():
            # each trigger should only fire once per event
            if event_id not in self.fired_trigger_list:
                ct_pending += 1
                # search for text
                if self.search_words in event[self.search_field]:
                    ct_matched += 1
                    # check the time
                    time_event = dateutil.parser.parse(event[self.time_field])
                    time_to_fire = time_event - timedelta(minutes=self.time_count)
                    time_too_late = time_to_fire + timedelta(minutes=TOO_LATE_AFTER_MINUTES)
                    if (now >= time_too_late):
                        ct_too_late += 1
                    elif (now >= time_to_fire):
                        ct_fired += 1
                        self.logger.debug(u'Fire trigger "{}" for event "{}"'.format(self.name,event['summary']))
                        self.fired_trigger_list.append(event_id)
                        indigo.trigger.execute(self.id)
        self.logger.debug(u'Evaluate trigger "{}": {} events, {} pending, {} matched, {} too late, {} fired'.format(self.name,len(self.events),ct_pending,ct_matched,ct_too_late,ct_fired))


    #-------------------------------------------------------------------------------
    @property
    def events(self):
        if self.reload_calendar:
            # grab the latest download of events
            calendar_dev = indigo.devices[self.calendar_id]
            self._events = json.loads(calendar_dev.states['event_data'])
            self.logger.debug(u'Trigger "{}" updated event list from "{}"'.format(self.name,calendar_dev.name))
            # prune the list of previously-fired triggers
            for event_id in self.fired_trigger_list:
                if event_id not in self._events:
                    self.fired_trigger_list.remove(event_id)
            self.reload_calendar = False
        return self._events


################################################################################
# Utilities
################################################################################
