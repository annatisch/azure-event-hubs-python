# --------------------------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License. See License.txt in the project root for license information.
# -----------------------------------------------------------------------------------

from abc import ABC, abstractmethod

class AbstractEventProcessor(ABC):
    """
    Abstract that must be extended by event processor classes.
    """
    def __init__(self, params=None):
        pass

    @abstractmethod
    async def open_async(self, context):
        """
        Called by processor host to initialize the event processor.
        """
        pass

    @abstractmethod
    async def close_async(self, context, reason):
        """
        Called by processor host to indicate that the event processor is being stopped.
        (Params) Context:Information about the partition
        """
        pass

    @abstractmethod
    async def process_events_async(self, context, messages):
        """
        Called by the processor host when a batch of events has arrived.
        This is where the real work of the event processor is done.
        (Params) Context: Information about the partition, Messages: The events to be processed.
        """
        pass

    @abstractmethod
    async def process_error_async(self, context, error):
        """
        Called when the underlying client experiences an error while receiving.
        EventProcessorHost will take care of recovering from the error and
        continuing to pump messages,so no action is required from
        (Params) Context: Information about the partition, Error: The error that occured.
        """
        pass
