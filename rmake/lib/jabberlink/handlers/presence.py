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


import logging
from wokkel import xmppim

log = logging.getLogger(__name__)


class FriendlyPresenceProtocol(xmppim.PresenceProtocol,
        xmppim.RosterClientProtocol):
    """Accept all subscription requests and reply in kind."""

    def connectionInitialized(self):
        xmppim.PresenceProtocol.connectionInitialized(self)
        xmppim.RosterClientProtocol.connectionInitialized(self)
        # RFC-3921 7.3 says that we should request the roster before sending
        # initial presence or expecting any subscriptions to be in effect.
        d = self.getRoster()

        @d.addCallback
        def process_roster(roster):
            # Purge roster items with no active subscription.
            for item in roster.values():
                if not item.subscriptionTo and not item.subscriptionFrom:
                    self.removeItem(item.jid)
        d.addBoth(lambda result: self.available())

    # Subscriptions / roster

    def subscribeReceived(self, presence):
        """If someone subscribed to us, subscribe to them."""
        log.debug("Auto-subscribing to %s", presence.sender.userhost())
        self.subscribed(presence.sender)
        self.subscribe(presence.sender.userhostJID())

    def unsubscribeReceived(self, presence):
        """If someone unsubscribed us, unsubscribe them."""
        log.debug("Auto-unsubscribing to %s", presence.sender.userhost())
        self.unsubscribed(presence.sender)
        self.unsubscribe(presence.sender.userhostJID())

    def onRosterSet(self, item):
        """If we no longer have visibility on someone, remove them entirely."""
        if not item.subscriptionTo and not item.subscriptionFrom:
            self.removeItem(item.jid)

    # Presence

    def availableReceived(self, presence):
        self.parent.link.onPresence(presence)

    def unavailableReceived(self, presence):
        self.parent.link.onPresence(presence)
