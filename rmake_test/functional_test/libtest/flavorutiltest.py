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

#conary
from conary.deps import arch
from conary.deps.deps import parseFlavor,ThawDependencySet, parseDep, Dependency
from conary.deps.deps import *
from rmake.lib.flavorutil import *

#test
from rmake_test import rmakehelp

class FlavorUtilTest(rmakehelp.RmakeHelper):

    def testGetTargetArch(self):
        targetArch = parseFlavor('is:x86(i486,i586)')
        currentArch = parseFlavor('is:x86_64(cmov)')
        assert(getTargetArch(targetArch, currentArch) == (True, 'x86'))

        targetArch = deps.DependencySet()
        # mock the currentArch so this test passes on a x86_64 host
        fakeArch = [
            [ Dependency('x86', flags={'i586': FLAG_SENSE_PREFERRED,
                                       'i486': FLAG_SENSE_PREFERRED,
                                       'i686': FLAG_SENSE_PREFERRED}) ] ]
        self.mock(arch, 'currentArch', fakeArch)
        targetArch.addDep(deps.InstructionSetDependency, arch.currentArch[0][0])
        self.assertEquals(getTargetArch(targetArch), (False, None))

    def testGetSysRootPath(self):
        def _test(flavor, path):
            self.assertEquals(getSysRootPath(parseFlavor(flavor)), path)
        _test('is: x86 target: x86_64',
              '/opt/cross-target-x86_64-unknown-linux/sys-root')
        _test('is: x86 target: x86',
              '/opt/cross-target-i386-unknown-linux/sys-root')
        _test('is: x86', '/opt/cross-target-i386-unknown-linux/sys-root')

    def testGetSysRootFlavor(self):
        def _test(flavor, flavor2):
            self.assertEquals(str(getSysRootFlavor(parseFlavor(flavor))), 
                              flavor2)
        _test('ssl is:x86 target:x86_64', 'ssl is: x86_64')
        _test('cross, ssl is:x86 target:x86_64', 'cross,ssl is: x86_64')

    def testGetBuiltFlavor(self):
        def _test(flavor, flavor2):
            self.assertEquals(str(getBuiltFlavor(parseFlavor(flavor))), 
                                  flavor2)
        _test('ssl is: x86', 'ssl is: x86')
        _test('ssl is: x86 target: x86_64', 'ssl is: x86_64')
        _test('~cross,ssl is: x86 target: x86_64', '~cross,ssl is: x86 target: x86_64')

    def testHasTarget(self):
        def _test(flavor, val):
            self.assertEquals(hasTarget(parseFlavor(flavor)), val)
        _test('is:x86', False)
        _test('is:x86 target: x86', True)

    def testRemoveFileDeps(self):
        def _test(flavor, flavor2):
            d = parseDep(flavor); assert(d is not None)
            self.assertEquals(removeFileDeps(parseDep(flavor)), 
                              parseDep(flavor2))
        _test('file: /tmp', '')
        _test('file: /tmp soname: ELF32/foo.so(SysV)',
              'soname: ELF32/foo.so(SysV)')

    def testRemoveTarget(self):
        def _test(flavor, flavor2):
            d = parseFlavor(flavor); assert(d is not None)
            self.assertEquals(removeTargetFlavor(parseFlavor(flavor)), 
                              parseFlavor(flavor2))
        _test('target: x86', '')
        _test('is: x86 target: x86(i486)', 'is: x86')

    def testIsCrossCompiler(self):
        def _test(flavor, val):
            self.assertEquals(isCrossCompiler(parseFlavor(flavor)), val)
        _test('is: x86 target: x86_64', False)
        _test('~cross is: x86 target: x86_64', True)
        _test('~!cross is: x86 target: x86_64', False)

    def testGetCrossCompile(self):
        def _test(flavor, targetFlavor, isCrossTool):
            x = getCrossCompile(parseFlavor(flavor))[1:]
            self.assertEquals(str(x[0]), targetFlavor)
            self.assertEquals(x[1], isCrossTool)
        _test('is: x86 target: x86_64', 'is: x86_64', False)
        _test('~!cross is: x86 target: x86_64', 'is: x86_64', False)
        _test('~cross is: x86 target: x86_64', 'is: x86_64', True)
