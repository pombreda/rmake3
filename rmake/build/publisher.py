#
# Copyright (c) 2006-2007 rPath, Inc.  All Rights Reserved.
#
"""
    Internal publisher for jobs and troves.  See build/subscriber.py for
    subscribers.  Jobs and troves trigger this publisher when their state
    changes.
"""

import traceback

from conary.lib import log

from rmake.lib import apirpc


class JobStatusPublisher(object):
    states = set(['TROVE_LOG_UPDATED',
                  'TROVE_LOADED',
                  'TROVE_STATE_UPDATED',
                  'TROVE_PREPARING_CHROOT',
                  'TROVE_BUILDING',
                  'TROVE_BUILT',
                  'TROVE_PREBUILT',
                  'TROVE_RESOLVING',
                  'TROVE_RESOLVED',
                  'TROVE_DUPLICATE',
                  'TROVE_PREPARED',
                  'TROVE_FAILED',
                  'JOB_LOG_UPDATED',
                  'JOB_STATE_UPDATED',
                  'JOB_TROVES_SET',
                  'JOB_COMMITTED',
                  'JOB_LOADED',
                  'JOB_FAILED',
        ])

    # these methods are called by the job and trove objects.
    # The publisher then publishes the right signal(s).

    def jobStateUpdated(self, job, state, status, *args):
        self._emit(self.JOB_STATE_UPDATED, state, job, state, status)
        if job.isFailed():
            self._emit(self.JOB_FAILED, '', job, *args)
        elif job.isLoaded():
            self._emit(self.JOB_LOADED, '', job, *args)

    def jobLogUpdated(self, job, message):
        self._emit(self.JOB_LOG_UPDATED, '', job, job.state, message)

    def buildTrovesSet(self, job):
        self._emit(self.JOB_TROVES_SET, '', job, list(job.iterTroveList(True)))

    def troveResolved(self, trove, resolveResult):
        self._emit(self.TROVE_RESOLVED, '', trove, resolveResult)

    def jobCommitted(self, job, troveTupleList):
        self._emit(self.JOB_COMMITTED, '', job, troveTupleList)

    def troveStateUpdated(self, buildTrove, state, oldState, *args):
        self._emit(self.TROVE_STATE_UPDATED, state, buildTrove, 
                   state, buildTrove.status)
        if buildTrove.isPreparing():
            self._emit(self.TROVE_PREPARING_CHROOT, '', buildTrove, *args)
        elif buildTrove.isResolving():
            self._emit(self.TROVE_RESOLVING, '', buildTrove, *args)
        if buildTrove.isBuilt():
            self._emit(self.TROVE_BUILT, '', buildTrove, *args)
        if buildTrove.isPrebuilt():
            self._emit(self.TROVE_PREBUILT, '', buildTrove, *args)
        if buildTrove.isDuplicate():
            self._emit(self.TROVE_DUPLICATE, '', buildTrove, *args)
        elif buildTrove.isBuilding():
            self._emit(self.TROVE_BUILDING, '', buildTrove, *args)
        elif buildTrove.isFailed():
            self._emit(self.TROVE_FAILED, '', buildTrove, *args)
        if buildTrove.isPrepared():
            self._emit(self.TROVE_PREPARED, '', buildTrove, *args)

    def troveLogUpdated(self, buildTrove, message):
        self._emit(self.TROVE_LOG_UPDATED, '', buildTrove, buildTrove.state,
                   message)
