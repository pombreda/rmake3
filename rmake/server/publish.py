#
# Copyright (c) rPath, Inc.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#


import os
import time
import traceback

from rmake.lib import pipereader

class _RmakeServerPublisher(object):
    """
        Sends job events from Rmake Server ->  Subscribers
    """
    def __init__(self, logger, db, forkCommand=os.fork):
        self.db = db
        self._fork = forkCommand
        self.logger = logger
        self.errorLimit = 100
        self.timeLimit = 60 * 5 # 5 minutes
        self.recentErrors = []
        self.errorTimes = []
        self._events = {}
        self.reader = None
        # event queuing code - to eventually be moved to a separate 
        # process
        # min length of time between emits
        self._emitEventTimeThreshold = .2
        # max # of issues to queue before emit (overrides time threshold)
        self._emitEventSizeThreshold = 10  
        self._numEvents = 0                # number of queued events
        self._lastEmit = time.time()       # time of last emit
        self._emitPid = 0                  # pid for rudimentary locking

    def addEvent(self, jobId, eventList):
        self._events.setdefault(jobId, []).extend(eventList)
        self._numEvents += len(eventList)

    def emitEvents(self):
        self.harvestErrors()
        if not self._events or self._emitPid:
            return
        if ((time.time() - self._lastEmit) < self._emitEventTimeThreshold
            and self._numEvents < self._emitEventSizeThreshold):
            return
        events = self._events
        self._events = {}
        self.reader, writer = pipereader.makeMarshalPipes()
        pid = self._fork('emitEvents')
        if pid:
            writer.close()
            self._numEvents = 0
            self._lastEmit = time.time()
            self._emitPid = pid
            #self.debug('_emitEvents forked pid %d' % pid)
            return
        self.reader.close()
        try:
            try:
                for jobId, eventList in events.iteritems():
                    self.db.reopen()
                    self._emitEvents(jobId, eventList, writer)
                os._exit(0)
            except Exception, err:
                self.logger.error('Emit Events failed: %s\n%s', err, 
                                  traceback.format_exc())
                os._exit(1)
        finally:
            os._exit(1)

    def _pidDied(self, pid, status, name=None):
        if pid == self._emitPid:
            self._emitPid = 0
            error = self.reader.readUntilClosed(timeout=20)
            for error in self.reader.readUntilClosed(timeout=20):
                self.handleError(error)
            self.reader = None

    def harvestErrors(self):
        if not self.reader:
            return
        error = self.reader.handleReadIfReady()
        while error:
            self.handleError(error)
            error = self.reader.handleReadIfReady()

    def handleError(self, error):
        errorTime, uri, msg = error
        idx = 0
        while ((idx < len(self.errorTimes)) and
               (time.time() - self.errorTimes[idx]) > self.timeLimit):
            idx += 1
        self.errorTimes = self.errorTimes[idx:]
        self.recentErrors = self.recentErrors[idx:]
        if (uri, msg) in self.recentErrors:
            idx = self.recentErrors.index((uri, msg))
            del self.recentErrors[idx]
            del self.errorTimes[idx]
            # it will get readded at the end
        else:
            self.logger.error('Subscriber %s failed: %s', uri, msg)
        self.errorTimes.append(errorTime)
        self.recentErrors.append((uri, msg))
        self.recentErrors = self.recentErrors[-self.errorLimit:]
        self.errorTimes = self.errorTimes[-self.errorLimit:]

    def _emitEvents(self, jobId, eventList, writer):
        """
            Publish events to all listening subscribers.
        """
        eventsBySubscriber = self._getEventListBySubscriber(self.db, jobId,
                                                            eventList)
        for subscriber, eventList in eventsBySubscriber.iteritems():
            try:
                subscriber._receiveEvents(subscriber.apiVersion, eventList)
            except Exception, err:
                writer.send((time.time(), subscriber.uri, str(err)))
        while writer.hasData():
            writer.handleWriteIfReady(sleep=10)

    def _getEventListBySubscriber(self, db, jobId, eventList):
        """
            For the given event list, return events sorted by subscriber.
            @return subscriber -> eventList dictionary.
        """
        subscribersByEvent = db.getSubscribersForEvents(jobId, eventList)
        if not subscribersByEvent:
            return {}
        eventsBySubscriber = {}
        for (event, subEvent), data in eventList:
            if (event, subEvent) not in subscribersByEvent:
                continue
            for subscriber in subscribersByEvent[event, subEvent]:
                eventsBySubscriber.setdefault(subscriber, []).append(
                                                ((event, subEvent), data))
        return eventsBySubscriber
