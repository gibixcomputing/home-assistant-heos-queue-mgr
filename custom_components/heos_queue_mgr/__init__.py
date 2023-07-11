"""Main module for HEOS queue management service calls."""

from __future__ import annotations

import logging
from collections.abc import Callable, Coroutine
from typing import Any, Literal, Protocol, TypedDict, cast

import voluptuous as vol
from pyheos import HeosError
from pyheos.connection import HeosConnection
from pyheos.player import HeosPlayer

from homeassistant.components.heos.media_player import HeosMediaPlayer
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_platform
from homeassistant.helpers.entity import Entity
from homeassistant.util.json import JsonValueType

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

ConnectionCallback = Callable[[str, int, HeosConnection], Coroutine[Any, Any, None]]


async def async_setup_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Set up the service calls."""
    del config_entry

    _LOGGER.debug("registering HEOS queue services")

    def register(serviceType: type[Service]) -> None:
        service = serviceType(hass)

        hass.services.async_register(
            DOMAIN,
            service.name,
            service.callback,
            service.schema,
            service.supports_response,
        )

        _LOGGER.debug("registered %s", service.name)

    register(ClearQueue)
    register(ClearQueueExceptNowPlaying)
    register(GetQueue)
    register(RemoveFromQueue)

    return True


class QueuedItemDict(TypedDict):
    """Simple dict representing the minimum queued item."""

    qid: int


class NowPlayingDict(TypedDict):
    """Simple dict representing the now playing response."""

    type: Literal["song"] | Literal["station"]
    qid: int


class Service(Protocol):
    """Base service protocol describing functions for a service call."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize a new instance."""

    @property
    def name(self) -> str:
        """Get the name of the service."""
        ...  # pylint: disable=unnecessary-ellipsis

    @property
    def schema(self) -> vol.Schema:
        """Get the schema to validate parameters for the service call."""
        ...  # pylint: disable=unnecessary-ellipsis

    @property
    def supports_response(self) -> SupportsResponse:
        """Get the service response availability."""
        ...  # pylint: disable=unnecessary-ellipsis

    @callback
    async def callback(self, service_call: ServiceCall) -> ServiceResponse:
        """Handle the service call."""


class ClearQueue(Service):
    """Handle the 'clear_queue' service call."""

    hass: HomeAssistant

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    @property
    def name(self) -> str:
        return "clear_queue"

    @property
    def schema(self) -> vol.Schema:
        return cv.make_entity_service_schema({})

    @property
    def supports_response(self) -> SupportsResponse:
        return SupportsResponse.NONE

    @callback
    async def callback(self, service_call: ServiceCall) -> ServiceResponse:
        _LOGGER.debug("%s service call: %s", self.name, service_call)

        async def handler(entity_id: str, pid: int, connection: HeosConnection) -> None:
            try:
                await connection.command("player/clear_queue", {"pid": pid})
            except HeosError as e:
                _LOGGER.exception(
                    "%s: error when clearing queue for %s",
                    self.name,
                    entity_id,
                    exc_info=e,
                )

        await async_exec_command(self.hass, service_call, handler)
        return None


class ClearQueueExceptNowPlaying(Service):
    """Handle the 'clear_queue_except_now_playing' service call."""

    hass: HomeAssistant

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    @property
    def name(self) -> str:
        return "clear_queue_except_now_playing"

    @property
    def schema(self) -> vol.Schema:
        return cv.make_entity_service_schema({})

    @property
    def supports_response(self) -> SupportsResponse:
        return SupportsResponse.NONE

    @callback
    async def callback(self, service_call: ServiceCall) -> ServiceResponse:
        _LOGGER.debug("%s service call: %s", self.name, service_call)

        await async_exec_command(self.hass, service_call, self.handler)
        return None

    async def handler(
        self, entity_id: str, pid: int, connection: HeosConnection
    ) -> None:
        try:
            response = await connection.command("player/get_queue", {"pid": pid})
            if not response.result:
                _LOGGER.warning(
                    "%s: player/get_queue failed for player %s",
                    self.name,
                    entity_id,
                )
                return
        except HeosError as e:
            _LOGGER.exception(
                "%s: error with player/get_queue for %s",
                self.name,
                entity_id,
                exc_info=e,
            )
            return

        queued_entries = cast(list[QueuedItemDict], response.payload)
        if len(queued_entries) <= 1:
            _LOGGER.debug(
                "%s: queue is sized correctly for player %s",
                self.name,
                entity_id,
            )
            # nothing to do, so skip on out.
            return

        try:
            response = await connection.command(
                "player/get_now_playing_media", {"pid": pid}
            )
            if not response.result:
                _LOGGER.warning(
                    "%s: player/get_now_playing_media failed for player %s",
                    self.name,
                    entity_id,
                )
                return

            now_playing = cast(NowPlayingDict, response.payload)
        except HeosError as e:
            _LOGGER.exception(
                "%s: error with player/get_now_playing_media for player %s",
                self.name,
                entity_id,
                exc_info=e,
            )
            return

        playing_qid = now_playing["qid"]
        _LOGGER.debug(
            "%s: now playing queue id %d for player %s",
            self.name,
            playing_qid,
            entity_id,
        )

        # remove queued items from the player except for the now playing one, the
        # underlying call allows us to batch them together so we can do this all
        # in one shot.
        qids_to_remove = ",".join(
            map(
                lambda i: str(i["qid"]),
                filter(lambda i: i["qid"] != playing_qid, queued_entries),
            )
        )
        _LOGGER.debug(
            "%s: removing queue items %s from player %s",
            self.name,
            qids_to_remove,
            entity_id,
        )

        try:
            await connection.command(
                "player/remove_from_queue", {"pid": pid, "qid": qids_to_remove}
            )
        except HeosError:
            # a failure to remove from the queue here can be ignored
            pass

        try:
            response = await connection.command(
                "player/get_now_playing_media", {"pid": pid}
            )
            if not response.result:
                _LOGGER.warning(
                    "%s: unable to get now playing after queue maintenance for player %s",
                    self.name,
                    entity_id,
                )
                return

            now_playing = cast(NowPlayingDict, response.payload)
        except HeosError as e:
            _LOGGER.exception(
                "%s: error with player/get_now_playing_media for player %s after queue maintenance",
                self.name,
                entity_id,
            )
            return

        playing_qid = now_playing["qid"]
        _LOGGER.debug(
            "%s: now playing qid %d for player %s after queue maintenance",
            self.name,
            playing_qid,
            entity_id,
        )

        if playing_qid != 1:
            _LOGGER.error(
                "%s: playing qid is not '1' after queue maintenance for player %s",
                self.name,
                entity_id,
            )


class GetQueue(Service):
    """Handle the 'get_queue' service call."""

    hass: HomeAssistant

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    @property
    def name(self) -> str:
        return "get_queue"

    @property
    def schema(self) -> vol.Schema:
        return cv.make_entity_service_schema({})

    @property
    def supports_response(self) -> SupportsResponse:
        return SupportsResponse.ONLY

    @callback
    async def callback(self, service_call: ServiceCall) -> ServiceResponse:
        _LOGGER.debug("%s service call: %s", self.name, service_call)

        queues: dict[str, Any] = {}
        errors: list[JsonValueType] = []

        async def handler(entity_id: str, pid: int, connection: HeosConnection) -> None:
            try:
                items = await connection.command("player/get_queue", {"pid": pid})
                if items.result:
                    queues[entity_id] = items.payload
            except HeosError as e:
                _LOGGER.exception(
                    "%s: error getting queue from %s",
                    self.name,
                    entity_id,
                    exc_info=e,
                )
                errors.append(entity_id)

        await async_exec_command(self.hass, service_call, handler)

        return {"count": len(queues), "queues": queues, "errors": errors}


class RemoveFromQueue(Service):
    """Handle the 'remove_from_queue' service call."""

    hass: HomeAssistant

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    @property
    def name(self) -> str:
        return "remove_from_queue"

    @property
    def schema(self) -> vol.Schema:
        return cv.make_entity_service_schema({vol.Required("queue_id"): int})

    @property
    def supports_response(self) -> SupportsResponse:
        return SupportsResponse.OPTIONAL

    @callback
    async def callback(self, service_call: ServiceCall) -> ServiceResponse:
        _LOGGER.debug("%s service call: %s", self.name, service_call)

        qid = service_call.data.get("queue_id")
        if qid is None:
            _LOGGER.error("%s: missing queue_id in service data", self.name)
            return None

        qid = int(qid)

        errors: list[JsonValueType] = []

        async def handler(entity_id: str, pid: int, connection: HeosConnection) -> None:
            try:
                await connection.command(
                    "player/remove_from_queue", {"pid": pid, "qid": qid}
                )
            except HeosError as e:
                _LOGGER.exception(
                    "%s: error removing %d from queue for player %s",
                    self.name,
                    qid,
                    entity_id,
                    exc_info=e,
                )
                errors.append(entity_id)

        await async_exec_command(self.hass, service_call, handler)

        if service_call.return_response:
            return {"errors": errors}

        return None


async def async_exec_command(
    hass: HomeAssistant,
    service_call: ServiceCall,
    connection_callback: ConnectionCallback,
) -> None:
    """Execute a command against all requested entities in the service call."""

    # get the valid entities we can operate on (heos components only)
    entity_platforms = entity_platform.async_get_platforms(hass, "heos")
    entity_list: list[list[Entity]] = []
    for ep in entity_platforms:
        entity_list.append(await ep.async_extract_from_service(service_call))

    entities = {e for l in entity_list for e in l}
    _LOGGER.debug("operating on entities: %s", entities)

    for entity in entities:
        if not isinstance(entity, HeosMediaPlayer):
            _LOGGER.warning("Skipping non HEOS media player: %s", entity.entity_id)
            continue

        player = cast(HeosPlayer, entity._player)  # pylint: disable=protected-access
        connection = cast(
            HeosConnection, player._heos._connection  # pylint: disable=protected-access
        )
        await connection_callback(
            entity.entity_id,
            player._player_id,  # pylint: disable=protected-access
            connection,
        )
