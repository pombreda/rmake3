#
# Copyright (c) 2006-2007 rPath, Inc.  All Rights Reserved.
#
"""
    Creates chroots to be used for building.

    Uses the chroothelper program to do final processing and chrooting.
"""

import grp
import os
import pwd
import shutil
import sys
import stat

#conary
from conary import conarycfg
from conary import conaryclient
from conary import callbacks
from conary.deps import deps
from conary.lib import util, log, openpgpkey, sha1helper

#rmake
from rmake import errors
from rmake import compat
from rmake import constants
from rmake.lib import flavorutil
from rmake.lib import rootfactory

def _addModeBits(path, bits):
    s = os.lstat(path)
    if not stat.S_ISLNK(s.st_mode) and not (s.st_mode & bits == bits):
        os.chmod(path, stat.S_IMODE(s.st_mode) | bits)

class ConaryBasedChroot(rootfactory.BasicChroot):
    """ 
        The root manages a root environment, creating and installing
        the necessary files for the root to be usuable, and cleaning up
        after itself as much as possible.
    """
    def __init__(self, jobList, crossJobList, logger, cfg, csCache=None,
                 chrootCache=None, targetFlavor=None, oldRoot=None):
        rootfactory.BasicChroot.__init__(self)
        self.cfg = cfg
        self.jobList = jobList
        self.crossJobList = crossJobList
        self.callback = None
        self.logger = logger
        self.csCache = csCache
        self.chrootCache = chrootCache
        self.oldRoot = oldRoot
        if targetFlavor is not None:
            cfg.initializeFlavors()
            self.sysroot = flavorutil.getSysRootPath(targetFlavor)

        self.addDir('/tmp', mode=01777)
        self.addDir('/var/tmp', mode=01777)
        self.addDir('/etc')
        self.addDir('/etc/rmake')
        self.addDir('/etc/conary')

        self.addDir(self.cfg.tmpDir, mode=01777)
        if self.crossJobList:
            self.addDir('%s/lib' % self.sysroot)
            self.addDir('%s/usr/lib' % self.sysroot)

    def moveOldRoot(self, oldRoot, newRoot):
        self.logger.info('Moving root from %s to %s for reuse' % (oldRoot,
                                                                  newRoot))
        if os.path.exists(newRoot):
            self.logger.warning('Root already exists at %s - cannot move old root to that spot')
            return False

        try:
            os.rename(oldRoot, newRoot)
        except OSError, err:
            self.logger.warning('Could not rename old root %s to %s: %s' % (oldRoot, newRoot, err))
            return False

        self.cfg.root = newRoot
        client = conaryclient.ConaryClient(self.cfg)
        try:
            assert(client.db.db.schemaVersion)
        except Exception, err:
            self.logger.warning('Could not access database in old root %s: %s.  Removing old root' % (oldRoot, err))
            os.rename(newRoot, oldRoot)
            return False
        return True

    def create(self, root):
        self.cfg.root = root
        rootfactory.BasicChroot.create(self, root)

    def install(self):
        self.cfg.root = self.root
        if self.oldRoot:
            if self.serverCfg.reuseChroots:
                self._moveOldRoot(self.oldRoot, self.root)
        if not self.jobList and not self.crossJobList:
            # should only be true in debugging situations
            return

        client = conaryclient.ConaryClient(self.cfg)
        repos = client.getRepos()
        if self.chrootCache and hasattr(repos, 'getChangeSetFingerprints'):
            fingerprints = client.repos.getChangeSetFingerprints(
                sorted(self.jobList + self.crossJobList),
                recurse=False, withFiles=True, withFileContents=True,
                excludeAutoSource=True, mirrorMode=False)
            chrootFingerprint = sha1helper.sha1String(''.join(fingerprints))
            if self.chrootCache.hasChroot(chrootFingerprint):
                strFingerprint = sha1helper.sha1ToString(chrootFingerprint)
                self.logger.info('restoring cached chroot with '
                        'fingerprint %s', strFingerprint)
                self.chrootCache.restore(chrootFingerprint, self.cfg.root)
                self.logger.info('chroot fingerprint %s '
                         'restore done', strFingerprint)
                return

        def _install(jobList):
            self.cfg.flavor = []
            openpgpkey.getKeyCache().setPublicPath(
                                     self.cfg.root + '/root/.gnupg/pubring.gpg')
            openpgpkey.getKeyCache().setPrivatePath(
                                self.cfg.root + '/root/.gnupg/secring.gpg')
            self.cfg.pubRing = [self.cfg.root + '/root/.gnupg/pubring.gpg']
            client = conaryclient.ConaryClient(self.cfg)
            client.setUpdateCallback(self.callback)
            if self.csCache:
                changeSetList = self.csCache.getChangeSets(client.getRepos(),
                                                           jobList,
                                                           callback=self.callback)
            else:
                changeSetList = []

            try:
                updJob, suggMap = client.updateChangeSet(
                    jobList, keepExisting=False, resolveDeps=False,
                    recurse=False, checkPathConflicts=False,
                    fromChangesets=changeSetList,
                    migrate=True)
            except conaryclient.update.NoNewTrovesError:
                # since we're migrating, this simply means there were no
                # operations to be performed
                pass
            else:
                util.mkdirChain(self.cfg.root + '/root')
                client.applyUpdate(updJob, replaceFiles=True,
                                   tagScript=self.cfg.root + '/root/tagscripts')

        if self.jobList:
            _install(self.jobList)

        if self.crossJobList:
            oldRoot = self.cfg.root
            try:
                self.cfg.root += self.sysroot
                _install(self.crossJobList)
            finally:
                self.cfg.root = oldRoot

        # directories must be traversable and files readable (RMK-1006)
        for root, dirs, files in os.walk(self.cfg.root, topdown=True):
            for directory in dirs:
                _addModeBits(os.sep.join((root, directory)), 05)
            for filename in files:
                _addModeBits(os.sep.join((root, filename)), 04)

        if self.chrootCache:
            strFingerprint = sha1helper.sha1ToString(chrootFingerprint)
            self.logger.info('caching chroot with fingerprint %s',
                    strFingerprint)
            self.chrootCache.store(chrootFingerprint, self.cfg.root)
            self.logger.info('caching chroot %s done',
                    strFingerprint)

    def _copyInConary(self):
        conaryDir = os.path.dirname(sys.modules['conary'].__file__)
        self.copyDir(conaryDir)
        #self.copyDir(conaryDir,
        #             '/usr/lib/python2.4/site-packages/conary')
        #self.copyDir(conaryDir,
        #             '/usr/lib64/python2.4/site-packages/conary')
        self.copyDir(conaryDir,
                     '/usr/share/rmake/conary')
        if conaryDir.endswith('site-packages/conary'):
            self.copyFile('/usr/bin/conary')
            self.copyFile('/usr/bin/cvc')
        elif os.path.exists(os.path.join(conaryDir, '../commands')):
            commandDir = os.path.realpath(os.path.join(conaryDir,'../commands'))
            for fname in ['cvc', 'conary']:
                self.copyFile(os.path.join(commandDir, fname),
                              os.path.join('/usr/bin', fname))
            # Need to copy perlreqs.pl too
            scriptsDir = os.path.realpath(os.path.join(conaryDir,'../scripts'))
            if os.path.exists(scriptsDir):
                self.copyDir(scriptsDir)
                self.copyFile(os.path.join(scriptsDir, 'perlreqs.pl'),
                    '/usr/libexec/conary/perlreqs.pl')

class rMakeChroot(ConaryBasedChroot):

    def __init__(self, buildTrove, chrootHelperPath, cfg, serverCfg,
                 jobList, crossJobList, logger, uid=None, gid=None, 
                 csCache=None, chrootCache=None, copyInConary=True,
                 oldRoot=None):
        """ 
            uid/gid:  the uid/gid which special files in the chroot should be 
                      owned by
        """
        ConaryBasedChroot.__init__(self, jobList, crossJobList, logger, cfg,
                                   csCache, chrootCache,
                                   buildTrove.getFlavor(), oldRoot=None)
        self.jobId = buildTrove.jobId
        self.buildTrove = buildTrove
        self.chrootHelperPath = chrootHelperPath
        self.serverCfg = serverCfg
        self.callback = ChrootCallback(self.buildTrove, logger,
                                       caching=bool(csCache))
        self.copyInConary = copyInConary

        if copyInConary:
            self._copyInConary()
            for dir in self.cfg.policyDirs:
                if os.path.exists(dir):
                    self.copyDir(dir)
        self._copyInRmake()

    def getRoot(self):
        return self.cfg.root

    def checkSanity(self):
        if self.copyInConary:
            # we're just overriding the version of conary used
            # as long as that't the only sanity check we can return 
            # immediately
            return
        for job in self.jobList:
            if job[0] == 'conary:python':
                version = job[2][0].trailingRevision().getVersion()
                try:
                    compat.ConaryVersion(version).checkRequiredVersion()
                except errors.RmakeError, error:
                    errorMsg = str(error) + (' - tried to install version %s in chroot' % version)
                    raise error.__class__(errorMsg)

    def useStandardRoot(self):
        return True

    def install(self):
        self.logger.info('Creating chroot')
        ConaryBasedChroot.install(self)
        # copy in the tarball files needed for building this package from
        # the cache.
        self._cacheBuildFiles()

    def _cacheBuildFiles(self):
        if not self.csCache:
            return
        client = conaryclient.ConaryClient(self.cfg)
        sourceTup = self.buildTrove.getNameVersionFlavor()
        sourceTup = (sourceTup[0], sourceTup[1], deps.parseFlavor(''))
        trv = self.csCache.getTroves(client.getRepos(), [sourceTup],
                                     withFiles=True)[0]
        allFiles = list(trv.iterFileList())
        fileContents = [(x[2], x[3]) for x in allFiles]
        oldRootLen = len(self.csCache.root)
        if fileContents:
            self.logger.info('Caching %s files' % len(fileContents))
            for path in self.csCache.getFileContentsPaths(client.getRepos(),
                                                          fileContents):
                newPath = path[oldRootLen:]
                self.copyFile(path, '/tmp/cscache/' + newPath,
                              mode=0755)


    def _copyInRmake(self):
        # should this be controlled by strict mode too?
        rmakeDir = os.path.dirname(sys.modules['rmake'].__file__)
        # don't copy in rmake into /usr/lib/python2.4/site-packages
        # as its important that we don't muck with the standard file 
        # system location for some test runs of rmake inside of rmake
        #self.copyDir(rmakeDir)
        # just copy to a standard path
        self.copyDir(rmakeDir, '/usr/share/rmake/rmake')

    def _postInstall(self):
        self.createConaryRc()
        self.createRmakeUsers()

    def createConaryRc(self):
        conaryrc = None
        try:
            if self.canChroot(): # then we will be chrooting into this dir
                conaryrc = open('%s/etc/conaryrc.prechroot' % self.cfg.root, 'w')
                oldroot = self.cfg.root
                self.cfg.root = '/'
                try:
                    self.cfg.storeConaryCfg(conaryrc)
                finally:
                    self.cfg.root = oldroot
            else:
                conaryrc = open('%s/etc/conaryrc.rmake' % self.cfg.root, 'w')
                self.cfg.storeConaryCfg(conaryrc)
        except Exception, msg:
            self.logger.error("Error writing conaryrc: %s", msg)
        conaryrc.close()

    def createRmakeUsers(self):
        """Copy passwd/group entries for rmake and rmake-chroot into the chroot.
        """
        passwd = open(os.path.join(self.cfg.root, 'etc/passwd'), 'a')
        group = open(os.path.join(self.cfg.root, 'etc/group'), 'a')
        for name in (constants.rmakeUser, constants.chrootUser):
            pwdata = pwd.getpwnam(name)
            print >> passwd, ":".join(str(x) for x in pwdata)
            grpdata = grp.getgrgid(pwdata.pw_gid)
            print >> group, ":".join(str(x) for x in grpdata)

    def canChroot(self):
        return (pwd.getpwnam(constants.rmakeUser).pw_uid == os.getuid())


    def unmount(self, root, raiseError=True):
        if not os.path.exists(root):
            return True
        if self.canChroot():
            self.logger.info('Running chroot helper to unmount...')
            util.mkdirChain(root + '/sbin')
            rc = os.system('%s --unmount %s' % (self.chrootHelperPath, root))
            if rc:
                if raiseError:
                    raise errors.ServerError('Could not unmount old chroot')
                return False
        return True


    def clean(self, root, raiseError=True):
        if self.canChroot():
            self.logger.info('Running chroot helper to clean/unmount...')
            util.mkdirChain(root + '/sbin')
            shutil.copy('/sbin/busybox', root + '/sbin/busybox')
            rc = os.system('%s %s --clean' % (self.chrootHelperPath, root))
            if rc:
                if raiseError:
                    raise errors.ServerError(
                            'Cannot create chroot - chroot helper failed'
                            ' to clean old chroot')
                else:
                    return False
        self.logger.debug("removing old chroot tree: %s", root)
        # First, remove the conary database
        try:
            os.unlink(util.joinPaths(root, '/var/lib/conarydb/conarydb'))
        except OSError:
            pass
        # attempt to remove just the /tmp dir first.
        # that's where the chroot process should have had all
        # of its files.  Doing this makes sure we don't remove
        # /bin/rm while it might still be needed the next time around.
        os.system('rm -rf %s/tmp' % root)
        removeFailed = False
        if os.path.exists(root + '/tmp'):
            removeFailed = True
        else:
            os.system('rm -rf %s' % root)
            if os.path.exists(root):
                removeFailed = True
        if removeFailed and raiseError:
            raise errors.ServerError(
                'Cannot create chroot - old root at %s could not be removed.'
                '  This may happen due to permissions problems such as root'
                ' owned files, or earlier build processes that have not'
                ' completely died.  Please shut down rmake, kill any remaining'
                ' rmake processes, and then retry.  If that does not work,'
                ' please remove the old root by hand.' % root)
        return not removeFailed


class ExistingChroot(rMakeChroot):
    def __init__(self, rootPath, logger, chrootHelperPath):
        self.root = rootPath
        self.logger = logger
        self.chrootHelperPath = chrootHelperPath
        rootfactory.BasicChroot.__init__(self)
        self._copyInRmake()

    def create(self, root):
        rootfactory.BasicChroot.create(self, root)

    def install(self):
        pass

    def getRoot(self):
        return self.root

    def _postInstall(self):
        pass

    def checkSanity(self):
        pass

class FullRmakeChroot(rMakeChroot):
    """
        This chroot contains everything needed to start the rMake chroot.
    """

    def __init__(self, *args, **kw):
        rMakeChroot.__init__(self, *args, **kw)
        self.addMount('/proc', '/proc', type='proc')
        self.addMount('/dev/pts', '/dev/pts', type='devpts')
        self.addDeviceNode('urandom') # needed for ssl and signing
        self.addDeviceNode('ptmx') # needed for pty use

        self.copyFile('/etc/hosts')
        self.copyFile('/etc/resolv.conf')

        # make time outputs accurate
        if os.path.exists('/etc/localtime'):
            self.copyFile('/etc/localtime')
        # glibc:runtime should provide a good default nsswitch
        if os.path.exists('/etc/nsswitch.conf'):
            self.copyFile('/etc/nsswitch.conf')

        if self.cfg.copyInConfig:
            for option in ['archDirs', 'mirrorDirs',
                           'siteConfigPath', 'useDirs', 'componentDirs']:
                for dir in self.cfg[option]:
                    if os.path.exists(dir):
                        self.copyDir(dir)
            for option in ['defaultMacros']:
                for path in self.cfg[option]:
                    if os.path.exists(path):
                        self.copyFile(path)

class ChrootCallback(callbacks.UpdateCallback):
    """
        Callback to update trove log as the chroot is created.
        @param buildTrove: trove we're creating a chroot for
        @type: build.buildtrove.BuildTrove
    """
    def __init__(self, buildTrove, logger, caching=True):
        callbacks.UpdateCallback.__init__(self)
        self.hunk = (0,0)
        self.buildTrove = buildTrove
        self.logger = logger
        self.showedHunk = False
        self.caching = caching

    def _message(self, text):
        self.buildTrove.log(text)

    def setChangesetHunk(self, num, total):
        self.showedHunk = False
        self.hunk = (num, total)

    def setUpdateHunk(self, num, total):
        self.hunk = (num, total)

    def setUpdateJob(self, jobs):
        descriptions = []
        jobs.sort()
        for job in jobs:
            if job[2][0]:
                n,v,f = job[0], job[2][0], job[2][1]
            else:
                n,v,f = job[0], job[1][0], job[1][1]
            
            v = '%s/%s' % (v.trailingLabel(), v.trailingRevision())
            archDeps = [x.name for x in f.iterDepsByClass(deps.InstructionSetDependency)]
            if archDeps:
                f = '[is: %s]' % ' '.join(archDeps)
            else:
                f = ''
            if job[2][0]:
                action = ''
            else:
                action = 'Erase '
            descriptions.append('%s%s=%s%s' % (action, n,v,f))
        if self.hunk[1] > 1:
            self._message("installing %d of %d:\n    %s" % \
                            (self.hunk[0], self.hunk[1],
                             '\n    '.join(descriptions)))
        else:
            self._message("installing: \n    %s" % \
                          ('\n    '.join(descriptions),))

    def downloadingChangeSet(self, got, need):
        if self.caching and not self.showedHunk:
            # we display our message here because here we have the size...
            # but we only want to display the message once per changeset
            self._message("Caching changeset %s of %s (%sKb)" % (
                                            self.hunk + (need/1024 or 1,)))
            self.showedHunk = True

