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
Describes a BuildConfiguration, which is close to, but neither a subset nor
a superset of a conarycfg file.
"""

import os
import re
import urllib

from conary import conarycfg
from conary import versions
from conary.deps import deps
from conary.lib import cfg,cfgtypes
from conary.lib import log as clog
from conary.conarycfg import CfgLabel
from conary.conarycfg import ParseError
from conary.conaryclient import cmdline
from conary.lib.cfgtypes import (CfgBool, CfgList, CfgDict, CfgString,
                                 CfgInt, CfgType, CfgQuotedLineList, 
                                 CfgPathList)

from rmake.cmdline import cmdutil



class CfgDependency(CfgType):

    def parseString(self, val):
        return deps.parseDep(val)

    def format(self, val, displayOptions=None):
        return str(val)


class CfgTroveSpec(CfgType):
    def parseString(self, val):
        return cmdline.parseTroveSpec(val)

    def format(self, val, displayOptions=None):
        if not val[2] is None:
            flavorStr = '[%s]' % val[2]
        else:
            flavorStr = ''
        if val[1]:
            verStr = '=%s' % val[1]
        else:
            verStr = ''

        return '%s%s%s' % (val[0], verStr, flavorStr)

class CfgTroveTuple(CfgType):
    def parseString(self, val):
        (name, version, flavor) = cmdline.parseTroveSpec(val)
        return (name, versions.VersionFromString(version), flavor)

    def format(self, val, displayOptions=None):
        return '%s=%s[%s]' % val

class CfgTroveTupleWithContext(CfgType):
    def parseString(self, val):
        (name, version, flavor, context) = cmdutil.parseTroveSpec(val)
        return (name, versions.VersionFromString(version), flavor, context)

    def format(self, val, displayOptions=None):
        return '%s=%s[%s]{%s}' % val


class CfgUUID(CfgType):

    def parseString(self, val):
        if not val:
            return ''
        newVal = val.replace('-', '').lower()
        if not re.match('^[0-9a-f]{32}$', newVal):
            raise ParseError, "Invalid UUID: '%s'" % val
        return newVal

    def toStrings(self, val, displayOptions):
        return [ val ]

class CfgUser(CfgType):
    def parseString(self, str):
        val = str.split()
        if len(val) < 1 or len(val) > 2:
            raise ParseError("expected <user> [<password>]")
        elif len(val) == 1:
            return (val[0], None)
        else:
            return tuple(val)

    def format(self, val, displayOptions=None):
        user, password = val
        if password is None:
            return user
        elif displayOptions.get('hidePasswords'):
            return '%s <password>' % (user)
        else:
            return '%s %s' % (user, password)


class RmakeBuildContext(cfg.ConfigSection):

    bootstrapTroves      = (CfgList(CfgTroveSpec), [],
            "INTERNAL USE ONLY: Troves to be installed before the remaining "
            "chroot contents.")
    copyInConary         = (CfgBool, False)
    copyInConfig         = (CfgBool, False) # RMK-1052
    rbuilderUrl          = (cfgtypes.CfgString, 'https://localhost/')
    rmakeUser            = (CfgUser, None)
    rmakeUrl             = (CfgString, 'unix:///var/lib/rmake/socket')
    clientCert           = (cfgtypes.CfgPath, None)
    defaultBuildReqs     = (CfgList(CfgString), [])
    resolveTroves        = (CfgList(CfgQuotedLineList(CfgTroveSpec)),
                            [[('group-dist', None, None)]])
    matchTroveRule       = (CfgList(CfgString), [])
    resolveTrovesOnly    = (CfgBool, False)
    reuseRoots           = (CfgBool, False)
    rpmRequirements      = (CfgList(CfgDependency), [],
            "INTERNAL USE ONLY: Dep provided by the RPM to be used for "
            "installation of the chroot.")
    strictMode           = (CfgBool, False)
    targetLabel          = (CfgLabel, versions.Label('NONE@local:NONE'))
    uuid                 = (CfgUUID, '')

    def __init__(self, parent, doc=None):
        cfg.ConfigSection.__init__(self, parent, doc=None)

        for info in conarycfg.ConaryContext._getConfigOptions():
            if info[0] not in self:
                self.addConfigOption(*info)


class BuildConfiguration(conarycfg.ConaryConfiguration):

    buildTroveSpecs      = CfgList(CfgTroveSpec)
    isolateTroves        = (CfgBool, False, "Ignore the results of other "
            "troves in the same job when searching for build requirements.")
    resolveTroveTups     = CfgList(CfgQuotedLineList(CfgTroveTuple))
    recurseGroups        = (CfgInt, 0)
    prepOnly             = (CfgBool, False)
    pluginDirs           = (CfgPathList, ['/usr/share/rmake/plugins',
                                          '~/.rmake/plugins.d'])
    usePlugin            = CfgDict(CfgBool)
    usePlugins           = (CfgBool, True)
    jobContext           = CfgList(CfgInt)
    recursedGroupTroves  = CfgList(CfgTroveTuple)
    reposName            = (CfgString, None)
    prebuiltBinaries               = CfgList(CfgTroveTuple)
    ignoreExternalRebuildDeps      = (CfgBool, False)
    ignoreAllRebuildDeps           = (CfgBool, False)
    primaryTroves = CfgList(CfgTroveTupleWithContext)

    # Here are options that are not visible from the command-line
    # and should not be displayed.  They are job-specific.  However,
    # they must be stored with the job, parsed with the job, etc.

    _hiddenOptions = [ 'buildTroveSpecs', 'resolveTroveTups', 'jobContext',
                       'recurseGroups', 'recursedGroupTroves',
                       'prebuiltBinaries', 'ignoreExternalRebuildDeps',
                       'ignoreAllRebuildDeps', 'primaryTroves', 'reposName',
                       ]

    _strictOptions = [ 'buildFlavor', 'buildLabel', 'cleanAfterCook','flavor',
                       'installLabelPath', 'repositoryMap', 'root',
                       'user', 'name', 'contact', 'signatureKey', 'dbPath',
                       'proxy', 'conaryProxy', 'lookaside', 'entitlement',
                       'autoLoadRecipes', 'proxyMap']

    _dirsToCopy = ['archDirs', 'mirrorDirs', 'siteConfigPath', 'useDirs', 
                   'componentDirs']
    _pathsToCopy = ['defaultMacros']
    _defaultSectionType   =  RmakeBuildContext

    def __init__(self, readConfigFiles=False, root='', conaryConfig=None, 
                 serverConfig=None, ignoreErrors=False, log=None, 
                 strictMode=None):
        # we default the value of these items to whatever they
        # are set to on the local system's conaryrc.
        conarycfg.ConaryConfiguration.__init__(self, readConfigFiles=False)
        if hasattr(self, 'setIgnoreErrors'):
            self.setIgnoreErrors(ignoreErrors)
        for info in RmakeBuildContext._getConfigOptions():
            if info[0] not in self:
                self.addConfigOption(*info)
        if strictMode is not None:
            self.strictMode = strictMode
        if not hasattr(self, 'rmakeUrl'):
            self.rmakeUrl = None
        if not hasattr(self, 'clientCert'):
            self.clientCert = None

        if readConfigFiles:
            if os.path.exists(root + '/etc/rmake/clientrc'):
                clog.warning(root + '/etc/rmake/clientrc should be renamed'
                                   ' to /etc/rmake/rmakerc')
                self.read(root + '/etc/rmake/clientrc', exception=False)
            self.read(root + '/etc/rmake/rmakerc', exception=False)
            if os.environ.has_key("HOME"):
                self.read(root + os.environ["HOME"] + "/" + ".rmakerc",
                          exception=False)
            self.read('rmakerc', exception=False)

        if self.strictMode:
            self.enforceManagedPolicy = True
            self.copyInConary = False
            self.copyInConfig = False

        # these values are not set based on 
        # config file values - we don't want to touch the system database, 
        # and we don't want to use conary's logging mechanism.
        self.root = ':memory:'
        self.dbPath = ':memory:'
        self.logFile = []
        for option in self._hiddenOptions:
            del self._lowerCaseMap[option.lower()]

        self.useConaryConfig(conaryConfig)
        if serverConfig:
            self.reposName = serverConfig.reposName
            self.repositoryMap.update(serverConfig.getRepositoryMap())
            self.user.extend(serverConfig.reposUser)

    def useConaryConfig(self, conaryConfig):
        def _shouldOverwrite(key, current, new):
            if key not in new:
                return False
            if (current.isDefault(key)
                    and current[key] == current.getDefaultValue(key)
                    and (not new.isDefault(key) or
                        new[key] != new.getDefaultValue(key))):
                return True
            return False
        if self.strictMode:
            if not conaryConfig:
                conaryConfig = conarycfg.ConaryConfiguration(False)
            for key in self.iterkeys():
                if key not in self._strictOptions and key in conaryConfig:
                    self.resetToDefault(key)

        if not conaryConfig:
            return

        # copy in conary config values that we haven't
        # overrided from the rmake config
        for key in self.iterkeys():
            if self.strictMode and key not in self._strictOptions:
                continue
            if  _shouldOverwrite(key, self, conaryConfig):
                self[key] = conaryConfig[key]

        for sectionName in conaryConfig.iterSectionNames():
            conarySection = conaryConfig.getSection(sectionName)
            if not self.hasSection(sectionName):
                self._addSection(sectionName, self._defaultSectionType(self))
            mySection = self.getSection(sectionName)
            for key in mySection.iterkeys():
                if self.strictMode and key not in self._strictOptions:
                    continue
                if  _shouldOverwrite(key, mySection, conarySection):
                    mySection[key] = conarySection[key]

        if self.strictMode:
            self.enforceManagedPolicy = True
            self.copyInConary = False
            self.copyInConfig = False
        if not self.copyInConfig:
            for option in self._dirsToCopy + self._pathsToCopy:
                self.resetToDefault(option)

    def getServerUri(self):
        schema, rest = urllib.splittype(self.rmakeUrl)
        if schema == 'unix':
            return self.rmakeUrl
        else:
            host, path = urllib.splithost(rest)
            user, host = urllib.splituser(host)
            host, port = urllib.splitport(host)
            if not port:
                port = 9999
            if self.rmakeUser:
                user = '%s:%s@' % self.rmakeUser
            else:
                user = ''
            return '%s://%s%s:%s%s' % (schema, user, host, port, path)

    def limitToHosts(self, hosts):
        if isinstance(hosts, str):
            hosts = [hosts]
        for host in hosts:
            if '@' in host or '=' in host:
                raise ParseError('Invalid host "%s"' % host)
            self.addMatchRule('=%s@' % host)

    def limitToLabels(self, labels):
        if isinstance(labels, str):
            labels = [labels]
        for label in labels:
            label = versions.Label(label)
            self.addMatchRule('=%s' % label)

    def addMatchRule(self, matchRule):
        self.configLine('matchTroveRule %s' % matchRule)

    def clearMatchRules(self):
        self.matchTroveRule = []
        for section in self.iterSections():
            section.matchTroveRule = []

    def getTargetLabel(self, versionOrLabel):
        if isinstance(versionOrLabel, versions.Label):
            cookLabel = versionOrLabel
        elif isinstance(versionOrLabel, versions.Branch):
            cookLabel = versionOrLabel.label()
        else:
            cookLabel = versionOrLabel.trailingLabel()

        targetLabel = self.targetLabel
        if targetLabel:
            # we treat NONE in the label as being a special target,
            # if your targetLabel is localhost@rpl:NONE, we build 
            # onto localhost@rpl:<branch> where branch is the branch
            # of the source version.

            needNewLabel = False
            if targetLabel.getHost().lower() == 'none':
                needNewLabel = True
                host = self.reposName
            else:
                host = targetLabel.getHost()

            if targetLabel.getNamespace().lower() == 'none':
                needNewLabel = True
                nameSpace = cookLabel.getNamespace()
            else:
                nameSpace = targetLabel.getNamespace()

            if targetLabel.branch.lower() == 'none':
                needNewLabel = True
                branch = cookLabel.branch
            else:
                branch = targetLabel.branch

            if needNewLabel:
                targetLabel = '%s@%s:%s' % (host, nameSpace, branch)
                targetLabel = versions.Label(targetLabel)
            return targetLabel
        else:
            return cookLabel

    def dropContexts(self):
        self._sections = {}

    def storeConaryCfg(self, out):
        conaryCfg = conarycfg.ConaryConfiguration(False)
        for key, value in self.iteritems():
            if self.isDefault(key):
                continue
            if key in conaryCfg:
                if key == 'macros':
                    # multi-line macros break conary config files (RMK-996)
                    continue
                if key == 'context':
                    # we're not writing out contexts!
                    continue
                conaryCfg[key] = value
        conaryCfg.store(out, includeDocs=False)

    def getMacros(self):
        # multi-line macros break conary config files, so provide
        # any macros separately (RMK-996)
        if 'macros' in self:
            macros = sorted(x for x in self.macros.iteritems())
            if macros:
                return '\n'.join('%s = %r' % x for x in macros) + '\n'
        return ''

    def _writeKey(self, out, cfgItem, value, options):
        if cfgItem.name in self._hiddenOptions:
            if not options.get('displayHidden', False):
                return
        conarycfg.ConaryConfiguration._writeKey(self, out, cfgItem,
                                                self[cfgItem.name], options)
