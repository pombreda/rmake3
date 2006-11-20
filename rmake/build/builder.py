#
# Copyright (c) 2006 rPath, Inc.
#
# This program is distributed under the terms of the Common Public License,
# version 1.0. A copy of this license should have been distributed with this
# source file in a file called LICENSE. If it is not present, the license
# is always available at http://www.opensource.org/licenses/cpl.php.
#
# This program is distributed in the hope that it will be useful, but
# without any warranty; without even the implied warranty of merchantability
# or fitness for a particular purpose. See the Common Public License for
# full details.
#
"""
Builder controls the process of building a set of troves.
"""

import signal
import sys
import os
import time
import traceback

from conary import conaryclient
from conary.lib import log
from conary.repository import changeset

from rmake.build import buildjob
from rmake.build import failure
from rmake.build import rootfactory
from rmake.build import dephandler
from rmake.lib import logfile, recipeutil
from rmake.lib import repocache

class Builder(object):
    """
        Build manager for rMake.

        Basically:
            * get a set of troves in init.
            * load the troves to determine what packages they create,
              while flavors they use, and what build requirements they have.
            * while buildable troves left:
                * build one trove.
                * commit to internal repos if successful.

        Almost all passing of information from the builder is done through 
        subscription.  Instances register to listen to particular events on 
        the trove and job objects.  Those events are triggered by changing the
        states of the trove objects.

        Instances that listen on this side of the rMake server are called
        "Internal subscribers" - the database is one, the message passer that
        lets the rmake server know about status updates is another.

        See build/subscribe.py for more information.

        @param serverCfg: rmake server Configuration.  Used to determine 
        location to commit troves.
        @type serverCfg: rmake.server.servercfg.rMakeConfiguration
        @param buildCfg: build configuration, describes all parameters for 
        build.
        @type buildCfg: rmake.build.buildcfg.BuildConfiguration instance.
    """
    def __init__(self, serverCfg, buildCfg, job):
        self.serverCfg = serverCfg
        self.buildCfg = buildCfg
        self.logFile = logfile.LogFile(
                            serverCfg.getBuildLogPath(job.jobId))
        self.repos = self.getRepos()
        self.job = job
        self.jobId = job.jobId
        self._buildingTroves = []
        self._chroots = []

    def getRepos(self):
        repos = conaryclient.ConaryClient(self.buildCfg).getRepos()
        return repocache.CachingTroveSource(repos,
                                        self.serverCfg.getCacheDir())

    def info(self, state, message):
        log.info('[%s] [jobId %s] B: %s', time.strftime('%x %X'), self.jobId, message)

    def _signalHandler(self, sigNum, frame):
        pid = os.fork()
        try:
            if not pid:
                for chroot in self._chroots:
                    chroot.stop()
            os._exit(0)
        finally:
            os._exit(1)

    def buildAndExit(self):
        try:
            signal.signal(signal.SIGTERM, self._signalHandler)
            try:
                self.logFile.redirectOutput()
                self.build()
                os._exit(0)
            except Exception, err:
                self.job.exceptionOccurred(err, traceback.format_exc())
                print >>sys.stderr, traceback.format_exc()
                self.logFile.restoreOutput()
                print >>sys.stderr, traceback.format_exc()
                if sys.stdin.isatty():
                    # this sets us back to be connected with the controlling 
                    # terminal (owned by our parent, the rmake server)
                    import epdb
                    epdb.post_mortem(sys.exc_info()[2])
                os._exit(0)
        finally:
            os._exit(1)

    def initializeBuild(self):
        self.job.log('Build started - loading troves')

        buildTroves = recipeutil.getSourceTrovesFromJob(self.job,
                                                        self.buildCfg,
                                                        self.repos)
        self.job.setBuildTroves(buildTroves)

        self.buildState = dephandler.DependencyBasedBuildState(
                                                buildTroves,
                                                self.buildCfg)
        self.dh = dephandler.DependencyHandler(self.job.getStatusLogger(),
                                               self.buildCfg, self.repos,
                                               self.buildState)

        if not self._checkBuildSanity(buildTroves):
            return False

        self.job.log('Finding a buildable trove')
        self.dh.updateBuildableTroves()
        return True

    def build(self):
        # main loop is here.
        if not self.initializeBuild():
            return False

        if self.job.hasBuildableTroves():
            while True:

                if self.job.hasBuildingTroves():
                    if self._checkForResults():
                        self.dh.updateBuildableTroves()
                elif self.job.hasBuildableTroves():
                    self._buildTrove(self.job.iterBuildableTroves().next())
                else:
                    break
                time.sleep(1)

            if self.buildState.jobPassed():
                self.job.jobPassed("build job finished successfully")
                return True
            self.job.jobFailed("build job had failures")
        else:
            self.job.jobFailed('Did not find any buildable troves')
        return False

    def _checkBuildSanity(self, buildTroves):
        def _referencesOtherTroves(trv):
            return (trv.isGroupRecipe() or trv.isRedirectRecipe()
                    or trv.isFilesetRecipe())

        delayed = [ x for x in buildTroves if _referencesOtherTroves(x) ]
        if delayed and len(buildTroves) > 1:
            err = ('group, redirect, and fileset packages must'
                   ' be alone in their own job')
            for trove in delayed:
                # publish failed status
                trove.troveFailed(failure.FailureReason('Trove failed sanity check: %s' % err))
            troveNames = ', '.join(x.getName().split(':')[0] for x in delayed)
            self.job.jobFailed(failure.FailureReason("Job failed sanity check: %s: %s" % (err, troveNames)))
            return False
        return True

    def _buildTrove(self, troveToBuild):
        chrootFactory = self.getChrootFactory()
        self.job.log('Building %s' % troveToBuild.getName())
        buildReqs = self.buildState.getBuildReqTroves(troveToBuild)

        try:
            chroot = chrootFactory.createRoot(buildReqs, troveToBuild)
            self._chroots.append(chroot)
        except Exception, err:
            f = failure.ChrootFailed(str(err), traceback.format_exc())
            # sends off messages to all listeners that this trove failed.
            troveToBuild.troveFailed(f)
            return

        n,v,f = troveToBuild.getNameVersionFlavor()
        targetLabel = self.buildCfg.getTargetLabel(v)
        logPath, pid = chroot.buildTrove(self.buildCfg, targetLabel, n, v, f)
        # sends off message that this trove is building.
        troveToBuild.troveBuilding(logPath, pid)
        self._buildingTroves.append((chrootFactory, chroot, troveToBuild))


    def _checkForResults(self):
        foundResult = False
        for chrootFactory, chroot, trove in list(self._buildingTroves):
            try:
                buildResult = chroot.checkResults(*trove.getNameVersionFlavor())
                if not buildResult:
                    continue
                foundResult = True
                self._buildingTroves.remove((chrootFactory, chroot, trove))

                if buildResult.isBuildSuccess():
                    csFile = buildResult.getChangeSetFile()
                    cs = changeset.ChangeSetFromFile(csFile)
                    self.repos.commitChangeSet(cs)
                    # sends off message that this trove built successfully
                    trove.troveBuilt(cs)
                    del cs # this makes sure the changeset closes the fd.
                    if self.buildCfg.cleanAfterCook:
                        chrootFactory.cleanRoot(chroot.getPid())
                    else:
                        chrootFactory.killRoot(chroot.getPid())
                    continue
                else:
                    reason = buildResult.getFailureReason()
                    trove.troveFailed(reason)
                    # passes through to killRoot at the bottom.
            except Exception, e:
                reason = failure.InternalError(str(e), traceback.format_exc())
                trove.troveFailed(reason)
            chrootFactory.killRoot(chroot.getPid())
        return foundResult

    def getChrootFactory(self):
        return rootfactory.ChrootFactory(self.job, self.serverCfg.buildDir,
                                         self.serverCfg.chrootHelperPath,
                                         self.buildCfg, self.serverCfg)
