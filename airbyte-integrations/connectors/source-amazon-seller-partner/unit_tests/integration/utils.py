#
# Copyright (c) 2024 Airbyte, Inc., all rights reserved.
#


import json
from typing import Any, Dict, Optional

from airbyte_cdk.test.catalog_builder import CatalogBuilder
from airbyte_cdk.test.entrypoint_wrapper import EntrypointOutput, read
from airbyte_cdk.test.mock_http.response_builder import _get_unit_test_folder
from airbyte_protocol.models import ConfiguredAirbyteCatalog, SyncMode
from source_amazon_seller_partner import SourceAmazonSellerPartner

from .config import ConfigBuilder


def config() -> ConfigBuilder:
    return ConfigBuilder()


def catalog(stream_name: str, sync_mode: SyncMode) -> ConfiguredAirbyteCatalog:
    return CatalogBuilder().with_stream(stream_name, sync_mode).build()


def source() -> SourceAmazonSellerPartner:
    return SourceAmazonSellerPartner()


def read_output(
    config_builder: ConfigBuilder,
    stream_name: str, sync_mode: SyncMode,
    state: Optional[Dict[str, Any]] = None,
    expecting_exception: Optional[bool] = False,
) -> EntrypointOutput:
    _catalog = catalog(stream_name, sync_mode)
    _config = config_builder.build()
    return read(source(), _config, _catalog, state, expecting_exception)


def find_template(resource: str, execution_folder: str, template_format: Optional[str] = "csv") -> str:
    response_template_filepath = str(
        _get_unit_test_folder(execution_folder) / "resource" / "http" / "response" / f"{resource}.{template_format}"
    )
    with open(response_template_filepath, "r") as template_file:
        if template_file == "json":
            return json.load(template_file)
        else:
            return template_file.read()