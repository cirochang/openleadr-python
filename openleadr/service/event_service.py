# SPDX-License-Identifier: Apache-2.0

# Copyright 2020 Contributors to OpenLEADR

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at

#     http://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from . import service, handler, VTNService
import asyncio
from openleadr import objects, utils, enums
import logging
from datetime import datetime, timezone
from functools import partial
logger = logging.getLogger('openleadr')


@service('EiEvent')
class EventService(VTNService):

    def __init__(self, vtn_id, polling_method='internal', message_queues=None):
        super().__init__(vtn_id)
        self.polling_method = polling_method
        self.message_queues = message_queues
        self.pending_events = {}        # Holds the event callbacks
        self.running_events = {}        # Holds the event callbacks for accepted events

    @handler('oadrRequestEvent')
    async def request_event(self, payload):
        """
        The VEN requests us to send any events we have.
        """
        result = self.on_request_event(payload['ven_id'])
        if asyncio.iscoroutine(result):
            result = await result
        if result is None:
            return 'oadrDistributeEvent', {'events': []}
        if isinstance(result, dict):
            return 'oadrDistributeEvent', {'events': [result]}
        if isinstance(result, objects.Event):
            return 'oadrDistributeEvent', {'events': [result]}
        if isinstance(result, list):
            return 'oadrDistributeEvent', {'events': result}
        else:
            raise TypeError("Event handler should return None, a dict or a list")

    def on_request_event(self, ven_id):
        """
        Placeholder for the on_request_event handler.
        """
        logger.warning("You should implement and register your own on_request_event handler "
                       "that returns the next event for a VEN. This handler will receive a "
                       "ven_id as its only argument, and should return None (if no events are "
                       "available), a single Event, or a list of Events.")
        return None

    @handler('oadrCreatedEvent')
    async def created_event(self, payload):
        """
        The VEN informs us that they created an EiEvent.
        """
        loop = asyncio.get_event_loop()
        ven_id = payload['ven_id']
        if self.polling_method == 'internal':
            for event_response in payload['event_responses']:
                event_id = event_response['event_id']
                opt_type = event_response['opt_type']
                if event_response['event_id'] in self.pending_events:
                    event, callback = self.pending_events.pop(event_id)
                    result = callback(ven_id=ven_id, event_id=event_id, opt_type=opt_type)
                    if asyncio.iscoroutine(result):
                        result = await result
                    if opt_type == 'optIn':
                        self.running_events[event_id] = (event, callback)
                        now = datetime.now(timezone.utc)
                        active_period = event.active_period
                        # Schedule status update to 'near' if applicable
                        if active_period.ramp_up_period is not None and event.event_descriptor.event_status == 'far':
                            ramp_up_start_delay = (active_period.dtstart - active_period.ramp_up_period) - now
                            update_coro = partial(self._update_event_status, ven_id, event, 'near')
                            loop.create_task(utils.delayed_call(func=update_coro, delay=ramp_up_start_delay))
                        # Schedule status update to 'active'
                        if event.event_descriptor.event_status in ('near', 'far'):
                            active_start_delay = active_period.dtstart - now
                            update_coro = partial(self._update_event_status, ven_id, event, 'active')
                            loop.create_task(utils.delayed_call(func=update_coro, delay=active_start_delay))
                        # Schedule status update to 'completed'
                        if event.event_descriptor.event_status in ('near', 'far', 'active'):
                            active_end_delay = active_period.dtstart + active_period.duration - now
                            update_coro = partial(self._update_event_status, ven_id, event, 'completed')
                            loop.create_task(utils.delayed_call(func=update_coro, delay=active_end_delay))
                elif event_response['event_id'] in self.running_events:
                    event, callback = self.running_events.pop(event_id)
                    result = callback(ven_id=ven_id, event_id=event_id, opt_type=opt_type)
                    if asyncio.iscoroutine(result):
                        result = await result
        else:
            result = self.on_created_event(ven_id=ven_id, event_id=event_id, opt_type=opt_type)
            if asyncio.iscoroutine(result):
                result = await(result)
        return 'oadrResponse', {}

    def on_created_event(self, ven_id, event_id, opt_type):
        """
        Placeholder for the on_created_event handler.
        """
        logger.warning("You should implement and register you own on_created_event handler "
                       "to receive the opt status for an Event that you sent to the VEN. This "
                       "handler will receive a ven_id, event_id and opt_status. "
                       "You don't need to return anything from this handler.")
        return None

    def _update_event_status(self, ven_id, event, event_status):
        """
        Update the event to the given Status.
        """
        event.event_descriptor.event_status = event_status
        if event_status == enums.EVENT_STATUS.CANCELLED:
            event.event_descriptor.modification_number += 1
        self.message_queues[ven_id].put_nowait(event)
