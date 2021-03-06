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


import errno
import grp
import logging
import os
import pwd
import signal
import sys
import time
from conary.conarycfg import ConfigFile, CfgBool
from conary.lib import options
from twisted.internet import error as twerror

from rmake.lib import pluginlib
from rmake.lib import logger as rmake_log
from rmake.lib.twisted_extras import deferred_service

log = logging.getLogger(__name__)


(NO_PARAM,  ONE_PARAM)  = (options.NO_PARAM, options.ONE_PARAM)
(OPT_PARAM, MULT_PARAM) = (options.OPT_PARAM, options.MULT_PARAM)


class DaemonConfig(ConfigFile):
    logDir         = '/var/log/'
    lockDir        = '/var/run/'
    verbose        = (CfgBool, False)


_commands = []


class DaemonCommand(options.AbstractCommand):
    docs = {'config'             : ("Set config KEY to VALUE", "'KEY VALUE'"),
            'config-file'        : ("Read PATH config file", "PATH"),
            'verbose'            : ("Increase verobsity in output"),
            'debug-all'          : "Debug exceptions"}

    def addParameters(self, argDef):
        d = {}
        d["config"] = MULT_PARAM
        d["config-file"] = '-c', MULT_PARAM
        d["debug-all"] = '-d', NO_PARAM
        d["skip-default-config"] = NO_PARAM

        argDef[self.defaultGroup] = d

    def addConfigOptions(self, cfgMap, argDef):
        cfgMap['verbose'] = 'verbose', NO_PARAM, '-v'
        options.AbstractCommand.addConfigOptions(self, cfgMap, argDef)

    def processConfigOptions(self, cfg, cfgMap, argSet):
        for file in argSet.pop('config-file', []):
            cfg.read(file)
        options.AbstractCommand.processConfigOptions(self, cfg, cfgMap, argSet)


class ConfigCommand(DaemonCommand):
    commands = ['config']

    help = 'Display configuration for this service'

    def runCommand(self, daemon, cfg, argSet, args):
        return cfg.display()
_commands.append(ConfigCommand)


class StopCommand(DaemonCommand):
    commands = ['stop', 'kill']

    help = 'Stop the service'

    def runCommand(self, daemon, cfg, argSet, args):
        return daemon.kill()
_commands.append(StopCommand)


class StartCommand(DaemonCommand):
    commands = ['start']

    help = 'Start the service'

    docs = {'no-daemon': "Do not run as a daemon"}

    def addParameters(self, argDef):
        DaemonCommand.addParameters(self, argDef)
        argDef["no-daemon"] = '-n', NO_PARAM

    def runCommand(self, daemon, cfg, argSet, args):
        return daemon.start(argSet)
_commands.append(StartCommand)


class Daemon(options.MainHandler):
    '''This class contains basic daemon functions, useful for creating your own
       daemon.
    '''
    abstractCommand = DaemonCommand
    name = 'daemon'
    commandList = _commands
    user   = None
    groups = None
    capabilities = None
    useConaryOptions = False

    def __init__(self):
        self._logFile = None
        rmake_log.setupLogging(consoleLevel=logging.WARNING)
        options.MainHandler.__init__(self)

    def setup(self, **kwargs):
        pass

    def preFork(self):
        pass

    def doWork(self):
        raise NotImplementedError

    def _lock_path(self):
        return os.path.join(self.cfg.lockDir, "%s.pid" % self.name)

    def readLockFile(self):
        path = self._lock_path()
        try:
            return int(open(path).readline().strip())
        except IOError, err:
            if err.errno != errno.ENOENT:
                raise
            return None
        except ValueError:
            return None

    def writeLockFile(self):
        if not os.path.isdir(self.cfg.lockDir):
            os.makedirs(self.cfg.lockDir)
        path = self._lock_path()
        open(path, 'w').write('%s\n' % os.getpid())

    def removeLockFile(self):
        path = self._lock_path()
        try:
            os.unlink(path)
        except OSError, err:
            if err.errno != errno.ENOENT:
                raise

    def testDaemon(self, pid=None):
        if pid is None:
            pid = self.readLockFile()
            if pid is None:
                # Lock file does not exist.
                return None

        try:
            fObj = open('/proc/%s/cmdline' % (pid,))
        except IOError, err:
            # Process in lock file does not exist.
            try:
                os.stat('/proc/uptime')
            except OSError, err:
                if err.errno != errno.ENOENT:
                    raise
                sys.exit("You must mount /proc to use this program.")
            return None

        cmdline = fObj.read().replace('\0', ' ').split()
        exe = os.path.basename(cmdline[0])
        if exe.startswith('python'):
            exe = os.path.basename(cmdline[1])

        if exe != self.name:
            # Process in lock file is not this process.
            return None

        return pid

    def kill(self):
        pid = self.readLockFile()
        if pid is None:
            sys.exit("Could not kill %s: lock file %s does not exist" %
                    (self.name, self._lock_path()))

        if not self.testDaemon(pid):
            sys.exit("Could not kill %s: process %s is not a %s" % (self.name,
                pid, self.name))

        try:
            signals = [signal.SIGTERM, signal.SIGQUIT, signal.SIGKILL]
            for signum in signals:
                os.kill(pid, signum)

                # Wait for the process to exit.
                for x in range(50):
                    os.kill(pid, 0)
                    time.sleep(0.1)

        except OSError, err:
            if err.errno != errno.ESRCH:
                raise
            # Process no longer exists, so the kill was successful.
            self.removeLockFile()
            return

        sys.exit("Failed to kill %s process %s" % (self.name, pid))

    def start(self, argSet):
        pid = self.testDaemon()
        if pid:
            sys.exit("%s already running as PID %s" % (self.name, pid))

        fork = not argSet.get('no-daemon')
        debug = bool(argSet.get('debug-all'))

        self.setup(argSet=argSet, fork=fork, debug=debug)
        self._dropPrivs()
        self.preFork()

        if not fork:
            self._run()
            return 0

        # Double-fork in order to be reparented by init.
        pid = os.fork()
        if pid:
            # This is the original calling process.
            pid, status = os.waitpid(pid, 0)
            if status:
                sys.exit("%s failed to start: process exited with "
                        "status %s" % (self.name, pid, status))
            return 0

        try:
            if os.fork():
                # This is the intermediate process.
                os._exit(0)
        except:
            os._exit(70)

        # This is the daemon process.
        try:
            sys.stdout.flush()
            sys.stderr.flush()

            os.setsid()
            sink = os.open(os.devnull, os.O_RDWR)
            os.dup2(sink, 0)
            os.dup2(sink, 1)
            os.dup2(sink, 2)
            os.close(sink)

            self._run()
            os._exit(0)
        except:
            os._exit(70)

    def _dropPrivs(self):
        if os.getuid():
            # Nothing we can do here.
            return

        if self.capabilities:
            # libcap isn't available in chrooted environments, so don't import
            # it unless we're actually going to use it.
            from rmake.lib import pycap

        if self.user:
            pwent = pwd.getpwnam(self.user)
            if self.groups:
                groupIds = []
                for group in self.groups:
                    grpent = grp.getgrnam(group)
                    groupIds.append(grpent.gr_gid)
                os.setgroups(groupIds)
            else:
                os.setgroups([])

            if self.capabilities:
                pycap.set_keepcaps(True)

            os.setgid(pwent.pw_gid)
            os.setuid(pwent.pw_uid)

        if self.capabilities:
            pycap.cap_set_proc(self.capabilities)

    def _run(self):
        '''Call this to execute the daemon'''
        self.writeLockFile()
        try:
            try:
                self.doWork()
            except KeyboardInterrupt:
                log.info("Caught SIGINT; exiting")
            except:
                log.exception("Unhandled exception in daemon:")
        finally:
            self.removeLockFile()

    def runCommand(self, thisCommand, cfg, argSet, otherArgs, **kw):
        self.cfg = cfg
        return options.MainHandler.runCommand(self, thisCommand, self, cfg,
                argSet, otherArgs, **kw)

    def getConfigFile(self, argv):
        return self.configClass()


class DaemonService(Daemon, deferred_service.MultiService):
    """
    Daemon implementation that acts as a Twisted multi-service.
    """

    def __init__(self):
        Daemon.__init__(self)
        deferred_service.MultiService.__init__(self)
        from twisted.internet import reactor
        self.reactor = reactor

    def setup(self, **kwargs):
        super(DaemonService, self).setup(**kwargs)
        self.privilegedStartService()

    def _runPostStart(self):
        d = self.postStartService()
        def start_success(dummy):
            log.debug("Daemon is running")
        def on_error(reason):
            rmake_log.logFailure(reason, "Daemon startup failed:")
            try:
                self.reactor.stop()
            except twerror.ReactorNotRunning:
                pass
        d.addCallbacks(start_success, on_error)

    def preFork(self):
        self.startService()
        self.reactor.addSystemEventTrigger('before', 'shutdown',
                self.stopService)

    def doWork(self):
        self.reactor.callWhenRunning(self._runPostStart)
        self.reactor.run()


class LoggingMixin(Daemon):

    logFileName = None

    def getLogPath(self):
        assert self.logFileName
        return os.path.join(self.cfg.logDir, self.logFileName)

    def setup(self, **kwargs):
        if kwargs['fork']:
            consoleLevel = None
        elif kwargs['debug']:
            consoleLevel = logging.DEBUG
        else:
            consoleLevel = logging.INFO
        rmake_log.setupLogging(logPath=self.getLogPath(),
                fileLevel=logging.INFO, consoleLevel=consoleLevel,
                fileFormat='file', consoleFormat='file',
                withTwisted=True)
        super(LoggingMixin, self).setup(**kwargs)


class PluginsMixin(Daemon):

    plugins = None
    pluginTypes = ()

    def getConfigFile(self, argv):
        self.plugins = pluginlib.getPluginManager(argv, self.configClass,
                self.pluginTypes)
        return Daemon.getConfigFile(self, argv)

    def setup(self, **kwargs):
        log.debug("Plugins loaded:")
        for plugin in self.plugins.plugins:
            log.debug("  %s from %s", plugin.name, plugin.path)
        super(PluginsMixin, self).setup(**kwargs)


def debugHook(signum, sigtb):
    port = 8080
    try:
        import epdb
        try:
            from epdb.epdb_server import InvertedTelnetServer
        except ImportError:
            from epdb.telnetserver import InvertedTelnetServer
        debugger = epdb.Epdb()
        debugger._server = InvertedTelnetServer(('', port))
        debugger._server.handle_request()
        debugger._port = port
        debugger.set_trace(skip=1)
    except:
        pass


def setDebugHook():
    signal.signal(signal.SIGUSR1, debugHook)
