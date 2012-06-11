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


import os
import sys


from rmake_test import rmakehelp

from conary import conaryclient
from conary.deps import deps
from conary.lib import util

from rmake.lib import repocache


class RepositoryCacheTest(rmakehelp.RmakeHelper):
    def testBasic(self):
        fooRun = self.addComponent('foo:runtime', '1')
        barRun = self.addComponent('bar:runtime', '1', '', ['/foobar'])
        foo = self.addCollection('foo', '1', [':runtime'])
        bar = self.addCollection('bar', '1', [':runtime'])

        cacheDir = self.workDir + '/cache'
        util.mkdirChain(cacheDir)

        client = conaryclient.ConaryClient(self.cfg)
        repos = self.openRepository()
        store = repocache.RepositoryCache(cacheDir)

        foocs, barruncs  = store.getChangeSetsForTroves(repos,
                                          [foo.getNameVersionFlavor(),
                                           barRun.getNameVersionFlavor()],
                                           withFiles=True, 
                                           withFileContents=True)
        assert(len(list(foocs.iterNewTroveList())) == 1)

        jobList = [(x[0], (None, None), (x[1], x[2]), False)
                    for x in [ foo.getNameVersionFlavor(),
                                barRun.getNameVersionFlavor()]]

        updJob, suggMap = client.updateChangeSet(jobList,
                               fromChangesets=[foocs, barruncs],
                               resolveDeps=False, recurse=False)
        client.applyUpdate(updJob, replaceFiles=False)
        assert(os.path.exists(self.rootDir + '/foobar'))

        foocs2, barruncs2 = store.getChangeSetsForTroves(None,
                                          [foo.getNameVersionFlavor(),
                                           barRun.getNameVersionFlavor()],
                                           withFileContents=True)
        foo2, barrun2 = store.getTroves(None,
                                        [foo.getNameVersionFlavor(),
                                        barRun.getNameVersionFlavor()],
                                        withFileContents=True)
        assert(foo2.verifyDigests())
        assert(barrun2.verifyDigests())


        assert(len(list(foocs2.iterNewTroveList())) == 1)
        
        self.resetRoot()
        client = conaryclient.ConaryClient(self.cfg)
        updJob, suggMap = client.updateChangeSet(jobList,
                               fromChangesets=[foocs2, barruncs2],
                               resolveDeps=False, recurse=False)
        self.resetRepository()
        # make sure we're using just the changesets
        client.applyUpdate(updJob, replaceFiles=False)
        assert(os.path.exists(self.rootDir + '/foobar'))

    def testGetFileContents(self):
        fooRun = self.addComponent('foo:runtime', '1',
                                    [('/foo', 'hello world!\n')])
        fileList = [ (x[2], x[3]) for x in fooRun.iterFileList()]
        repos = self.openRepository()
        assert(repos.getFileContents(fileList)[0].get().read() 
                == 'hello world!\n')
        cacheDir = self.workDir + '/cache'
        util.mkdirChain(cacheDir)
        store = repocache.RepositoryCache(cacheDir)
        assert(store.getFileContents(repos, fileList)[0].get().read() 
                == 'hello world!\n')
        assert(store.getFileContents(repos, fileList)[0].get().read() 
                == 'hello world!\n')

    def testGetFileContentsPaths(self):
        fooRun = self.addComponent('foo:runtime', '1',
                                    [('/foo', 'hello world!\n')])
        fileList = [ (x[2], x[3]) for x in fooRun.iterFileList()]
        repos = self.openRepository()
        cacheDir = self.workDir + '/cache'
        util.mkdirChain(cacheDir)
        store = repocache.RepositoryCache(cacheDir)
        assert(os.path.exists(store.getFileContentsPaths(repos, fileList)[0]))

    def testReadOnly(self):
        fooRun = self.addComponent('foo:runtime', '1',
                                    [('/foo', 'hello world!\n'),
                                     ('/bar', 'goodbye world!\n')])
        fileDict = dict((x[1], (x[2], x[3])) for x in fooRun.iterFileList())
        fooFile = fileDict['/foo']
        barFile = fileDict['/bar']
        repos = self.openRepository()
        cacheDir = self.workDir + '/cache'
        util.mkdirChain(cacheDir)
        store = repocache.RepositoryCache(cacheDir)
        # store it in the cache
        assert(store.getFileContents(repos, [fooFile])[0].get().read()
                == 'hello world!\n')
        store = repocache.RepositoryCache(cacheDir, readOnly=True)
        assert(store.getFileContents(repos, [barFile])[0].get().read() 
                == 'goodbye world!\n')
        assert(len(os.listdir(cacheDir)) == 1) # for /foo

        store.getTroves(repos, [fooRun.getNameVersionFlavor()])
        assert(len(os.listdir(cacheDir)) == 1) # nothing added

        # now try adding that missing file.  Make sure we get /foo from
        # the cache by removing it from the repository.
        self.resetRepository()
        fooRun = self.addComponent('foo:runtime', '1',
                                   [('/bar', 'goodbye world!\n')])
        store = repocache.RepositoryCache(cacheDir, readOnly=False)
        assert(store.getFileContents(repos, [barFile])[0].get().read()
                == 'goodbye world!\n')
        assert(len(os.listdir(cacheDir)) == 2) # /bar is now added

        store.getTroves(repos, [fooRun.getNameVersionFlavor()])
        assert(len(os.listdir(cacheDir)) == 3) # fooRun now added

    def testResolveByGroups(self):
        cacheDir = self.workDir + '/cache'
        util.mkdirChain(cacheDir)
        store = repocache.RepositoryCache(cacheDir, readOnly=False)
        repos = self.openRepository()

        self.addComponent('bar:runtime', '1')
        self.addComponent('foo:runtime', '1')
        self.addComponent('bam:runtime', '1')
        foo = self.addCollection('foo', '1', [':runtime'])
        bar = self.addCollection('bar', '1', [':runtime'])
        bam = self.addCollection('bam', '1', [':runtime'])
        dep = deps.parseDep('trove: foo:runtime trove:bar:runtime')
        dep2 = deps.parseDep('trove: bam:runtime')
        xx = store.resolveDependenciesByGroups(repos, [foo, bar, bam],
                                               [dep, dep2])
        yy = store.resolveDependenciesByGroups(repos, [foo, bar, bam],
                                               [dep, dep2])
        assert(xx == yy)
