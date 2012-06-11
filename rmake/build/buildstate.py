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
Describes the basic state of a build.
"""

from rmake.build import buildtrove


class AbstractBuildState(object):

    def __init__(self, sourceTroves):
        self.troves = []
        self.trovesByNVF = {}

        self.states = dict((x, set()) for x in buildtrove.TroveState.by_value)
        self.statesByTrove = {}
        self.addTroves(sourceTroves)

    def addTroves(self, sourceTroves):
        self.troves.extend(sourceTroves)
        for sourceTrove in sourceTroves:
            self.trovesByNVF[sourceTrove.getNameVersionFlavor(True)] = sourceTrove
            self.states[sourceTrove.state].add(sourceTrove)
            self.statesByTrove[sourceTrove.getNameVersionFlavor(True)] = sourceTrove.state

    def getTrove(self, name, version, flavor, context=''):
        return self.trovesByNVF[name, version, flavor, context]

    def _setState(self, sourceTrove, newState):
        nvf = sourceTrove.getNameVersionFlavor(True)
        oldState = self.statesByTrove[nvf]
        self.states[oldState].discard(sourceTrove)
        self.statesByTrove[nvf] = newState
        self.states[newState].add(sourceTrove)

    def getBuildableTroves(self):
        return self.states[buildtrove.TroveState.BUILDABLE]

    def getBuildingTroves(self):
        return self.states[buildtrove.TroveState.BUILDING]

    def getBuiltTroves(self):
        return self.states[buildtrove.TroveState.BUILT]

    def getDuplicateTroves(self):
        return self.states[buildtrove.TroveState.DUPLICATE]

    def getPreparedTroves(self):
        return self.states[buildtrove.TroveState.PREPARED]

    def getFailedTroves(self):
        return self.states[buildtrove.TroveState.FAILED] | self.states[buildtrove.TroveState.UNBUILDABLE]

    def jobFinished(self):
        return set(self.troves) == (self.getBuiltTroves()
                                    | self.getDuplicateTroves()
                                    | self.getPreparedTroves()
                                    | self.getFailedTroves())
                                            
    def jobPassed(self):
        return (set(self.troves) == (set(self.getBuiltTroves())
                                     | set(self.getDuplicateTroves()
                                     | set(self.getPreparedTroves()))))

    def isUnbuilt(self, trove):
        return (trove in self.states[buildtrove.TroveState.INIT]
                or trove in self.states[buildtrove.TroveState.BUILDABLE]
                or trove in self.states[buildtrove.TroveState.BUILDING]
                or trove in self.states[buildtrove.TroveState.PREPARING]
                or trove in self.states[buildtrove.TroveState.RESOLVING]
                or trove in self.states[buildtrove.TroveState.PREBUILT]
                or trove in self.states[buildtrove.TroveState.WAITING])

    def isBuilt(self, trove):
        return trove in self.states[buildtrove.TroveState.BUILT]
