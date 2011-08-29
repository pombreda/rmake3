#
# Copyright (c) 2011 rPath, Inc.
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
#


import errno
import itertools
import os
import resource
import signal
import tempfile
import time
import traceback

from conary.build import cook,macros,use
from conary.deps import deps
from conary.lib import log,util
from conary.local import database
from conary import versions
from conary.deps.deps import ThawFlavor

from rmake import compat
from rmake.failure import BuildFailed
from rmake.lib import flavorutil
from rmake.lib import logfile
from rmake.lib import pipereader
from rmake.lib import recipeutil
from rmake.worker import resolvesource


class CookResults(object):
    def __init__(self, name, version, flavorList):
        self.name = name
        self.version = version
        if not isinstance(flavorList, (list, tuple)):
            flavorList = [flavorList]
        self.flavorList = flavorList
        self.status = ''
        self.csFile = ''
        self.pid = 0
        self.failureReason = None
        self.signal = ''

    def exitedNormally(self):
        return not self.signal

    def setExitStatus(self, status):
        self.status = status

    def setExitSignal(self, signal):
        self.signal = signal

    def getExitSignal(self):
        return self.signal

    def getExitStatus(self):
        return self.status

    def setChangeSetFile(self, csFile):
        self.csFile = csFile

    def getChangeSetFile(self):
        return self.csFile

    def setFailureReason(self, reason):
        self.failureReason = reason

    def getFailureReason(self):
        return self.failureReason

    def isBuildSuccess(self):
        return self.exitedNormally() and not self.status

    def __freeze__(self):
        d = self.__dict__.copy()
        d['pid'] = self.pid
        d['version'] = str(self.version)
        d['flavorList'] = [ x.freeze() for x in self.flavorList ]
        d['failureReason'] = freeze('FailureReason', self.failureReason)
        return d

    @staticmethod
    def __thaw__(d):
        d = d.copy()
        new = CookResults(d.pop('name'),
                          versions.VersionFromString(d.pop('version')),
                          [ ThawFlavor(x) for x in d.pop('flavorList')])
        new.__dict__.update(d)
        new.failureReason = thaw('FailureReason', new.failureReason)
        return new


def cookTrove(cfg, repos, logger, name, version, flavorList, targetLabel,
              loadSpecsList=None, builtTroves=None, logData=None,
              buildReqs=None, crossReqs=None):
    if not isinstance(flavorList, (tuple, list)):
        flavorList = [flavorList]
    util.mkdirChain(cfg.root + '/tmp')
    fd, csFile = tempfile.mkstemp(dir=cfg.root + '/tmp',
                                  prefix='rmake-%s-' % name,
                                  suffix='.ccs')
    os.chmod(csFile, 0644)
    os.close(fd)
    logPath = cfg.root + '/tmp/rmake/%s-%s.log' % (name,
                                    version.trailingRevision())
    logFile = logfile.LogFile(logPath)
    os.chmod(logPath, 0664)
    os.chmod(cfg.root + '/tmp/rmake', 0775)

    results = CookResults(name, version, flavorList)

    # ignore child output problems
    signal.signal(signal.SIGTTOU, signal.SIG_IGN)

    inF, outF = pipereader.makePipes()
    pid = os.fork()
    if not pid:
        try:
            try:
                signal.signal(signal.SIGTERM, signal.SIG_DFL)
                os.close(inF)
                os.setpgrp()
                # don't accidentally make world writable files
                os.umask(0022)
                # don't allow us to create core dumps
                resource.setrlimit(resource.RLIMIT_CORE, (0,0))
                log.setVerbosity(log.DEBUG)
                log.info("Cook process started (pid %s)" % os.getpid())
                _cookTrove(cfg, repos, name, version, flavorList, targetLabel,
                           loadSpecsList, builtTroves,
                           csFile, buildReqs=buildReqs, crossReqs=crossReqs,
                           failureFd=outF, logger=logger)
            except Exception, msg:
                if len(flavorList) > 1:
                    errMsg = 'Error cooking %s=%s with flavors %s: %s' % \
                        (name, version, ', '.join([str(x) for x in flavorList]),
                         str(msg))
                else:
                    errMsg = str(msg)
                _buildFailed(outF, errMsg, traceback.format_exc())
                logFile.close()
                os._exit(1)
            else:
                logFile.close()
                os._exit(0)
        finally:
            logFile.close()
            # some kind of error occurred if we get here.
            os._exit(1)
    else:
        os.close(outF)
        return logPath, pid, (results, pid, inF, csFile)


def getResults(results, pid, inF, csFile):
    (gotResult, status) = os.waitpid(pid, os.WNOHANG)
    if not gotResult:
        return None

    if os.WIFSIGNALED(status):
        results.setExitSignal(os.WTERMSIG(status))
    else:
        assert(os.WIFEXITED(status))
        results.setExitStatus(os.WEXITSTATUS(status))

    if results.isBuildSuccess():
        results.setChangeSetFile(csFile)
    elif results.getExitSignal():
        results.setFailureReason(BuildFailed('Build exited with signal %s' % results.getExitSignal()))
    else:
        errReason = []
        buffer = os.read(inF, 1024)
        while buffer:
            errReason.append(buffer)
            buffer = os.read(inF, 1024)
        errReason = ''.join(errReason)
        errTag, data = errReason.split('\002', 1)
        results.setFailureReason(thaw('FailureReason', (errTag, data)))
    os.close(inF)
    return results

def stopBuild(results, pid, inF, csFile):
    log.info('killing %s' % pid)
    try:
        os.kill(-pid, signal.SIGTERM)
    except OSError, err:
        if err.errno != errno.ESRCH:
            raise
        # Process is either dead or already reaped. Do waitpid() to make sure
        # it gets reaped properly.

    timeSlept = 0
    while timeSlept < 10:
        try:
            gotResult, status = os.waitpid(pid, os.WNOHANG)
        except OSError, err:
            if err.errno != errno.ECHILD:
                raise
            # Process died and someone else reaped it.
            gotResult = True
            break
        if gotResult:
            break
        else:
            time.sleep(.5)
            timeSlept += .5
    os.close(inF)

    if not gotResult:
        log.warning('pid %s did not respond to kill, trying SIGKILL' % pid)
        try:
            os.kill(-pid, signal.SIGKILL)
        except OSError, err:
            if err.errno != errno.ESRCH:
                raise
            else:
                return

        # just hang waiting
        gotResult, status = os.waitpid(pid, 0)
    log.info('pid %s killed' % pid)

def _buildFailed(failureFd, errMsg, traceBack=''):
    log.error(errMsg)
    frz = '\002'.join(str(x) for x in freeze('FailureReason',
                                BuildFailed(errMsg, traceBack)))
    if failureFd is not None:
        os.write(failureFd, frz)
        os.close(failureFd)
    os._exit(1)

def _cookTrove(cfg, repos, name, version, flavorList, targetLabel,
               loadSpecsList, builtTroves, csFile, buildReqs, crossReqs,
               failureFd, logger):
    baseFlavor = cfg.buildFlavor
    db = database.Database(cfg.root, cfg.dbPath)
    buildLabel = version.trailingLabel()
    buildBranch = version.branch()
    binaryBranch = version.getBinaryVersion().branch()
    if targetLabel:
        source = recipeutil.RemoveHostSource(db, targetLabel.getHost())
        if version.trailingLabel() == targetLabel and version.depth() > 1:
            buildBranch = version.branch().parentBranch()
            buildLabel = buildBranch.label()
            revision = versions.Revision('1-1')
            binaryBranch = buildBranch.createVersion(revision)\
                                            .getBinaryVersion().branch()
    else:
        source = db
    loaders = []
    recipeClasses = []

    if not isinstance(flavorList, (tuple, list)):
        flavorList = [flavorList]
    if not isinstance(loadSpecsList, (tuple, list)):
        loadSpecsList = [loadSpecsList] * len(flavorList)

    for flavor, loadSpecs in itertools.izip(flavorList, loadSpecsList):
        try:
            logger.debug('Cooking %s=%s[%s] to %s (stored in %s)' % \
                         (name, version, flavor, targetLabel, csFile))
            cfg.buildFlavor = deps.overrideFlavor(baseFlavor, flavor)
            cfg.initializeFlavors()
            (loader, recipeClass, localFlags, usedFlags)  = \
                recipeutil.loadRecipeClass(repos, name, version,
                                           cfg.buildFlavor,
                                           ignoreInstalled=False, root=cfg.root,
                                           loadInstalledSource=source,
                                           overrides=loadSpecs,
                                           cfg=cfg)
            loaders.append(loader)
            recipeClasses.append(recipeClass)
            recipeClass.buildRequirementsOverride = buildReqs
            recipeClass.crossRequirementsOverride = crossReqs
        except Exception, msg:
            errMsg = 'Error loading recipe %s=%s[%s]: %s' % \
                                            (name, version, flavor, str(msg))
            _buildFailed(failureFd, errMsg, traceback.format_exc())

    try:
        # get the correct environment variables from this root
        # some packages depend on environment variables e.g. $QTDIR that 
        # are set by other packages.  
        setupEnvironment()

        # now override flags set in flavor
        # don't need to reset this flavor ever, because
        # we are in a fork
        flavorutil.setLocalFlags(localFlags)
        packageName = name.split(':')[0]
        # this shouldn't matter for group recipes as it will get overridden
        # by the behavior in cookGroupObject.  But it matters for some other
        # recipe types.  That should be fixed and all that code should be
        # moved inside cookObject so I could get rid of this.
        use.setBuildFlagsFromFlavor(packageName, cfg.buildFlavor, error=False)
        use.resetUsed()
        use.setUsed(usedFlags)


        # we don't want to sign packages here, if necessary, we can sign
        # them at a higher level.
        cfg.signatureKeyMap = {}
        cfg.signatureKey = None
        crossCompile = flavorutil.getCrossCompile(cfg.buildFlavor)

        # add extra buildreqs manually added for this trove
        # by the builder.  Only add them if the recipe is of the
        # right type, and the cfg file we're passed in understands them
        # (it might be a simple conary cfg file).
        if (hasattr(recipeClasses[0], 'buildRequires')
            and hasattr(cfg, 'defaultBuildReqs')):
            for recipeClass in recipeClasses:
                recipeClass.buildRequires += cfg.defaultBuildReqs

        if builtTroves:
            # FIXME: is this cached?
            builtTroves = repos.getTroves(builtTroves, withFiles=False)

            builtTroves = resolvesource.BuiltTroveSource(builtTroves, repos)
            builtTroves.searchAsRepository()
            if targetLabel:
                builtTroves = recipeutil.RemoveHostSource(builtTroves,
                                                          targetLabel.getHost())
            else:
                builtTroves = recipeutil.RemoveHostSource(builtTroves,
                                              version.trailingLabel().getHost())

            # this should only make a difference when cooking groups, redirects,
            # etc.
            oldRepos = repos
            repos = resolvesource.DepHandlerSource(builtTroves,
                                                   None,
                                                   repos)
            repos.TROVE_QUERY_ALL = oldRepos.TROVE_QUERY_ALL

        # if we're already on the target label, we'll assume no targeting 
        # is necessary
        if targetLabel == version.trailingLabel():
            targetLabel = None

    except Exception, msg:
        errMsg = 'Error initializing cook environment %s=%s[%s]: %s' % \
                                            (name, version, flavor, str(msg))
        _buildFailed(failureFd, errMsg, traceback.format_exc())

    try:
        os.chdir('/tmp') # make sure we're in a directory
                         # that we can write to.  Although
                         # this _shouldn't_ be an issue,
                         # conary 1.0.{19,20} require it.
        # finally actually cook the recipe!
        groupOptions = cook.GroupCookOptions(alwaysBumpCount=False,
                                        shortenFlavors=cfg.shortenGroupFlavors,
                                        errorOnFlavorChange=False)
        m = macros.Macros()
        m._override('buildlabel', str(buildLabel))
        m._override('buildbranch', str(buildBranch))
        m._override('binarybranch', str(binaryBranch))
        toCook = compat.ConaryVersion().getObjectsToCook(loaders, recipeClasses)
        built = cook.cookObject(repos, cfg, toCook, version,
                                prep=False,
                                macros=m,
                                targetLabel=targetLabel,
                                changeSetFile=csFile,
                                alwaysBumpCount=False,
                                ignoreDeps=False,
                                logBuild=True,
                                crossCompile=crossCompile,
                                requireCleanSources=True,
                                groupOptions=groupOptions)
        if not built:
            if log.errorOccurred():
                msg = 'Check logs'
            else:
                msg = 'Unknown failure'
            errMsg = 'Error building recipe %s=%s[%s]: %s' % (name, version,
                                                              flavor, msg)
            _buildFailed(failureFd, errMsg)
    except Exception, msg:
        errMsg = 'Error building recipe %s=%s[%s]: %s' % (name, version,
                                                          flavor, str(msg))
        _buildFailed(failureFd, errMsg, traceback.format_exc())


def setupEnvironment():
    """
    Grab a fresh copy of the environment, based on the currently installed
    troves.
    """
    skipenv = set(['DISPLAY', 'STY', 'COVERAGE_DIR'])
    for key in os.environ.keys():
        if key not in skipenv:
            del os.environ[key]
    for line in os.popen('/bin/bash -l -c env'):
        key, val = line.split('=', 1)
        if key not in skipenv:
            os.environ[key] = val[:-1]
    os.environ['LANG'] = 'C'
    os.environ['HOME'] = '/tmp/rmake'
