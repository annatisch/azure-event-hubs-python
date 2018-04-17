# --------------------------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License. See License.txt in the project root for license information.
# --------------------------------------------------------------------------------------------

"""
Internal implementations of protocol handlers. It should be implementing send/recv over AMQP
for general purposes. Keep any service/broker specifics out of this file.

"""

# pylint: disable=line-too-long
# pylint: disable=C0111
# pylint: disable=W0613
# pylint: disable=W0702

import logging
import time
import os
from proton import PN_PYREF, DELEGATED, generate_uuid
from proton import Delivery, EventBase, Condition
from proton.handlers import Handler, EndpointStateHandler
from proton.handlers import IncomingMessageHandler
from proton.handlers import CFlowController, OutgoingMessageHandler
from proton.reactor import EventType, EventInjector

try:
    import Queue
except:
    import queue as Queue

log = logging.getLogger("eventhubs")


class ClientHandler(Handler):
    def __init__(self, prefix, client):
        super(ClientHandler, self).__init__()
        self.name = "%s-%s" % (prefix, str(generate_uuid())[:8])
        self.client = client
        self.link = None
        self.iteration = 0
        self.fatal_conditions = ["amqp:unauthorized-access", "amqp:not-found"]

    def start(self):
        self.iteration += 1
        self.on_start()

    def stop(self, condition):
        self.on_stop(condition)
        if self.link:
            self.link.close()
            self.link.free()
            self.link = None

    def _get_link_name(self):
        return "%s:%d" % (self.name, self.iteration)

    def on_start(self):
        assert False, "Subclass must override this!"

    def on_stop(self, condition):
        pass

    def on_link_closed(self, condition):
        pass

    def on_link_remote_close(self, event):
        link = event.link
        if EndpointStateHandler.is_local_closed(link):
            return DELEGATED
        link.close()
        link.free()
        condition = link.remote_condition
        connection = event.connection
        if condition:
            log.error("%s: link detached name:%s ref:%s %s:%s",
                          connection.container,
                          link.name,
                          condition.name,
                          connection.remote_container,
                          condition.description)
        else:
            log.error("%s: link detached name=%s ref:%s",
                          connection.container,
                          link.name,
                          connection.remote_container)
        self.on_link_closed(condition)
        if condition and condition.name in self.fatal_conditions:
            connection.close()
        elif link == self.link:
            self.link = None
            event.reactor.schedule(1.0, self)

    def on_timer_task(self, event):
        if self.link is None and not self.client.stopped:
            self.start()

class ReceiverHandler(ClientHandler):
    def __init__(self, client, receiver, source, selector):
        super(ReceiverHandler, self).__init__("recv", client)
        self.receiver = receiver
        self.source = source
        self.selector = selector
        self.handlers = []
        if receiver.prefetch:
            self.handlers.append(CFlowController(receiver.prefetch))
        self.handlers.append(IncomingMessageHandler(True, self))

    def on_start(self):
        self.link = self.client.container.create_receiver(
            self.client.connection,
            self.source,
            name=self._get_link_name(),
            handler=self,
            options=self.receiver.selector(self.selector))
        self.receiver.on_start(self.link, self.iteration)

    def on_stop(self, condition):
        self.receiver.on_stop(self.client.stopped)

    def on_message(self, event):
        self.receiver.on_message(event)

    def on_link_local_open(self, event):
        log.info("%s: link local open. name=%s source=%s offset=%s",
                     event.connection.container,
                     event.link.name,
                     self.source,
                     self.selector.filter_set["selector"].value)

    def on_link_remote_open(self, event):
        log.info("%s: link remote open. name=%s source=%s",
                     event.connection.container,
                     event.link.name,
                     self.source)

class SenderHandler(ClientHandler):
    TIMEOUT = 60.0

    class DeliveryEvent(InjectorEvent):
        def __init__(self, handler, message, callback, state):
            super(SenderHandler.DeliveryEvent, self).__init__(InjectorEvent.SEND, subject=handler)
            self.message = message
            self.callback = callback
            self.state = state
            self.start = time.time()

        def elapsed(self):
            return time.time() - self.start

        def complete(self, outcome, condition):
            self.callback(self.state, outcome, condition)

    class DeliveryTracker(object):
        def __init__(self, handler):
            self.handler = handler
            self.task = None

        def track(self):
            if self.task is None:
                self.task = self.handler.client.container.schedule(SenderHandler.TIMEOUT, self)

        def stop(self):
            if self.task:
                self.task.cancel()
                self.task = None

        def on_timer_task(self, event):
            log.debug("delivery tracker timer event")
            self.task = None
            self.handler.check_timeout()

    def __init__(self, client, sender, target):
        super(SenderHandler, self).__init__("send", client)
        self.sender = sender
        self.target = target
        self.handlers = [OutgoingMessageHandler(True, self)]
        self.queue = Queue.Queue()
        self.deliveries = {}
        self.tracker = SenderHandler.DeliveryTracker(self)

    def send(self, message, callback, state):
        event = SenderHandler.DeliveryEvent(self, message, callback, state)
        self.queue.put(event)
        self.client.injector.trigger(event)

    def on_start(self):
        self.link = self.client.container.create_sender(
            self.client.connection,
            self.target,
            name=self._get_link_name(),
            handler=self)
        self.sender.on_start(self.link, self.iteration)

    def on_stop(self, condition):
        self.on_link_closed(condition)

    def on_link_closed(self, condition):
        for dlv in self.deliveries:
            self.deliveries[dlv].complete(Delivery.RELEASED, condition)
        self.deliveries.clear()
        self.tracker.stop()

    def on_link_local_open(self, event):
        log.info("%s: link local open. name=%s target=%s",
                 event.connection.container,
                 event.link.name,
                 self.target)

    def on_link_remote_open(self, event):
        log.info("%s: link remote open. name=%s, credit=%d, queue=%d, map=%d",
                 event.connection.container,
                 event.link.name,
                 event.link.credit,
                 self.queue.qsize(),
                 len(self.deliveries))

    def on_sendable(self, event):
        while self.link and self.link.credit and not self.queue.empty():
            dlv_event = self.queue.get(False)
            delivery = dlv_event.message.send(self.link)
            self.deliveries[delivery] = dlv_event
            log.debug("%s: send message %s", self.client.container_id, delivery.tag)
        if self.deliveries:
            self.tracker.track()

    def on_delivery(self, event):
        dlv = event.delivery
        log.debug("%s: on_delivery %s", self.client.container_id, dlv.tag)
        if dlv.updated:
            dlv_event = self.deliveries.pop(dlv, None)
            if dlv_event:
                dlv_event.complete(dlv.remote_state, dlv.remote.condition)
            dlv.settle()

    def check_timeout(self):
        expired = []
        for dlv in self.deliveries:
            if self.deliveries[dlv].elapsed() >= SenderHandler.TIMEOUT:
                expired.append(dlv)
        for dlv in expired:
            dlv_event = self.deliveries.pop(dlv)
            dlv.update(Delivery.RELEASED)
            dlv.settle()
            dlv_event.complete(Delivery.RELEASED, Condition("timeout",\
                description="Send not complete after %d seconds. ref %s" % (SenderHandler.TIMEOUT, self.client.remote_container)))
        self.on_sendable(None)
