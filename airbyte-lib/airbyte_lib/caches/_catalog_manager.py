# Copyright (c) 2023 Airbyte, Inc., all rights reserved.

"""A SQL Cache implementation."""
from __future__ import annotations

import json
from typing import TYPE_CHECKING, Callable

from sqlalchemy import Column, DateTime, String
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import Session
from sqlalchemy.sql import func

from airbyte_protocol.models import (
    AirbyteStateMessage,
    AirbyteStream,
    ConfiguredAirbyteCatalog,
    ConfiguredAirbyteStream,
    DestinationSyncMode,
    SyncMode,
)

from airbyte_lib import exceptions as exc


if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

STREAMS_TABLE_NAME = "_airbytelib_streams"
STATE_TABLE_NAME = "_airbytelib_state"

GLOBAL_STATE_STREAM_NAMES = ["_GLOBAL", "_LEGACY"]

Base = declarative_base()


class CachedStream(Base):  # type: ignore[valid-type,misc]
    __tablename__ = STREAMS_TABLE_NAME

    stream_name = Column(String)
    source_name = Column(String)
    table_name = Column(String, primary_key=True)
    catalog_metadata = Column(String)


class StreamState(Base):  # type: ignore[valid-type,misc]
    __tablename__ = STATE_TABLE_NAME

    source_name = Column(String)
    stream_name = Column(String)
    table_name = Column(String, primary_key=True)
    state_json = Column(String)
    last_updated = Column(DateTime(timezone=True), onupdate=func.now(), default=func.now())


class CatalogManager:
    """
    A class to manage the stream catalog of data synced to a cache:
    * What streams exist and to what tables they map
    * The JSON schema for each stream
    * The state of each stream if available
    """

    def __init__(
        self,
        engine: Engine,
        table_name_resolver: Callable[[str], str],
    ) -> None:
        self._engine: Engine = engine
        self._table_name_resolver = table_name_resolver
        self.source_catalog: ConfiguredAirbyteCatalog | None = None
        self._load_catalog_from_internal_table()

    def _ensure_internal_tables(self) -> None:
        engine = self._engine
        Base.metadata.create_all(engine)

    def record_state(
        self,
        source_name: str,
        state: AirbyteStateMessage,
        stream_name: str,
    ) -> None:
        self._ensure_internal_tables()
        engine = self._engine
        with Session(engine) as session:
            session.query(StreamState).filter(
                StreamState.table_name == self._table_name_resolver(stream_name)
            ).delete()
            session.commit()
            session.add(
                StreamState(
                    source_name=source_name,
                    stream_name=stream_name,
                    table_name=self._table_name_resolver(stream_name),
                    state_json=state.json(),
                )
            )
            session.commit()

    def get_state(
        self,
        source_name: str,
        streams: list[str],
    ) -> list[dict] | None:
        self._ensure_internal_tables()
        engine = self._engine
        with Session(engine) as session:
            states = (
                session.query(StreamState)
                .filter(
                    StreamState.source_name == source_name,
                    StreamState.stream_name.in_([*streams, *GLOBAL_STATE_STREAM_NAMES]),
                )
                .all()
            )
            if not states:
                return None
            # Only return the states if the table name matches what the current cache
            # would generate. Otherwise consider it part of a different cache.
            states = [
                state
                for state in states
                if state.table_name == self._table_name_resolver(state.stream_name)
            ]
            return [json.loads(state.state_json) for state in states]

    def register_source(
        self,
        source_name: str,
        incoming_source_catalog: ConfiguredAirbyteCatalog,
    ) -> None:
        if not self.source_catalog:
            self.source_catalog = incoming_source_catalog
        else:
            # merge in the new streams, keyed by name
            new_streams = {stream.stream.name: stream for stream in incoming_source_catalog.streams}
            for stream in self.source_catalog.streams:
                if stream.stream.name not in new_streams:
                    new_streams[stream.stream.name] = stream
            self.source_catalog = ConfiguredAirbyteCatalog(
                streams=list(new_streams.values()),
            )

        self._ensure_internal_tables()
        engine = self._engine
        with Session(engine) as session:
            # delete all existing streams from the db
            session.query(CachedStream).filter(
                CachedStream.table_name.in_(
                    [
                        self._table_name_resolver(stream.stream.name)
                        for stream in self.source_catalog.streams
                    ]
                )
            ).delete()
            session.commit()
            # add the new ones
            streams = [
                CachedStream(
                    source_name=source_name,
                    stream_name=stream.stream.name,
                    table_name=self._table_name_resolver(stream.stream.name),
                    catalog_metadata=json.dumps(stream.stream.json_schema),
                )
                for stream in incoming_source_catalog.streams
            ]
            session.add_all(streams)

            session.commit()

    def get_stream_config(
        self,
        stream_name: str,
    ) -> ConfiguredAirbyteStream:
        """Return the column definitions for the given stream."""
        if not self.source_catalog:
            raise exc.AirbyteLibInternalError(
                message="Cannot get stream JSON schema without a catalog.",
            )

        matching_streams: list[ConfiguredAirbyteStream] = [
            stream for stream in self.source_catalog.streams if stream.stream.name == stream_name
        ]
        if not matching_streams:
            raise exc.AirbyteStreamNotFoundError(
                stream_name=stream_name,
            )

        if len(matching_streams) > 1:
            raise exc.AirbyteLibInternalError(
                message="Multiple streams found with same name.",
                context={
                    "stream_name": stream_name,
                },
            )

        return matching_streams[0]

    def _load_catalog_from_internal_table(self) -> None:
        self._ensure_internal_tables()
        engine = self._engine
        with Session(engine) as session:
            # load all the streams
            streams: list[CachedStream] = session.query(CachedStream).all()
            if not streams:
                # no streams means the cache is pristine
                return

            # load the catalog
            self.source_catalog = ConfiguredAirbyteCatalog(
                streams=[
                    ConfiguredAirbyteStream(
                        stream=AirbyteStream(
                            name=stream.stream_name,
                            json_schema=json.loads(stream.catalog_metadata),
                            supported_sync_modes=[SyncMode.full_refresh],
                        ),
                        sync_mode=SyncMode.full_refresh,
                        destination_sync_mode=DestinationSyncMode.append,
                    )
                    for stream in streams
                    # only load the streams where the table name matches what
                    # the current cache would generate
                    if stream.table_name == self._table_name_resolver(stream.stream_name)
                ]
            )
