#
# Copyright (c) 2010 rPath, Inc.
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

"""
This plugin serves as the entry point to the basic build functionality of rMake.
"""

from rmake.build import constants as buildconst
from rmake.build import disp_handler
from rmake.build import repos
from rmake.build import server
from rmake.build import servercfg
from rmake.build import worker
from rmake.lib import pluginlib


class BuildPlugin(pluginlib.Plugin):

    cfg = None
    types = ['dispatcher', 'worker']

    def dispatcher_pre_setup(self, dispatcher):
        disp_handler.register()

        self.cfg = servercfg.rMakeConfiguration(True)
        self.server = server.BuildServer(dispatcher, self.cfg)
        dispatcher._addChild('build', self.server)

    def dispatcher_post_setup(self, dispatcher):
        if not self.cfg.isExternalRepos():
            repos.startRepository(self.cfg)
        if not self.cfg.isExternalProxy():
            repos.startProxy(self.cfg)
        self.server._post_setup()

    def worker_get_task_types(self):
        return {
                buildconst.LOAD_TASK: worker.LoadTask,
                }
