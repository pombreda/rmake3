#!/usr/bin/python
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


"""
Simple client that communicates with rMake.
"""

import errno
import logging
import os
import sys


from conary import conarycfg
from conary import errors as conaryerrors
from conary.lib import cfg, log
from conary.lib import options

from rmake import constants
from rmake import errors
from rmake.lib import logger as rmake_log
from rmake.lib import pluginlib


sys.excepthook = errors.genExcepthook(debug=False)

from rmake import compat

from rmake.build import buildcfg
from rmake.cmdline import command
from rmake.cmdline import helper


class RmakeMain(options.MainHandler):
    name = 'rmake'
    version = constants.version

    abstractCommand = command.rMakeCommand
    configClass = buildcfg.BuildConfiguration

    useConaryOptions = False
    setSysExcepthook = False

    commandList = command._commands

    def usage(self, rc=1, showAll=False):
        print 'rmake: front end to rMake build tool'
        if not showAll:
            print
            print 'Common Commands (use "rmake help" for the full list)'
        return options.MainHandler.usage(self, rc, showAll=showAll)

    def initializePlugins(self, argv):
        p = pluginlib.getPluginManager(argv, buildcfg.BuildConfiguration,
                readFiles=True)
        p.p.client.preInit(self, argv)
        return p

    def getConfigFile(self, argv):
        pluginManager = self.initializePlugins(argv)
        if '--skip-default-config' in argv:
            argv.remove('--skip-default-config')
            read = False
        else:
            read = True

        buildConfig = buildcfg.BuildConfiguration(readConfigFiles=read)
        conaryConfig = conarycfg.ConaryConfiguration(readConfigFiles=read)
        return buildConfig, conaryConfig, pluginManager


    def runCommand(self, thisCommand, (buildConfig, conaryConfig,
                                       pluginManager), argSet, args):
        pluginManager.p.client.preCommand(self, thisCommand,
                (buildConfig, conaryConfig), argSet, args)
        compat.checkRequiredVersions()
        thisCommand.verbose = (log.getVerbosity() <= log.INFO)
        if args[1] != 'help':
            # NOTE: the help system assumes that the base level of output
            # you want is "warning", but rmake is more verbose than that.
            # Due to limitations in how configurable the help system is, 
            # I can't easily fix that.  Someday I should though.  For now,
            # if we're running help, we make log.WARNING the default level,
            # and otherwise log.INFO is the default.
            log.setMinVerbosity(log.INFO)

        # don't let the buildFlavor be overridden yet
        client = helper.rMakeHelper(buildConfig=buildConfig)

        pluginManager.p.client.preCommand2(self, client, thisCommand)

        try:
            return options.MainHandler.runCommand(self, thisCommand, client, 
                                                  buildConfig, argSet, args)
        except errors.BadParameters:
            if not thisCommand.verbose:
                log.setVerbosity(log.WARNING)
            thisCommand.usage()
            raise

def main(argv):
    try:
        argv = list(argv)
        debugAll = '--debug-all' in argv
        if debugAll:
            debuggerException = Exception
            argv.remove('--debug-all')
        else:
            debuggerException = errors.RmakeInternalError
        sys.excepthook = errors.genExcepthook(debug=debugAll,
                                              debugCtrlC=debugAll)
        compat.ConaryVersion().checkRequiredVersion()
        return RmakeMain().main(argv, debuggerException=debuggerException)
    except debuggerException, err:
        raise
    except errors.OpenError, err:
        log.error(str(err) + ' -- check to see that the server is running.')
        return 1
    except (errors.RmakeError, conaryerrors.ConaryError, cfg.ParseError,
            conaryerrors.CvcError), err:
        log.error(err)
        return 1
    except IOError, e:
        # allow broken pipe to exit
        if e.errno != errno.EPIPE:
            raise
    except KeyboardInterrupt:
        return 1
    return 0
