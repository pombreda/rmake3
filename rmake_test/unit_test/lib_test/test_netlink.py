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


from twisted.trial import unittest

from rmake.lib import netlink


class NetlinkTest(unittest.TestCase):

    def test_scope_filter(self):
        """Ensure that no loopback or link-scope addresses are returned."""
        rtnl = netlink.RoutingNetlink()
        addrs = rtnl.getAllAddresses(raw=True)

        # I guess this won't work on machines with no networking, but it's more
        # valuable to prove that something happened.
        assert addrs

        for family, address, prefix in addrs:
            if family == 'inet':
                # No loopback
                assert address[0] != chr(127)
            elif family == 'inet6':
                # No loopback
                assert address != '\0\0\0\0\0\0\0\1'
                # No link-local
                assert address[:2] != '\xfe\x80'
            else:
                self.fail("Invalid family " + family)
