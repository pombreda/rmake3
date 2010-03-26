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


"""
rMake messagebus client implementation for Twisted.

This includes a client protocol, factory, and XMLRPC proxy.
"""


import logging
from twisted.internet import defer
from twisted.python import failure
from twisted.words.protocols.jabber.error import StanzaError
from twisted.words.protocols.jabber.xmlstream import XMPPHandler
from rmake.lib import pubsub
from rmake.messagebus import common
from rmake.messagebus import message
from rmake.messagebus.common import toJID
from rmake.messagebus.pubsub import BusSubscriber
from wokkel import disco
from wokkel import iwokkel
from wokkel import xmppim
from wokkel.client import XMPPClient
from wokkel.ping import PingHandler
from zope.interface import implements

log = logging.getLogger(__name__)


class RmakeHandler(XMPPHandler):

    implements(iwokkel.IDisco)

    jid = None

    def connectionInitialized(self):
        self.jid = self.xmlstream.authenticator.jid
        self.xmlstream.addObserver(common.XPATH_RMAKE_MESSAGE, self.onMessage)
        self.xmlstream.addObserver(common.XPATH_RMAKE_IQ, self.onCommand)

    def onMessage(self, element):
        msg = message.Message.from_dom(element)
        if msg:
            self.parent.messageReceived(msg)

    def onCommand(self, element):
        pass

    def getDiscoInfo(self, requestor, target, nodeIdentifier=''):
        desc = self.parent.description or 'Unknown rMake component'
        ident = [disco.DiscoIdentity('automation', 'rmake', desc),
                disco.DiscoFeature(common.NS_RMAKE)]
        if self.parent.role:
            ident.append(common.getInfoForm(self.parent.role))
        return defer.succeed(ident)

    def getDiscoItems(self, requestor, target, nodeIdentifier=''):
        return defer.succeed([])


class RmakeClientHandler(RmakeHandler):

    targetRole = 'dispatcher'

    def __init__(self, targetJID):
        self.targetJID = targetJID

    def connectionInitialized(self):
        RmakeHandler.connectionInitialized(self)
        d = self.parent.checkAndSubscribe(self.targetJID, self.targetRole)
        def got_ok(dummy):
            self.parent.targetConnected()
        def got_error(failure):
            failure.trap(StanzaError)
            if failure.value.condition == 'service-unavailable':
                self.parent.targetLost(failure)
            else:
                return failure
        d.addCallbacks(got_ok, got_error)
        d.addErrback(onError)


class BusService(XMPPClient, pubsub.Publisher):

    # Service discovery info
    role = None
    description = None

    def __init__(self, reactor, jid, password, handler=None,
            other_handlers=None):
        XMPPClient.__init__(self, toJID(jid), password)
        pubsub.Publisher.__init__(self)
        self._reactor = reactor

        if not handler:
            handler = RmakeHandler()
        self._handler = handler
        self._handler.setHandlerParent(self)

        self._handlers = {
                'disco': disco.DiscoClientProtocol(),
                'disco_s': disco.DiscoHandler(),
                'ping': PingHandler(),
                'presence': PresenceProtocol(),
                }
        if other_handlers:
            self._handlers.update(other_handlers)
        for handler in self._handlers.values():
            handler.setHandlerParent(self)

    def checkAndSubscribe(self, jid, role):
        d = self._handlers['disco'].requestInfo(jid)
        def got_info(info):
            if common.NS_RMAKE not in info.features:
                raise RuntimeError("%s is not a rmake component" % jid.full())
            form = info.extensions[common.FORM_RMAKE_INFO]
            actual_role = form.fields['role'].value
            if role != actual_role:
                raise RuntimeError("%s is not a rmake %s" % (jid.full(), role))
            self._handlers['presence'].subscribe(jid.userhostJID())
        d.addCallback(got_info)
        return d

    def messageReceived(self, msg):
        if isinstance(msg, message.Event):
            try:
                msg.publish(self)
            except:
                log.exception("Error handling event %s:", msg.event)

    def onPresence(self, presence):
        pass


class BusClientService(BusService):

    """Base class for services that maintain a messagebus client."""

    resource = 'rmake'

    def __init__(self, reactor, jid, password, targetJID):
        # Connect with an anonymous JID (just the host + resource)
        self._targetJID = toJID(targetJID)
        BusService.__init__(self, reactor, jid, password,
                handler=RmakeClientHandler(self._targetJID))
        self.addRelay(self._send_events)

    def _send_events(self, event, *args, **kwargs):
        msg = message.Event(event=event, args=args, kwargs=kwargs)
        msg.send(self.xmlstream, self._targetJID)

    def onPresence(self, presence):
        if presence.sender == self._targetJID and not presence.available:
            self.targetLost(failure.Failure(
                RuntimeError("Target service became unavailable")))

    def targetConnected(self):
        pass

    def targetLost(self, failure):
        # TODO: Not a great way to handle this.
        log.error("Server went away (%s), shutting down.", self._targetJID)
        self._reactor.stop()


class PresenceProtocol(xmppim.PresenceProtocol, xmppim.RosterClientProtocol):
    """Accept all subscription requests and reply in kind."""

    def connectionInitialized(self):
        xmppim.PresenceProtocol.connectionInitialized(self)
        xmppim.RosterClientProtocol.connectionInitialized(self)
        # RFC-3921 7.3 says that we should request the roster before sending
        # initial presence or expecting any subscriptions to be in effect.
        def process_roster(roster):
            # Purge roster items with no active subscription.
            for item in roster.values():
                if not item.subscriptionTo and not item.subscriptionFrom:
                    self.removeItem(item.jid)
        d = self.getRoster()
        d.addCallback(process_roster)
        d.addBoth(lambda result: self.available())

    # Subscriptions / roster

    def subscribeReceived(self, presence):
        """If someone subscribed to us, subscribe to them."""
        self.subscribed(presence.sender)
        self.subscribe(presence.sender.userhostJID())

    def unsubscribeReceived(self, presence):
        """If someone unsubscribed us, unsubscribe them."""
        self.unsubscribed(presence.sender)
        self.unsubscribe(presence.sender.userhostJID())

    def onRosterSet(self, item):
        """If we no longer have visibility on someone, remove them entirely."""
        if not item.subscriptionTo and not item.subscriptionFrom:
            self.removeItem(item.jid)

    # Presence

    def availableReceived(self, presence):
        self.parent.onPresence(presence)

    def unavailableReceived(self, presence):
        self.parent.onPresence(presence)


def onError(failure):
    log.error("Unhandled error in callback:\n%s", failure.getTraceback())
