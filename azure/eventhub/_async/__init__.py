# --------------------------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License. See License.txt in the project root for license information.
# --------------------------------------------------------------------------------------------

import logging
import asyncio
import time
import datetime

from uamqp import authentication, constants, types, errors
from uamqp import (
    Message,
    Source,
    ConnectionAsync,
    AMQPClientAsync,
    SendClientAsync,
    ReceiveClientAsync)

from azure.eventhub import (
    Sender,
    Receiver,
    EventHubClient,
    EventData,
    EventHubError)

from .sender_async import AsyncSender
from .receiver_async import AsyncReceiver


log = logging.getLogger(__name__)


class EventHubClientAsync(EventHubClient):
    """
    The EventHubClient class defines a high level interface for asynchronously
    sending events to and receiving events from the Azure Event Hubs service.
    """

    def _create_auth(self, auth_uri, username, password):  # pylint: disable=no-self-use
        """
        Create an ~uamqp.authentication.cbs_auth_async.SASTokenAuthAsync instance to authenticate
        the session.

        :param auth_uri: The URI to authenticate against.
        :type auth_uri: str
        :param username: The name of the shared access policy.
        :type username: str
        :param password: The shared access key.
        :type password: str
        """
        if "@sas.root" in username:
            return authentication.SASLPlain(self.address.hostname, username, password)
        return authentication.SASTokenAsync.from_shared_access_key(auth_uri, username, password, timeout=60)

    def _create_connection_async(self):
        """
        Create a new ~uamqp._async.connection_async.ConnectionAsync instance that will be shared between all
        AsyncSender/AsyncReceiver clients.
        """
        if not self.connection:
            log.info("{}: Creating connection with address={}".format(
                self.container_id, self.address.geturl()))
            self.connection = ConnectionAsync(
                self.address.hostname,
                self.auth,
                container_id=self.container_id,
                properties=self._create_properties(),
                debug=self.debug)

    async def _close_connection_async(self):
        """
        Close and destroy the connection async.
        """
        if self.connection:
            await self.connection.destroy_async()
            self.connection = None

    async def _close_clients_async(self):
        """
        Close all open AsyncSender/AsyncReceiver clients.
        """
        await asyncio.gather(*[c.close_async() for c in self.clients])

    async def _wait_for_client(self, client):
        try:
            while client.get_handler_state().value == 2:
                await self.connection.work_async()
        except Exception as exp:  # pylint: disable=broad-except
            await client.close_async(exception=exp)

    async def _start_client_async(self, client):
        try:
            await client.open_async(self.connection)
            started = await client.has_started()
            while not started:
                await self.connection.work_async()
                started = await client.has_started()
        except Exception as exp:  # pylint: disable=broad-except
            await client.close_async(exception=exp)

    async def _handle_redirect(self, redirects):
        if len(redirects) != len(self.clients):
            not_redirected = [c for c in self.clients if not c.redirected]
            _, timeout = await asyncio.wait([self._wait_for_client(c) for c in not_redirected], timeout=5)
            if timeout:
                raise EventHubError("Some clients are attempting to redirect the connection.")
            redirects = [c.redirected for c in self.clients if c.redirected]
        if not all(r.hostname == redirects[0].hostname for r in redirects):
            raise EventHubError("Multiple clients attempting to redirect to different hosts.")
        self.auth = self._create_auth(redirects[0].address.decode('utf-8'), **self._auth_config)
        #port = str(redirects[0].port).encode('UTF-8')
        #path = self.address.path.encode('UTF-8')
        #self.mgmt_node = b"pyot/$management" #+ redirects[0].hostname  b"amqps://pyot.azure-devices.net"  + b":" + port + 
        #print("setting mgmt node", self.mgmt_node)
        await self.connection.redirect_async(redirects[0], self.auth)
        await asyncio.gather(*[c.open_async(self.connection) for c in self.clients])

    async def run_async(self):
        """
        Run the EventHubClient asynchronously.
        Opens the connection and starts running all AsyncSender/AsyncReceiver clients.
        Returns a list of the start up results. For a succcesful client start the
        result will be `None`, otherwise the exception raised.
        If all clients failed to start, then run will fail, shut down the connection
        and raise an exception.
        If at least one client starts up successfully the run command will succeed.

        :rtype: list[~azure.eventhub.common.EventHubError]
        """
        log.info("{}: Starting {} clients".format(self.container_id, len(self.clients)))
        self._create_connection_async()
        tasks = [self._start_client_async(c) for c in self.clients]
        try:
            await asyncio.gather(*tasks)
            redirects = [c.redirected for c in self.clients if c.redirected]
            failed = [c.error for c in self.clients if c.error]
            if failed and len(failed) == len(self.clients):
                log.warning("{}: All clients failed to start.".format(self.container_id))
                raise failed[0]
            elif failed:
                log.warning("{}: {} clients failed to start.".format(self.container_id, len(failed)))
            elif redirects:
                await self._handle_redirect(redirects)
        except EventHubError:
            await self.stop_async()
            raise
        except Exception as exp:
            await self.stop_async()
            raise EventHubError(str(exp))
        return failed

    async def stop_async(self):
        """
        Stop the EventHubClient and all its Sender/Receiver clients.
        """
        log.info("{}: Stopping {} clients".format(self.container_id, len(self.clients)))
        self.stopped = True
        await self._close_clients_async()
        await self._close_connection_async()

    async def get_eventhub_info_async(self):
        """
        Get details on the specified EventHub async.

        :rtype: dict
        """
        self._create_connection_async()
        eh_name = self.address.path.lstrip('/')
        target = "amqps://{}/{}".format(self.address.hostname, eh_name)
        try:
            mgmt_client = AMQPClientAsync(target, auth=self.auth, debug=self.debug)
            await mgmt_client.open_async(connection=self.connection)
            mgmt_msg = Message(application_properties={'name': eh_name})
            response = await mgmt_client.mgmt_request_async(
                mgmt_msg,
                constants.READ_OPERATION,
                op_type=b'com.microsoft:eventhub',
                node=self.mgmt_node,
                status_code_field=b'status-code',
                description_fields=b'status-description')
            eh_info = response.get_data()
            output = {}
            if eh_info:
                output['name'] = eh_info[b'name'].decode('utf-8')
                output['type'] = eh_info[b'type'].decode('utf-8')
                output['created_at'] = datetime.datetime.fromtimestamp(float(eh_info[b'created_at'])/1000)
                output['partition_count'] = eh_info[b'partition_count']
                output['partition_ids'] = [p.decode('utf-8') for p in eh_info[b'partition_ids']]
            return output
        finally:
            await mgmt_client.close_async()

    def add_async_receiver(self, consumer_group, partition, offset=None, prefetch=300, operation=None, loop=None):
        """
        Add an async receiver to the client for a particular consumer group and partition.

        :param consumer_group: The name of the consumer group.
        :type consumer_group: str
        :param partition: The ID of the partition.
        :type partition: str
        :param offset: The offset from which to start receiving.
        :type offset: ~azure.eventhub.common.Offset
        :param prefetch: The message prefetch count of the receiver. Default is 300.
        :type prefetch: int
        :operation: An optional operation to be appended to the hostname in the source URL.
         The value must start with `/` character.
        :type operation: str
        :rtype: ~azure.eventhub._async.receiver_async.ReceiverAsync
        """
        path = self.address.path + operation if operation else self.address.path
        source_url = "amqps://{}{}/ConsumerGroups/{}/Partitions/{}".format(
            self.address.hostname, path, consumer_group, partition)
        source = Source(source_url)
        if offset is not None:
            source.set_filter(offset.selector())
        handler = AsyncReceiver(self, source, prefetch=prefetch, loop=loop)
        self.clients.append(handler)
        return handler

    def add_async_epoch_receiver(self, consumer_group, partition, epoch, prefetch=300, operation=None, loop=None):
        """
        Add an async receiver to the client with an epoch value. Only a single epoch receiver
        can connect to a partition at any given time - additional epoch receivers must have
        a higher epoch value or they will be rejected. If a 2nd epoch receiver has
        connected, the first will be closed.

        :param consumer_group: The name of the consumer group.
        :type consumer_group: str
        :param partition: The ID of the partition.
        :type partition: str
        :param epoch: The epoch value for the receiver.
        :type epoch: int
        :param prefetch: The message prefetch count of the receiver. Default is 300.
        :type prefetch: int
        :operation: An optional operation to be appended to the hostname in the source URL.
         The value must start with `/` character.
        :type operation: str
        :rtype: ~azure.eventhub._async.receiver_async.ReceiverAsync
        """
        path = self.address.path + operation if operation else self.address.path
        source_url = "amqps://{}{}/ConsumerGroups/{}/Partitions/{}".format(
            self.address.hostname, path, consumer_group, partition)
        handler = AsyncReceiver(self, source_url, prefetch=prefetch, epoch=epoch, loop=loop)
        self.clients.append(handler)
        return handler

    def add_async_sender(self, partition=None, operation=None, loop=None):
        """
        Add an async sender to the client to send ~azure.eventhub.common.EventData object
        to an EventHub.

        :param partition: Optionally specify a particular partition to send to.
         If omitted, the events will be distributed to available partitions via
         round-robin.
        :type partition: str
        :operation: An optional operation to be appended to the hostname in the target URL.
         The value must start with `/` character.
        :type operation: str
        :rtype: ~azure.eventhub._async.sender_async.SenderAsync
        """
        target = "amqps://{}{}".format(self.address.hostname, self.address.path)
        if operation:
            target = target + operation
        handler = AsyncSender(self, target, partition=partition, loop=loop)
        self.clients.append(handler)
        return handler
