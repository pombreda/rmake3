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


import itertools
import os

from conary.lib.sha1helper import md5FromString
from conary import dbstore

from rmake import errors
from rmake.build import buildcfg
from rmake.build.subscriber import _JobDbLogger
from rmake.db import authcache
from rmake.db import schema
from rmake.db import jobstore
from rmake.db import logstore
from rmake.db import nodestore
from rmake.db import subscriber

class DBInterface(object):
    def __init__(self, db):
        self._holdCommits = False
        self.db = db
        self.schemaVersion = self.loadSchema(migrate=True)

    def _getOne(self, cu, key):
        try:
            cu = iter(cu)
            res = cu.next()
            assert(not(list(cu))) # make sure that we really only
                                 # got one entry
            return res
        except:
            raise KeyError, key


    def cursor(self):
        return self.db.cursor()

    def commitAfter(self, fn, *args, **kw):
        """
            Commits after running a function
        """
        self._holdCommits = True
        self.db.transaction()
        try:
            rv = fn(*args, **kw)
            self._holdCommits = False
            self.commit()
            return rv
        except:
            self.rollback()
            self._holdCommits = False
            raise

    def commit(self):
        if not self._holdCommits:
            return self.db.commit()
        else:
            return True

    def rollback(self):
        return self.db.rollback()

    def inTransaction(self):
        return self.db.inTransaction()

    def reopen(self):
        if self.db:
            self.db.close_fork()
        self.db = self.open()

    def close(self):
        self.db.close_fork()
        self.db = None

class Database(DBInterface):

    def __init__(self, path, contentsPath, clean = False):
        self.driver, self.dbpath = path

        if os.path.exists(self.dbpath) and clean:
            os.unlink(self.dbpath)

        db = self.open()
        DBInterface.__init__(self, db)

        self.auth = authcache.AuthenticationCache(self)
        self.jobStore = jobstore.JobStore(self)
        self.logStore = logstore.LogStore(contentsPath + '/logs')
        self.jobQueue = jobstore.JobQueue(self)
        self.subscriberStore = subscriber.SubscriberData(self)
        self.nodeStore = nodestore.NodeStore(self)

    def loadSchema(self, migrate=True):
        if migrate:
            return schema.SchemaManager(self.db).loadAndMigrate()
        else:
            return schema.SchemaManager(self.db).loadSchema()

    def open(self):
        return dbstore.connect(self.dbpath, driver=self.driver, timeout=120000,
                               lockJournal=True)

    def subscribeToJob(self, job):
        """ 
            Watches updates to this job object and will record them
            in the db.
        """
        _JobDbLogger(self).attach(job)

    def addJob(self, job):
        jobId = self.jobStore.addJob(job)
        cfg = job.getMainConfig()
        if cfg:
            for subscriber in cfg.subscribe.values():
                self.subscriberStore.add(jobId, subscriber)
        self.commit()
        return job

    def deleteJobs(self, jobIdList):
        troveInfoList = self.jobStore.deleteJobs(jobIdList)
        self.logStore.deleteLogs(troveInfoList)
        self.commit()
        return jobIdList

    def getJob(self, jobId, withTroves=True, withConfigs=True):
        try:
            return self.jobStore.getJob(jobId, withTroves=withTroves,
                                        withConfigs=withConfigs)
        except KeyError:
            raise errors.JobNotFound(jobId)

    def _getChrootIdForTrove(self, trove):
        return self.nodeStore.getOrCreateChrootId(trove)

    def getJobs(self, jobIds, withTroves=True, withConfigs=True):
        try:
            return self.jobStore.getJobs(jobIds, withTroves=withTroves,
                                         withConfigs=withConfigs)
        except KeyError, err:
            raise errors.JobNotFound(err.args[0])

    def getTrove(self, jobId, name, version, flavor, context=''):
        try:
            return self.jobStore.getTrove(jobId, name, version, flavor, context)
        except KeyError:
            raise errors.TroveNotFound(jobId, name, version, flavor, context)

    def getTroves(self, troveList):
        try:
            return self.jobStore.getTroves(troveList)
        except KeyError, err:
            raise errors.TroveNotFound(*err.args[0])

    def getConfig(self, jobId, context=''):
        try:
            return self.jobStore.getConfig(jobId, context)
        except KeyError, err:
            raise errors.JobNotFound(err.args[0])

    def convertToJobId(self, jobIdOrUUId):
        return self.convertToJobIds([jobIdOrUUId])[0]

    def convertToJobIds(self, items):
        """
            Converts a list of mixed jobIds and uuids to jobIds
            @param jobIdUUIDList: list of jobIds or uuids, or an
            @return list of jobIds
        """
        uuids = [ x for x in items if isinstance(x, str) and len(x) == 32]

        try:
            d = dict(itertools.izip(uuids,
                                    self.jobStore.getJobIdsFromUUIDs(uuids)))
        except KeyError, err:
            raise errors.JobNotFound(err.args[0])

        jobIds = []
        for jobIdUUId in items:
            if isinstance(jobIdUUId, int):
                jobIds.append(jobIdUUId)
            elif jobIdUUId in d:
                jobIds.append(d[jobIdUUId])
            else:
                try:
                    jobId = int(jobIdUUId)
                except ValueError:
                    raise errors.JobNotFound(jobIdUUId)
                jobIds.append(jobId)

        return jobIds

    def getJobsByState(self, state, withTroves=True):
        return self.jobStore.getJobsByState(state, withTroves=withTroves)

    def popJobFromQueue(self):
        try:
            jobId = self.jobQueue.pop()
        except IndexError:
            return None
        self.commit()
        return self.getJob(jobId)

    def listJobIdsOnQueue(self):
        return self.jobQueue.listJobIds()

    def queueJob(self, job):
        self.jobQueue.add(job)
        self.commit()

    def getJobConfig(self, jobId):
        return self.jobStore.getJobConfig(jobId)

    def getSubscriber(self, subscriberId):
        return self.subscriberStore.get(subscriberId)

    def getSubscribersForEvents(self, jobId, eventList):
        subscribers = self.subscriberStore.getMatches(jobId, eventList)
        return subscribers

    def listSubscribers(self, jobId):
        subscribers = self.subscriberStore.getByJobId(jobId)
        return subscribers


    def listSubscribersByUri(self, jobId, uri):
        subscribers = self.subscriberStore.getByUri(jobId, uri)
        return subscribers

    def addSubscriber(self, jobId, subscriber):
        self.subscriberStore.add(jobId, subscriber)
        self.db.commit()
        # subscriber object is modified to store subscriberId

    def removeSubscriber(self, subscriberId):
        self.subscriberStore.remove(subscriberId)
        self.db.commit()

    def listJobs(self, activeOnly=False, jobLimit=None):
        return self.jobStore.listJobs(activeOnly, jobLimit)

    def listTrovesByState(self, jobId, state=None):
        return self.jobStore.listTrovesByState(jobId, state)

    def jobExists(self, jobId):
        return self.jobStore.jobExists(jobId)

    def isJobBuilding(self):
        return self.jobStore.isJobBuilding()

    def hasTroveBuildLog(self, trove):
        if ((trove.logPath and os.path.exists(trove.logPath)) 
             or self.logStore.hasTroveLog(trove)):
            return True
        return False

    def openTroveBuildLog(self, trove):
        if trove.logPath:
            try:
                return open(trove.logPath, 'r')
            except (IOError, OSError), err:
                raise errors.RmakeError('Could not open log for %s=%s[%s] from %s: %s' % (trove.getNameVersionFlavor() + (trove.jobId, err)))
        else:
            if self.logStore.hasTroveLog(trove):
                return self.logStore.openTroveLog(trove)
            raise errors.RmakeError('Log for %s=%s[%s] from %s missing' % \
                                     (trove.getNameVersionFlavor() + 
                                      (trove.jobId,)))

    def updateJobStatus(self, job):
        self.jobStore.updateJobLog(job, job.status)
        self.jobStore.updateJob(job)
        self.commit()

    def updateJobLog(self, job, message):
        self.jobStore.updateJobLog(job, message)
        self.jobStore.updateJob(job)
        self.commit()

    def updateTroveLog(self, trove, message):
        self.jobStore.updateTroveLog(trove, message)
        self.jobStore.updateTrove(trove)
        self.commit()

    def updateTrove(self, trove):
        self.jobStore.updateTrove(trove)
        self.commit()

    def setBuildTroves(self, job):
        self.jobStore.setBuildTroves(job)
        self.commit()

    def trovePreparingChroot(self, trove):
        self.jobStore.updateTrove(trove)
        self.nodeStore.setChrootActive(trove, True)
        self.commit()

    def troveResolving(self, trove):
        self.jobStore.updateTrove(trove)
        self.commit()

    def troveBuilding(self, trove):
        self.jobStore.updateTrove(trove)
        self.nodeStore.setChrootActive(trove, True)
        self.commit()

    def troveBuilt(self, trove):
        self.jobStore.updateTrove(trove)
        self.jobStore.setBinaryTroves(trove, trove.getBinaryTroves())
        self.commit()

    def jobCommitted(self, job,  troveMap):
        for trove in job.iterTroves():
            committedTups = troveMap.get(trove.getNameVersionFlavor(withContext=True), None)
            if committedTups:
                binaries = [ x for x in committedTups
                             if not x[0].endswith(':source') ]
                if binaries:
                    self.jobStore.setBinaryTroves(trove, binaries)
        self.commit()

    def troveFailed(self, trove):
        self.jobStore.updateTrove(trove)
        self.commit()

    def updateTroveStatus(self, trove):
        self.jobStore.updateTrove(trove)
        if trove.isFinished():
            self.nodeStore.setChrootActive(trove, False)
        self.commit()

    # return all the log messages since last mark
    def getJobLogs(self, jobId, mark = 0):
        return self.jobStore.getJobLogs(jobId, mark=mark)

    def getTroveLogs(self, jobId, troveTuple, mark = 0):
        return self.jobStore.getTroveLogs(jobId, troveTuple, mark=mark)

    def getTroveBuildLog(self, jobId, troveTuple, mark):
        jobId = self.convertToJobId(jobId)
        trove = self.getTrove(jobId, *troveTuple)
        if not self.hasTroveBuildLog(trove):
            return not trove.isFinished(), '', 0
        f = self.openTroveBuildLog(trove)
        if mark < 0:
            f.seek(0, 2)
            end = f.tell()
            f.seek(max(end + mark, 0))
        else:
            f.seek(mark)
        return not trove.isFinished(), f.read(), f.tell()

    def addNode(self, name, host, slots, buildFlavors, chrootPaths):
        self.nodeStore.addNode(name, host, slots, buildFlavors)
        self.nodeStore.setChrootsForNode(name, chrootPaths)
        self.commit()

    def removeNode(self, name):
        self.nodeStore.removeNode(name)
        self.commit()

    def deactivateAllNodes(self):
        self.nodeStore.deactivateAllNodes()
        self.commit()

    def chrootIsActive(self, nodeName, path):
        try:
            return self.nodeStore.chrootIsActive(nodeName, path)
        except KeyError, err:
            raise errors.RmakeError('Chroot %s does not exist!' % err.args[1])

    def moveChroot(self, nodeName, path, newPath):
        self.nodeStore.moveChroot(nodeName, path, newPath)
        self.commit()

    def removeChroot(self, nodeName, path):
        self.nodeStore.removeChroot(nodeName, path)
        self.commit()

    def listChroots(self):
        return self.nodeStore.getAllChroots()

    def listNodes(self):
        return self.nodeStore.listNodes()

    def getEmptySlots(self):
        return self.nodeStore.getEmptySlots()
