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


import time

from rmake_test import rmakehelp

from conary.lib import util

from rmake.build import buildcfg
from rmake.build import buildtrove



class ServerTest(rmakehelp.RmakeHelper):

    def testFailingJobs(self):
        # when the rmake server is restarted, it automatically
        # resets all running jobs to "Failed - Server Stopped".
        # We're testing that behavior here.
        trv = self.addComponent('sleep:source', '1',
                                [('sleep.recipe', rmakehelp.sleepRecipe)])
        self.openRmakeRepository()
        client = self.startRmakeServer()
        helper = self.getRmakeHelper(client.uri)
        jobId, txt = self.captureOutput(helper.buildTroves,
                                       [trv.getNameVersionFlavor()], buildConfig=self.buildCfg)
        job = helper.getJob(jobId)
        found = False
        while not [ x for x in job.iterTrovesByState(
                                        buildtrove.TROVE_STATE_BUILDING) ]:
            time.sleep(.2)
            job = helper.getJob(jobId)
        # FIME: This test exhibits a race condition - in the _shutdown portion,
        # if the # stopAllJobs command completes before the killAllPids 
        # can kill the forked job guy, then we're set.
        # Otherwise, the job won't change state.
        self.stopRmakeServer()
        db = self.openRmakeDatabase()
        job = db.getJob(jobId)
        if job.isBuilding():
            raise testsuite.SkipTestException('Race condition in test')
        self.assertEquals(job.getStateName(), 'Failed')
        for buildTrove in job.iterTroves():
            assert(buildTrove.isFailed())

    def testUUID(self):
        self.openRmakeRepository()
        client = self.startRmakeServer()
        db = self.openRmakeDatabase()
        trv, cs = self.Component('foo:source')

        uuid = self.genUUID('foo')
        job = self.newJob(trv, uuid=uuid)
        assert(client.getJob(job.uuid).uuid == job.uuid)

    def testJobOwner(self):
        # RMK-652
        self.openRmakeRepository()
        client = self.startRmakeServer()
        db = self.openRmakeDatabase()
        trv, cs = self.Component('foo:source')

        uuid = self.genUUID('foo')
        job = self.newJob(trv, uuid=uuid, owner='john_doe')
        owner = db.getJob(job.jobId).owner
        self.failUnlessEqual(owner, 'john_doe')

    def testGetJobSanitizes(self):
        self.openRmakeRepository()
        client = self.startRmakeServer(multinode=True)
        db = self.openRmakeDatabase()
        trv, cs = self.Component('foo:source')
        buildCfg = buildcfg.BuildConfiguration(False)
        buildCfg.configLine('rmakeUser test foo')
        oldCfg = self.buildCfg
        self.buildCfg = buildCfg
        buildCfg.user = oldCfg.user
        job = self.newJob(trv)
        assert(db.getJob(job.jobId, withConfigs=True).getMainConfig().user)
        assert(db.getJob(job.jobId, withConfigs=True).getMainConfig().rmakeUser)
        cfg = client.getJob(job.jobId, withConfigs=True).getMainConfig()
        assert(not cfg.user)
        assert(not cfg.rmakeUser)

    def testListChroots(self):
        self.openRmakeRepository()
        client = self.startRmakeServer()
        trv, cs = self.Component('foo:source')
        job = self.newJob(trv)
        trv = job.iterTroves().next()
        trv.creatingChroot('_local_', 'foo')
        db = self.openRmakeDatabase()
        assert([ x.path for x in db.listChroots()] == ['foo'])
        util.mkdirChain(self.rmakeCfg.buildDir + '/chroots/foo')
        assert([ x.path for x in client.listChroots()] == ['foo'])
        util.rmtree(self.rmakeCfg.buildDir + '/chroots/foo')
        assert([ x.path for x in client.listChroots()] == [])
