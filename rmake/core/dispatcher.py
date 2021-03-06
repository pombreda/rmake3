#
# Copyright (c) SAS Institute Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#


"""
The dispatcher is responsible for moving a job through the build workflow.

It creates jobs, assigns them to nodes, and monitors the progress of the jobs.
Status updates are routed back to clients and to the database.
"""


import errno
import logging
import os
import random
import stat
from conary.lib import util
from rmake import errors
from rmake.core import admin
from rmake.core import constants as core_const
from rmake.core import database as coredb
from rmake.core import log_server
from rmake.core import support
from rmake.core import types
from rmake.core.handler import getHandlerClass
from rmake.errors import RmakeError
from rmake.lib import dbpool
from rmake.lib import rpc_pickle
from rmake.lib import structlog
from rmake.lib import uuid
from rmake.lib.apirpc import RPCServer, expose
from rmake.lib.logger import logFailure
from rmake.lib.twisted_extras import deferred_service
from rmake.lib.twisted_extras.firehose import FirehoseResource
from rmake.lib.twisted_extras.ipv6 import TCP6Server
from rmake.messagebus import message
from twisted.application.internet import UNIXServer
from twisted.web.resource import Resource
from twisted.web.server import Site


log = logging.getLogger(__name__)

# Protocol versions of the launcher that are supported by the dispatcher
PROTOCOL_VERSIONS = set([3])


class Dispatcher(deferred_service.MultiService, RPCServer):

    def __init__(self, cfg, plugin_mgr, clock=None):
        deferred_service.MultiService.__init__(self)
        RPCServer.__init__(self)
        self.cfg = cfg

        self.db = None
        self.pool = None
        self.plugins = plugin_mgr
        self.firehose = None
        self.logServer = None

        if clock is None:
            from twisted.internet import reactor
            self.clock = reactor
        else:
            self.clock = clock

        self.jobs = {}
        self.jobLoggers = {}
        self.workers = {}
        self.tasks = {}
        self.taskQueue = []

        self.plugins.p.dispatcher.pre_setup(self)
        self._start_db()
        self._start_bus()
        self._start_rpc()
        self.plugins.p.dispatcher.post_setup(self)

    def _start_db(self):
        coredb.populateDatabase(self.cfg.databaseUrl)
        self.pool = dbpool.ConnectionPool(self.cfg.databaseUrl)
        self.pool.setServiceParent(self)
        self.db = coredb.CoreDB(self.pool)

    def _start_bus(self):
        self.bus = support.DispatcherBusService(self, self.cfg)
        self.bus.setServiceParent(self)

        support.WorkerChecker(self).setServiceParent(self)
        support.JobPruner(self).setServiceParent(self)

    def _start_rpc(self):
        # Child controllers
        admin.AdminController(self).setServiceParent(self)

        root = Resource()
        root.putChild('picklerpc', rpc_pickle.PickleRPCResource(self))
        self.firehose = FirehoseResource()
        root.putChild('firehose', self.firehose)
        self.logServer = log_server.LogTreeManager(self.cfg.jobLogDir)
        root.putChild('logs', self.logServer.getResource())
        site = Site(root, logPath=self.cfg.logPath_http)
        if self.cfg.listenPath:
            try:
                st = os.lstat(self.cfg.listenPath)
            except OSError, err:
                if err.errno != errno.ENOENT:
                    raise
            else:
                if not stat.S_ISSOCK(st.st_mode):
                    raise RuntimeError("Path '%s' exists but is not a socket" %
                            (self.cfg.listenPath,))
                os.unlink(self.cfg.listenPath)

            UNIXServer(self.cfg.listenPath, site).setServiceParent(self)
        if self.cfg.listenPort:
            TCP6Server(self.cfg.listenPort, site,
                    interface=self.cfg.listenAddress).setServiceParent(self)

    ## Client API

    @expose
    def getJobs(self, job_uuids):
        return self.db.getJobs(job_uuids)

    def _jobLogDir(self, job):
        return os.path.join(self.cfg.jobLogDir, str(job.job_uuid))

    @expose
    def createJob(self, job, callbackInTrans=None, firehose=None):
        """Add the given job the database and start running it.

        @param job: The job to add.
        @type  job: L{rmake.core.types.RmakeJob}
        @param callbackInTrans: A function to call inside the database thread
            to perform additional database operations within the same
            transaction.
        @type  callbackInTrans: C{callable}
        @param firehose: Firehose session ID that will be subscribed to the new
            job.
        @type firehose: C{str}
        @return: C{Deferred} fired with a reconstituted C{RmakeJob} upon
            completion.
        """
        # This dance makes sure that if the job data is a frozen object, it
        # doesn't get replaced with a thawed version unintentionally.
        if not isinstance(job.data, types.FrozenObject):
            data = types.FrozenObject.fromObject(job.data)
            job = job._replace(data=data)

        job = job.thaw()

        try:
            handlerClass = getHandlerClass(job.job_type)
        except KeyError:
            raise RmakeError("Job type %r is unsupported" % job.job_type)

        logManager = structlog.JobLogManager(self._jobLogDir(job))
        jobLog = logManager.getLogger()
        self.logServer.setNodeActive(logManager.getPath(None), True)

        handler = handlerClass(self, job, jobLog)

        if firehose:
            try:
                sid = uuid.UUID(str(firehose))
            except ValueError:
                raise RmakeError("Invalid firehose session ID")
            self.firehose.subscribe(('job', str(job.job_uuid)), sid)

        d = self.db.createJob(job, None, callbackInTrans)
        @d.addCallback
        def post_create(newJob):
            log.info("Job %s of type '%s' started", newJob.job_uuid,
                    newJob.job_type)
            self.jobs[newJob.job_uuid] = handler
            self.jobLoggers[newJob.job_uuid] = logManager
            handler.start()

            # Note that the handler will immediately send a new status, so no
            # point in sending it here.
            self._publish(job, 'self', 'created')

            return newJob
        return d

    @expose
    def getWorkerList(self):
        # In the future, this will return some information about each worker,
        # but for now it's just the JID.
        return dict((x.full(), None)
                for x in self.bus.getNeighborList())

    def _publish(self, job, category, data):
        if not isinstance(job, uuid.UUID):
            job = job.job_uuid
        event = ('job', str(job), category)
        self.firehose.publish(event, data)

    @expose
    def deleteJobs(self, job_uuids):
        d = self.db.getJobs(job_uuids)
        @d.addCallback
        def got_jobs(result):
            for job_uuid, job in zip(job_uuids, result):
                if not job:
                    raise errors.JobNotFound(str(job_uuid))
                if not job.status.final:
                    raise RmakeError("Can't delete a running job")
            for job in result:
                jobLogDir = self._jobLogDir(job)
                if os.path.exists(jobLogDir):
                    util.rmtree(jobLogDir)
            return self.db.deleteJobs(job_uuids)
        return d

    # Job handler API

    def jobDone(self, job_uuid):
        if job_uuid not in self.jobs:
            return

        status = self.jobs[job_uuid].job.status
        if status.completed:
            result = 'done'
        elif status.failed:
            result = 'failed'
        else:
            result = 'finished'
        log.info("Job %s %s: %s", job_uuid, result, status.text)

        self._publish(job_uuid, 'self', 'finalized')
        self._setLogActive(job_uuid, None, False)

        # Discard tasks that are out for processing
        handler = self.jobs[job_uuid]
        for task_uuid in handler.tasks:
            task_info = self.tasks.pop(task_uuid, None)
            if task_info and task_info.worker:
                log.debug("Discarding task %s from running set", task_uuid)
                task_info.worker.tasks.pop(task_uuid, None)

        # Discard tasks that never got assigned
        for task in self.taskQueue[:]:
            if task.job_uuid == job_uuid:
                log.debug("Discarding task %s from queue", task.task_uuid)
                self.taskQueue.remove(task)

        logManager = self.jobLoggers.pop(job_uuid, None)
        if logManager:
            logManager.close()
        del self.jobs[job_uuid]

    def updateJob(self, job, frozen_handler=None):
        d = self.db.updateJob(job, frozen_handler=frozen_handler)
        @d.addCallback
        def post_update(newJob):
            if not newJob:
                # Superceded by another update
                return None
            self._publish(newJob, 'status', newJob.status.freeze())
            if newJob.status.final:
                self.jobDone(newJob.job_uuid)
            return newJob
        return d

    def createTask(self, task):
        d = self.db.createTask(task)
        def cb_post_create(newTask):
            newTask = newTask.thaw()
            handler = self.jobs[newTask.job_uuid]
            self.tasks[newTask.task_uuid] = TaskInfo(newTask, handler)
            self.taskQueue.append(newTask)
            self._setLogActive(newTask.job_uuid, newTask.task_uuid, True)
            # Try to assign the task immediately
            self._assignTasks()
            return newTask
        d.addCallback(cb_post_create)

        # Notify handler of initial task status, but not if it already failed
        # because the fail-ing entity will have done so already.
        d.addCallback(self._taskUpdated, onlyIfRunning=True)

        d.addErrback(self._failJob, task.job_uuid)
        return d

    def _failJob(self, failure, job_uuid):
        handler = self.jobs.get(job_uuid)
        if not handler:
            return
        handler.failJob(failure, "Unhandled error in dispatcher:")

    def _setLogActive(self, job_uuid, task_uuid, active):
        logManager = self.jobLoggers.get(job_uuid)
        if not logManager:
            return
        logPath = logManager.getPath(task_uuid)
        if active:
            self.logServer.setNodeActive(logPath, True)
        else:
            # Allow time for any misordered messages in the pipe to take effect
            # before finalizing.
            # TODO: close the logfile here, too
            self.clock.callLater(1, self.logServer.setNodeActive, logPath,
                    False)

    def getLogPath(self, job_uuid, task_uuid=None):
        logManager = self.jobLoggers.get(job_uuid)
        return logManager.getPath(task_uuid)

    def getAllLogPaths(self, job_uuid):
        logManager = self.jobLoggers.get(job_uuid)
        return logManager.getAllPaths()

    ## Message bus API

    def updateTask(self, task):
        d = self.db.updateTask(task)
        d.addCallback(self._taskUpdated)
        d.addErrback(self._failJob, task.job_uuid)
        d.addErrback(logFailure)

    def _taskUpdated(self, newTask, onlyIfRunning=False):
        if not newTask:
            # Superceded
            return
        # If the task is finished, remove it from the assigned node and try to
        # assign more tasks.
        if newTask.status.final:
            if onlyIfRunning:
                return
            info = self.tasks.pop(newTask.task_uuid, None)
            if info and info.worker:
                info.worker.tasks.pop(newTask.task_uuid, None)
            self.clock.callLater(0, self._assignTasks)
            self._setLogActive(newTask.job_uuid, newTask.task_uuid, False)
        handler = self.jobs.get(newTask.job_uuid)
        if handler:
            handler.taskUpdated(newTask)

    def workerHeartbeat(self, jid, msg):
        worker = self.workers.get(jid)
        if worker is None:
            log.info("Worker %s connected", jid.full())
            worker = self.workers[jid] = WorkerInfo(jid)
            # We need to fully initialize the worker before the worker_up hook
            # is called
            worker.setCaps(msg)
            self.plugins.p.dispatcher.worker_up(self, worker)
        else:
            worker.setCaps(msg)
        self._assignTasks()

    def workerDown(self, jid):
        worker = self.workers.get(jid)
        if worker is None:
            return
        log.info("Worker %s disconnected", jid.full())

        for info in worker.tasks.values():
            task = info.taskForUpdate()
            task.status = types.JobStatus(400,
                    "The worker processing this task has gone offline.")
            self.updateTask(task)
        del self.workers[jid]

        self.plugins.p.dispatcher.worker_down(self, worker)

    def workerLogging(self, records, job_uuid, task_uuid):
        logManager = self.jobLoggers.get(job_uuid)
        if logManager is None:
            return
        logManager.emitMany(records, task_uuid)
        logPath = logManager.getPath(task_uuid)
        self.logServer.touchNode(logPath)

    ## Task assignment

    def _assignTasks(self):
        # Sort by priority but preserve the insertion order within each level
        buckets = {}
        for task in self.taskQueue:
            buckets.setdefault(task.task_priority, []).append(task)
        for priority, tasks in sorted(buckets.iteritems()):
            for task in tasks:
                result = self._assignTask(task)
                if result != core_const.A_LATER:
                    # Task is no longer queued (assigned or failed)
                    self.taskQueue.remove(task)
                if result == core_const.A_NOW:
                    # Update task now that node_assigned is set.
                    self.updateTask(task)

    def _assignTask(self, task):
        """Attempt to assign a task to a node.

        If it is not immediately assignable, it is queued.

        @return: A_NOW if the task was assigned, A_LATER if the task should be
            queued, or A_NEVER if the task cannot be assigned.
        """
        log.debug("Trying to assign task %s of job %s", task.task_uuid,
                task.job_uuid)
        scores = {}
        laters = 0
        wrong_zone = 0
        for worker in self.workers.values():
            if not worker.active:
                log.debug("Worker %s is offline and can't run new tasks",
                        worker.jid.full())
                continue
            result, score = self._scoreTask(task, worker)
            if result == core_const.A_NOW:
                log.debug("Worker %s can run task %s now: score=%s",
                        worker.jid.full(), task.task_uuid, score)
                scores.setdefault(score, []).append(worker.jid)
            elif result == core_const.A_LATER:
                log.debug("Worker %s can run task %s later", worker.jid.full(),
                        task.task_uuid)
                laters += 1
            else:
                if result == core_const.A_WRONG_ZONE:
                    wrong_zone += 1
                log.debug("Worker %s cannot run task %s", worker.jid.full(),
                        task.task_uuid)

        if scores:
            # The task is assignable now.
            best = sorted(scores)[-1]
            jid = random.choice(scores[best])
            self._sendTask(task, jid)
            return core_const.A_NOW
        elif laters:
            # Queue the task for later.
            return core_const.A_LATER
        else:
            # No worker can run this task.
            if wrong_zone:
                error = "No capable workers are in the requested zone."
            else:
                error = "No workers are capable of running this task."
            self.clock.callLater(0, self._failTask, task, error)
            return core_const.A_NEVER

    def _scoreTask(self, task, worker):
        # Task must be supported
        if types.TaskCapability(task.task_type) not in worker.caps:
            return core_const.A_NEVER, None
        # Task must be in no zone or this zone
        if (task.task_zone is not None and types.ZoneCapability(
                task.task_zone) not in worker.caps):
            return core_const.A_WRONG_ZONE, None
        # Use slot logic and custom logic from job handler
        handler = self.jobs[task.job_uuid]
        return handler.scoreTask(task, worker)

    def _sendTask(self, task, jid):
        log.debug("Assigning task %s to worker %s", task.task_uuid, jid)

        # Internal accounting
        task.node_assigned = jid.full()
        worker = self.workers[jid]
        info = self.tasks[task.task_uuid]
        info.worker = worker
        worker.tasks[task.task_uuid] = info

        # Send the task to the worker node
        msg = message.StartTask(task.freeze())
        self.bus.sendTo(jid, msg)

    def _failTask(self, task, message):
        log.error("Task %s failed: %s", task.task_uuid, message)
        task.times.ticks = types.JobTimes.TICK_OVERRIDE
        task.status.code = core_const.TASK_NOT_ASSIGNABLE
        task.status.text = "Task failed: %s" % (message,)
        self.updateTask(task)


class WorkerInfo(object):

    def __init__(self, jid):
        self.jid = jid
        self.caps = types.CapabilitySet()
        self.tasks = {}
        self.slots = {}
        self.addresses = set()
        self.protocol = 0
        self.active = None
        # expiring is incremented each time WorkerChecker runs and zeroed each
        # time the worker heartbeats. When it gets high enough, the worker is
        # assumed dead.
        self.expiring = 0

    def setCaps(self, msg):
        self.caps = types.CapabilitySet(msg.caps)
        if isinstance(msg.slots, (int, long)):
            self.slots = {None: msg.slots}
        else:
            self.slots = msg.slots
        self.addresses = msg.addresses
        self.expiring = 0

        vcap = self.caps[types.VersionCapability]
        if vcap:
            assert len(vcap) == 1
            versions = set(vcap.pop().versions)
        else:
            # Version 1 didn't advertise a version
            versions = set([1])
        if versions & PROTOCOL_VERSIONS:
            self.active = True
            self.protocol = sorted(versions & PROTOCOL_VERSIONS)[-1]
        else:
            if self.active is not False:
                us, them = max(PROTOCOL_VERSIONS), max(versions)
                log.error("Worker %s is not running a version of rMake "
                        "compatible with this dispatcher (worker: %r, "
                        "required: %r)", self.jid.full(), them, us)
            self.active = False

    def supports(self, caps):
        """Return C{True} if the worker supports all of C{caps}."""
        for cap in caps:
            if cap not in self.caps:
                return False
        return True

    @property
    def zoneNames(self):
        return [ x.zoneName for x in self.caps[types.ZoneCapability] ]

class TaskInfo(object):

    def __init__(self, task, handler):
        self.task_uuid = task.task_uuid
        self._task = task.freeze()
        self.handler = handler
        self.worker = None

    def taskForUpdate(self):
        task = self._task.thaw()
        task.task_data = None
        task.times.ticks = types.JobTimes.TICK_OVERRIDE
        return task
