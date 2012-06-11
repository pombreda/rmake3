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


import copy
import itertools
import os
import tempfile
import traceback

#conary
from conary.build import cook,loadrecipe,lookaside,recipe,use
from conary.build import errors as builderrors
from conary import conarycfg
from conary import conaryclient
from conary.deps import deps
from conary.lib import log,util
from conary.deps.deps import Flavor
from conary.repository import trovesource

#rmake
from rmake import compat
from rmake import errors
from rmake import failure
from rmake.lib import flavorutil
from rmake.lib import repocache
from rmake.build import buildtrove


def getRecipes(repos, troveTups):
    fileIds = []
    troveSpecs = [ (x[0], x[1], Flavor()) for x in troveTups ]
    troves = repos.getTroves(troveSpecs)
    for i, trove in enumerate(troves):
        filename = trove.getName().split(':')[0] + '.recipe'
        found = False
        for (pathId, filePath, fileId, fileVersion) in trove.iterFileList():
            if filePath == filename:
                fileIds.append((fileId, fileVersion))
                found = True
                break
    repos.getFileContents(fileIds) #caches files
    return troves

def loadRecipe(repos, name, version, flavor, trv,
               defaultFlavor=None, loadInstalledSource=None,
               installLabelPath=None, buildLabel=None, groupRecipeSource=None,
               cfg=None):
    name = name.split(':')[0]
    try:
        if defaultFlavor is not None:
            fullFlavor = deps.overrideFlavor(defaultFlavor, flavor)
        else:
            fullFlavor = flavor
        # set up necessary flavors and track used flags before
        # calling loadRecipe, since even loading the class
        # may check some flags that may never be checked inside
        # the recipe
        recipeObj, loader = getRecipeObj(repos, name,
                                       version, fullFlavor, trv,
                                       loadInstalledSource=loadInstalledSource,
                                       installLabelPath=installLabelPath,
                                       buildLabel=buildLabel,
                                       groupRecipeSource=groupRecipeSource,
                                       cfg=cfg)
        relevantFlavor = use.usedFlagsToFlavor(recipeObj.name)
        relevantFlavor = flavorutil.removeInstructionSetFlavor(relevantFlavor)
        # always add in the entire arch flavor.  We need to ensure the
        # relevant flavor is unique per architecture, also, arch flavors
        # can affect the macros used.
        if defaultFlavor is not None:
            relevantFlavor.union(flavor)
        relevantFlavor.union(flavorutil.getArchFlags(fullFlavor))
        relevantFlags = flavorutil.getFlavorUseFlags(relevantFlavor)
        flags = flavorutil.getFlavorUseFlags(fullFlavor)
        use.track(False)

        for flagSet in ('Use',):
        # allow local flags not to be used -- they are set to their default
            if flagSet not in relevantFlags:
                continue
            for flag in relevantFlags[flagSet]:
                if flag not in flags[flagSet]:
                    raise (RuntimeError,
                            "Recipe %s uses Flavor %s but value not known" %(name, flag))
        if 'Arch' in relevantFlags:
            for majarch in relevantFlags['Arch'].keys():
                for subarch in relevantFlags['Arch'][majarch]:
                    if not use.Arch[majarch][subarch]:
                        #negative values for subarches are assumed
                        continue
                    if subarch not in flags['Arch'][majarch]:
                        log.error("arch %s.%s used but not specified" % (
                                                         majarch, subarch))
                        raise RuntimeError, (
                                "arch %s.%s used but not specified" % (
                                                         majarch, subarch))
            use.resetUsed()
    except:
        log.error('Error Loading Recipe (%s, %s, %s):\n%s' %
                                    (name, version, fullFlavor,
                                     ''.join(traceback.format_exc())))
        raise
    return loader, recipeObj, relevantFlavor


def getRecipeObj(repos, name, version, flavor, trv,
                 loadInstalledSource=None, installLabelPath=None, 
                 loadRecipeSpecs=None, buildLabel = None,
                 groupRecipeSource=None, cfg=None):
    if cfg:
        cfg = copy.deepcopy(cfg)
    else:
        cfg = conarycfg.ConaryConfiguration(False)
    cfg.initializeFlavors()
    branch = version.branch()
    if not buildLabel:
        buildLabel = version.branch().label()
    if not installLabelPath:
        cfg.installLabelPath = [buildLabel]
    else:
        cfg.installLabelPath = installLabelPath
    cfg.buildFlavor = flavor
    cfg.defaultBasePackages = []
    name = name.split(':')[0]
    use.LocalFlags._clear()
    assert(flavorutil.getArch(flavor))
    use.setBuildFlagsFromFlavor(name, flavor, error=False)
    use.resetUsed()
    use.track(True)
    ignoreInstalled = not loadInstalledSource
    macros = {'buildlabel' : buildLabel.asString(),
              'buildbranch' : version.branch().asString()}
    cfg.lookaside = tempfile.mkdtemp()
    try:
        loader = RecipeLoaderFromSourceTrove(trv, repos, cfg,
                                         name + ':source', branch,
                                         ignoreInstalled=ignoreInstalled,
                                         db=loadInstalledSource,
                                         buildFlavor=flavor)
        recipeClass = loader.getRecipe()
        recipeClass._trove = trv
        if recipe.isGroupRecipe(recipeClass):
            recipeObj = recipeClass(repos, cfg, buildLabel, flavor, None,
                                extraMacros=macros)
            recipeObj.sourceVersion = version
            recipeObj.loadPolicy()
            recipeObj.setup()
            if groupRecipeSource:
                recipeObj.troveSource = groupRecipeSource
                sourceComponents = recipeObj._findSources(groupRecipeSource)
                recipeObj.delayedRequires = sourceComponents
        elif (recipe.isPackageRecipe(recipeClass) or
              recipe.isFactoryRecipe(recipeClass) or
              recipe.isCapsuleRecipe(recipeClass)):
            if recipe.isFactoryRecipe(recipeClass):
                #This requires a specific capability in conary
                compat.ConaryVersion().requireFactoryRecipeGeneration()
                #Load the FactoryRecipe
                factoryClass = recipeClass
                loaded = cook.loadFactoryRecipe(factoryClass, cfg, repos, flavor)
                recipeClass = loaded.getRecipe()
            lcache = lookaside.RepositoryCache(repos)
            recipeObj = recipeClass(cfg, lcache, [], macros, lightInstance=True)
            recipeObj.sourceVersion = version
            recipeObj.populateLcache()
            if not recipeObj.needsCrossFlags():
                recipeObj.crossRequires = []
            recipeObj.loadPolicy()
            recipeObj.setup()
        elif recipe.isInfoRecipe(recipeClass):
            recipeObj = recipeClass(cfg, None, None, macros)
            recipeObj.sourceVersion = version
            recipeObj.setup()
        elif recipe.isRedirectRecipe(recipeClass):
            binaryBranch = version.getBinaryVersion().branch()
            recipeObj = recipeClass(repos, cfg, binaryBranch, flavor)
            recipeObj.sourceVersion = version
            recipeObj.setup()
        elif recipe.isFileSetRecipe(recipeClass):
            recipeObj = recipeClass(repos, cfg, buildLabel, flavor, extraMacros=macros)
            recipeObj.sourceVersion = version
            recipeObj.setup()
        else:
            raise RuntimeError, 'Unknown class type %s for recipe %s' % (recipeClass, name)
    finally:
        util.rmtree(cfg.lookaside)
    return recipeObj, loader

def loadRecipeClass(repos, name, version, flavor, trv=None,
                    ignoreInstalled=True, root=None, 
                    loadInstalledSource=None, overrides=None,
                    buildLabel=None, cfg=None):
    if trv is None:
        trv = repos.getTrove(name,version,deps.parseFlavor(''), withFiles=True)

    if cfg is None:
        cfg = conarycfg.ConaryConfiguration(False)
    else:
        cfg = copy.deepcopy(cfg)
    cfg.initializeFlavors()
    if root:
        cfg.root = root
    branch = version.branch()
    label = version.branch().label()
    cfg.buildLabel = label
    cfg.buildFlavor = flavor
    name = name.split(':')[0]

    use.LocalFlags._clear()
    use.setBuildFlagsFromFlavor(name, flavor, error=False)
    use.resetUsed()
    use.track(True)

    loader = RecipeLoaderFromSourceTrove(trv, repos, cfg,
                                     name + ':source', branch,
                                     ignoreInstalled=ignoreInstalled,
                                     db=loadInstalledSource,
                                     overrides=overrides,
                                     buildFlavor=flavor)
    recipeClass = loader.getRecipe()
    recipeClass._trove = trv

    use.track(False)
    localFlags = flavorutil.getLocalFlags()
    usedFlags = use.getUsed()
    use.LocalFlags._clear()
    return loader, recipeClass, localFlags, usedFlags

def _getLoadedSpecs(loader, recipeObj):
    loadedSpecsFn = getattr(loader, 'getLoadedSpecs', None)
    if loadedSpecsFn:
        return loader.getLoadedSpecs()
    recipeClass = recipeObj.__class__
    loadedSpecs = getattr(recipeClass, '_loadedSpecs', {})
    if not loadedSpecs:
        return {}
    finalDict = {}
    toParse = [(finalDict, loadedSpecs)]
    while toParse:
        specDict, unparsedSpecs = toParse.pop()
        for troveSpec, (troveTup, recipeClass) in unparsedSpecs.items():
            newDict = {}
            specDict[troveSpec] = (troveTup, newDict)
            toParse.append((newDict, getattr(recipeClass, '_loadedSpecs', {})))
    return finalDict

def loadSourceTroves(job, repos, buildFlavor, troveList,
                     loadInstalledSource=None, installLabelPath=None,
                     groupRecipeSource=None, internalHostName=None, 
                     total=0, count=0):
    """
    Load the source troves associated with a set of build troves
    C{troveList}. Returns a mapping of C{(name, version, flavor,
    context)} to L{LoadTroveResult<rmake.build.buildtrove.LoadTroveResult>}
    indicating information loaded from the source such as packages
    and build requirements.
    """
    if not total:
        total = len(troveList)
    job.log('Downloading %s recipes...' % len(troveList))
    troveList = list(sorted(troveList, key=lambda x: x.getName()))
    troves = getRecipes(repos, [x.getNameVersionFlavor() for x in troveList])
    resultSet = {}

    for idx, (buildTrove, trove) in enumerate(itertools.izip(troveList, troves)):
        n,v,f = buildTrove.getNameVersionFlavor()
        job.log('Loading %s out of %s: %s' % (count + idx + 1, total, n))
        relevantFlavor = None
        if v.getHost() == internalHostName:
            buildLabel = v.branch().parentBranch().label()
        else:
            buildLabel = v.trailingLabel()
        try:
            (loader, recipeObj, relevantFlavor) = loadRecipe(repos,
                                 n, v, f,
                                 trove,
                                 buildFlavor,
                                 loadInstalledSource=loadInstalledSource,
                                 installLabelPath=installLabelPath,
                                 buildLabel=buildLabel,
                                 groupRecipeSource=groupRecipeSource,
                                 cfg=job.getTroveConfig(buildTrove))
            buildTrove.setFlavor(relevantFlavor)
            buildTrove.setRecipeType(buildtrove.getRecipeType(recipeObj))
            buildTrove.setLoadedSpecsList([_getLoadedSpecs(loader, recipeObj)])
            if hasattr(loader, 'getLoadedTroves'):
                buildTrove.setLoadedTroves(loader.getLoadedTroves())
            else:
                buildTrove.setLoadedTroves(recipeObj.getLoadedTroves())
            buildTrove.setDerivedPackages(set(
                getattr(recipeObj, 'packages', [recipeObj.name])))
            if 'delayedRequires' in recipeObj.__dict__:
                buildTrove.setDelayedRequirements(recipeObj.delayedRequires)
            buildTrove.setBuildRequirements(set(
                getattr(recipeObj, 'buildRequires', [])))
            buildTrove.setCrossRequirements(set(
                getattr(recipeObj, 'crossRequires', [])))

        except Exception, err:
            if isinstance(err, errors.RmakeError):
                # we assume our internal errors have enough info
                # to determine what the bug is.
                fail = failure.LoadFailed(str(err))
            else:
                fail = failure.LoadFailed(str(err), traceback.format_exc())
            buildTrove.troveFailed(fail)


def loadSourceTrovesForJob(job, troveList=None, repos=None, reposName=None):
    cfg = job.getMainConfig()
    if repos:
        cacheDir = None
    else:
        cacheDir = tempfile.mkdtemp(prefix='rmake-trovecache-')
        try:
            client = conaryclient.ConaryClient(cfg)
            repos = repocache.CachingTroveSource(client.getRepos(), cacheDir)
        except:
            util.rmtree(cacheDir)
            raise

    try:
        if troveList is None:
            troveList = list(job.iterLoadableTroves())
        if not troveList:
            return {}
        if reposName is None:
            reposName = cfg.reposName

        tupList = sorted(x.getNameVersionFlavor() for x in troveList)

        # create fake "packages" for all the troves we're building so that
        # they can be found for loadInstalled.
        if not cfg.isolateTroves:
            buildTrovePackages = [ (x[0].split(':')[0], x[1], x[2])
                for x in tupList ]
            buildTroveSource = RemoveHostSource(trovesource.SimpleTroveSource(
                buildTrovePackages), reposName)
        else:
            buildTroveSource = None

        # don't search the internal repository explicitly for loadRecipe
        # sources - they may be a part of some bogus build.
        repos = RemoveHostRepos(repos, reposName)

        groupRecipeSource = RemoveHostSource(trovesource.SimpleTroveSource(
            tupList), reposName)

        trovesByConfig = {}
        for trove in troveList:
            trovesByConfig.setdefault(trove.getContext(), []).append(trove)

        total = len(troveList)
        count = 0
        for context, contextTroves in trovesByConfig.items():
            buildCfg = contextTroves[0].cfg

            loadInstalledList = [ trovesource.TroveListTroveSource(repos, x)
                for x in buildCfg.resolveTroveTups ]
            loadInstalledList.append(repos)

            if buildTroveSource is not None:
                loadInstalledSource = trovesource.stack(buildTroveSource,
                                                        *loadInstalledList)
            else:
                loadInstalledSource = trovesource.stack(*loadInstalledList)

            loadInstalledRepos = trovesource.stack(*loadInstalledList)
            if isinstance(loadInstalledRepos, trovesource.TroveSourceStack):
                for source in loadInstalledRepos.iterSources():
                    source._getLeavesOnly = True
                    source.searchWithFlavor()
                    # keep allowNoLabel set.
            else:
                loadInstalledRepos._getLeavesOnly = True
                loadInstalledRepos.searchWithFlavor()
            cachedRepos = CachingSource(loadInstalledRepos)

            loadSourceTroves(job, cachedRepos,
                buildCfg.buildFlavor, contextTroves, total=total, count=count,
                loadInstalledSource=loadInstalledSource,
                installLabelPath=buildCfg.installLabelPath,
                groupRecipeSource=groupRecipeSource,
                internalHostName=reposName)
            count += len(contextTroves)
    finally:
        if cacheDir:
            util.rmtree(cacheDir)

class RemoveHostRepos(object):
    def __init__(self, troveSource, host):
        self.troveSource = troveSource
        self.host = host

    def __getattr__(self, attr):
        return getattr(self.troveSource, attr)

    def findTroves(self, labelPath, *args, **kw):
        if labelPath is not None:
            labelPath = [ x for x in labelPath if x.getHost() != self.host]
        return self.troveSource.findTroves(labelPath, *args, **kw)

    def findTrove(self, labelPath, *args, **kw):
        if labelPath is not None:
            labelPath = [ x for x in labelPath if x.getHost() != self.host]
        return self.troveSource.findTrove(labelPath, *args, **kw)

class CachingSource(object):
    """
        Trovesource that caches calls to findTrove(s).
    """
    def __init__(self, troveSource):
        self.troveSource = troveSource
        self._cache = {}

    def __getattr__(self, key):
        return getattr(self.troveSource, key)

    def findTroves(self, installLabelPath, troveTups, *args, **kw):
        """
            Caching findTroves call.
        """
        finalResults = {}
        toFind = []
        # cache is {troveTup : [((ILP, *args, **kw), result)]}
        # first find troveTup in cache then search all the ILP, args, kw
        # pairs we've cached before.
        key = (installLabelPath, args, sorted(kw.items()))
        for troveTup in troveTups:
            if troveTup in self._cache:
                results = [ x[1] for x in self._cache[troveTup] if x[0] == key ]
                if results:
                    finalResults[troveTup] = results[0]
                    continue
            toFind.append(troveTup)
        newResults = self.troveSource.findTroves(installLabelPath, toFind, *args, **kw)
        for troveTup, troveList in newResults.iteritems():
            self._cache.setdefault(troveTup, []).append((key, troveList))
        finalResults.update(newResults)
        return finalResults

    def findTrove(self, labelPath, troveTup, *args, **kw):
        return self.findTroves(labelPath, [troveTup], *args, **kw)[troveTup]

class RemoveHostSource(trovesource.SearchableTroveSource):
    def __init__(self, troveSource, host):
        self.troveSource = troveSource
        self.host = host
        trovesource.SearchableTroveSource.__init__(self)
        self._bestFlavor = troveSource._bestFlavor
        self._getLeavesOnly = troveSource._getLeavesOnly
        self._flavorCheck = troveSource._flavorCheck
        self._allowNoLabel = troveSource._allowNoLabel

    def iterTrovesByPath(self, *args, **kw):
        return []

    def close(self):
        return getattr(self.troveSource, 'close')()

    def resolveDependencies(self, label, *args, **kw):
        if self._allowNoLabel:
            return self.troveSource.resolveDependencies(label, *args, **kw)

        suggMap = self.troveSource.resolveDependencies(None, *args, **kw)
        for depSet, solListList in suggMap.iteritems():
            newSolListList = []
            for solList in solListList:
                newSolList = []
                for sol in solList:
                    trailingLabel = sol[1].trailingLabel()
                    if trailingLabel == label:
                        newSolList.append(sol)
                    if trailingLabel.getHost() != self.host:
                        continue
                    if not sol[1].branch().hasParentBranch():
                        continue
                    if sol[1].branch().parentBranch().label() != label:
                        continue
                    newSolList.append(sol)
                newSolListList.append(newSolList)
            suggMap[depSet] = newSolListList
        return suggMap


    def resolveDependenciesByGroups(self, *args, **kw):
        return self.troveSource.resolveDependenciesByGroups(*args, **kw)

    def trovesByName(self, name):
        return self.troveSource.trovesByName(name)

    def hasTroves(self, *args, **kw):
        return self.troveSource.hasTroves(*args, **kw)

    def findTroves(self, labelPath, *args, **kw):
        if labelPath is not None:
            newPath = []
            for label in labelPath:
                if label.getHost() != self.host:
                    newPath.append(label)
            labelPath = newPath
        return trovesource.SearchableTroveSource.findTroves(self, labelPath,
                                                            *args, **kw)

    def getLabelsForTroveName(self, troveName,
                              troveTypes=trovesource.TROVE_QUERY_PRESENT):
        versionList = self.getTroveVersionList(troveName,
                                               troveTypes=troveTypes)
        labelSet = set()
        for version in versionList:
            if version.getHost() == self.host:
                labelSet.add(self._removeLabel(version).trailingLabel())
            else:
                labelSet.add(version.trailingLabel())
        return labelSet

    def _removeLabel(self, version):
        upVersion = None
        if version.hasParentVersion():
            return version.parentVersion()
        elif version.branch().hasParentBranch():
            branch = version.branch().parentBranch()
            shadowLength = version.shadowLength() - 1
            revision = version.trailingRevision().copy()
            if revision.buildCount is not None:
                revision.buildCount.truncateShadowCount(shadowLength)
            revision.sourceCount.truncateShadowCount(shadowLength)
            upVersion = branch.createVersion(revision)
            if (revision.buildCount is not None 
                and list(revision.buildCount.iterCounts())[-1] == 0):
                upVersion.incrementBuildCount()
            return upVersion
        else:
            return version

    def _filterByVersionQuery(self, versionType, versionList, versionQuery):
        versionMap = {}
        for version in versionList:
            upVersion = version
            if version.trailingLabel().getHost() == self.host:
                upVersion = self._removeLabel(version)
            versionMap.setdefault(upVersion, []).append(version)
            versionMap.setdefault(version, []).append(version)
        results = trovesource.SearchableTroveSource._filterByVersionQuery(
                                                        self, versionType,
                                                        versionMap.keys(),
                                                        versionQuery)
        return dict((x[0],
                     list(itertools.chain(*[versionMap[y] for y in x[1]])))
                     for x in results.items())


if hasattr(loadrecipe, 'RecipeLoaderFromSourceTrove'):
    RecipeLoaderFromSourceTrove = loadrecipe.RecipeLoaderFromSourceTrove
else:
    class RecipeLoaderFromSourceTrove(loadrecipe.RecipeLoader):

        @staticmethod
        def findFileByPath(sourceTrove, path):
            for (pathId, filePath, fileId, fileVersion) in sourceTrove.iterFileList():
                if filePath == path:
                    return (fileId, fileVersion)

            return None

        def __init__(self, sourceTrove, repos, cfg, versionStr=None, 
                     labelPath=None,
                     ignoreInstalled=False, filterVersions=False,
                     parentDir=None, defaultToLatest = False,
                     buildFlavor = None, db = None, overrides = None,
                     getFileFunction = None, branch = None):
            self.recipes = {}

            if getFileFunction is None:
                getFileFunction = lambda repos, fileId, fileVersion, path: \
                        repos.getFileContents([ (fileId, fileVersion) ])[0].get()

            name = sourceTrove.getName().split(':')[0]

            recipePath = name + '.recipe'
            match = self.findFileByPath(sourceTrove, recipePath)

            if not match:
                # this is just missing the recipe; we need it
                raise builderrors.RecipeFileError("version %s of %s does not "
                                                  "contain %s" %
                          (sourceTrove.getName(),
                           sourceTrove.getVersion().asString(),
                           recipePath))

            (fd, recipeFile) = tempfile.mkstemp(".recipe", 'temp-%s-' %name, 
                                                dir=cfg.tmpDir)
            outF = os.fdopen(fd, "w")

            inF = getFileFunction(repos, match[0], match[1], recipePath)

            util.copyfileobj(inF, outF)

            del inF
            outF.close()
            del outF

            if branch is None:
                branch = sourceTrove.getVersion().branch()

            try:
                loadrecipe.RecipeLoader.__init__(self, recipeFile, cfg, repos,
                          sourceTrove.getName(),
                          branch = branch,
                          ignoreInstalled=ignoreInstalled,
                          directory=parentDir, buildFlavor=buildFlavor,
                          db=db, overrides=overrides)
            finally:
                os.unlink(recipeFile)

            self.recipe._trove = sourceTrove.copy()
