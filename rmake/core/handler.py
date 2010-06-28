#
# Copyright (c) 2010 rPath, Inc.
#
# This program is distributed under the terms of the Common Public License,
# version 1.0. A copy of this license should have been distributed with this
# source file in a file called LICENSE. If it is not present, the license
# is always available at http://www.rpath.com/permanent/licenses/CPL-1.0.
#
# This program is distributed in the hope that it will be useful, but
# without any warranty; without even the implied warranty of merchantability
# or fitness for a particular purpose. See the Common Public License for
# full details.

"""
The job handler is a state machine unique to each build type that governs the
flow of tasks and status on the dispatcher. It sets build status, creates
tasks for workers to run, and monitors those tasks for completion and failure.

Each handler defines a set of attributes that it wishes to persist each time
the handler's state is updated. These will be recovered in case of a dispatcher
crash, and can be used to determine which tasks need to be run. An ideal
handler arranges its efforts so that the external effects of all previous
states are on permanent storage before it changes state, and that all effects
within the current state can be retried without harm. A recovering handler will
attempt to recreate all tasks within that state, and note the pre-existing
outcomes of any tasks that are still known by the system. It may choose to
restart tasks that were already failed at recovery time.
"""

import logging
from rmake.core.types import RmakeTask, FrozenObject, JobStatus
from twisted.internet import defer
from twisted.python import reflect

log = logging.getLogger(__name__)

JOB_STATE_VERSION = 1

_jobHandlers = {}


def registerHandler(handlerClass):
    assert handlerClass.jobType
    _jobHandlers[handlerClass.jobType] = handlerClass


def getHandlerClass(jobType):
    return _jobHandlers[jobType]


class JobHandler(object):
    __slots__ = ('dispatcher', 'job', 'state', 'tasks', 'clock')
    _save = ('state',)

    jobType = None
    jobVersion = 1
    firstState = 'starting'

    def __init__(self, dispatcher, job):
        self.dispatcher = dispatcher
        self.clock = dispatcher.clock
        self.job = job
        self.state = None
        self.tasks = {}
        self.setup()

    ## State machine methods

    def setup(self):
        pass

    def start(self):
        """Start running a job."""
        assert not self.state
        self.setStatus(100, 'Initializing')
        self._changeState(self.firstState)

    def _changeState(self, state):
        """Move to a new state, persist it, and invoke the state function."""
        if self.state == state:
            return
        self.state = state
        # Wait for the job status to be updated successfully before moving to
        # the next state.
        log.debug("Job %s changing state to %s", self.job.job_uuid, state)
        d = self.dispatcher.updateJob(self.job, frozen_handler=self.freeze())
        d.addCallback(self._runState)
        def eb_failure(reason):
            # Try to fail a job; if it suceeds then change state to 'done'
            d2 = self.failJob(reason)
            d2.addCallback(self._changeState)
            return d2
        d.addErrback(eb_failure)

    def _runState(self, dummy):
        """Run the function for handling a particular state."""
        log.debug("Job %s running state %s", self.job.job_uuid, self.state)
        if self.state == 'done':
            return
        func = getattr(self, self.state, None)
        if func is None:
            raise AttributeError("Handler %s doesn't define a state %r!" %
                    (reflect.qual(type(self)), self.state))
        d = defer.maybeDeferred(func)
        def cb_done(newState):
            # State functions return (or callback) their new state, so pass
            # that state into _changeState after one loop through the reactor
            # (to avoid recursion).
            if newState is None:
                newState = 'done'
            elif not isinstance(newState, str):
                raise TypeError("Handler must return its next state, not %r" %
                        (newState,))
            self.clock.callLater(0, self._changeState, newState)
        d.addCallback(cb_done)
        # Pass func's deferred up so the caller can catch exceptions.
        return d

    ## Changing status and state

    def setStatus(self, codeOrStatus, text=None, detail=None):
        """Change the job's status.

        Either pass a code, text, and optional details, or pass a L{JobStatus}
        object as the first argument.
        """
        if isinstance(codeOrStatus, JobStatus):
            self.job.status = codeOrStatus
        else:
            assert text is not None
            self.job.status.code = codeOrStatus
            self.job.status.text = text
            self.job.status.detail = detail
        self.job.times.ticks += 1
        log.debug("Job %s status is: %s %s", self.job.job_uuid,
                self.job.status.code, self.job.status.text)

        d = self.dispatcher.updateJob(self.job)
        d.addErrback(self.failJob, message="Error setting job status:",
                failHard=self.job.status.failed)
        return d

    def failJob(self, failure, message="Unhandled error in job handler:",
            failHard=False):
        """Log an exception and set the job status to 'failed'.

        @param failure: Failure object to log
        @type  failure: L{twisted.python.failure.Failure}
        @param message: Message to display as the first line in the logfile
        @type  message: C{str}
        @param failHard: If C{True}, log the error but don't set the status.
        @type  failHard: C{bool}
        """
        log.error("%s\n%sJob: %s\n", message, failure.getTraceback(),
                self.job.job_uuid)
        if failHard:
            self.dispatcher.jobDone(self.job.job_uuid)
            # Transition directly to 'done' state, but short-circuit _setState
            # so it doesn't try to persist the handler -- it'll probably fail
            # if we've already failed twice to set status.
            self.state = 'done'
            return defer.succeed('done')

        status = JobStatus.from_failure(failure, "Job failed")
        d = self.setStatus(status)
        # If setting status fails we'll have to forget about the job and hope
        # for the best. Perhaps in the future we can handle short-term database
        # faults more gracefully.
        @d.addErrback
        def set_status_failed(failure):
            log.error("Failed to set job status to failed:\n%s",
                    failure.getTraceback())
            self.dispatcher.jobDone(self.job.job_uuid)
        # Transition directly to 'done' state
        d.addBoth(lambda _: 'done')
        return d

    ## Creating/monitoring tasks

    def newTask(self, taskName, taskType, data):
        if not isinstance(data, FrozenObject):
            data = FrozenObject.fromObject(data)
        task = RmakeTask(None, self.job.job_uuid, taskName, taskType, data)

        d = defer.Deferred()
        self.tasks[task.task_uuid] = (d, [])

        d2 = self.dispatcher.createTask(task)
        def eb_create_failed(reason):
            del self.tasks[task.task_uuid]
            d.errback(reason)
        d2.addErrback(eb_create_failed)

        return task

    def taskUpdated(self, task):
        d, callbacks = self.tasks[task.task_uuid]
        for func, args, kwargs in callbacks:
            func(task, *args, **kwargs)
        if task.status.final:
            d.callback(task)
            del self.tasks[task.task_uuid]

    def waitForTask(self, task):
        return self.tasks[task.task_uuid][0]

    ## Freezer machinery

    @classmethod
    def _getVersion(cls):
        return (JOB_STATE_VERSION, cls.jobVersion)

    def freeze(self):
        state_dict = {}
        NOT_SET = object()
        for cls in type(self).mro():
            slots = cls.__dict__.get('_save', ())
            for slot in sorted(slots):
                value = getattr(self, slot, NOT_SET)
                if value is not NOT_SET:
                    state_dict[slot] = value
        data = (self._getVersion(), self.job.data, state_dict)
        return FrozenObject.fromObject(data)
