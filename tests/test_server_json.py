"""MCP Registry server.json metadata coverage."""

import json
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pytest
from jsonschema import (  # type: ignore[import-untyped]
    Draft7Validator,
    FormatChecker,
    ValidationError,
)

SCHEMA_URI = "https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json"
URI_FORMAT_CHECKER = FormatChecker()


def _is_uri(value: object) -> bool:
    if not isinstance(value, str):
        return True
    parsed = urlparse(value)
    return bool(parsed.scheme) and not any(character.isspace() for character in value)


URI_FORMAT_CHECKER.checks("uri")(_is_uri)

SERVER_JSON_SCHEMA: dict[str, Any] = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "$id": SCHEMA_URI,
    "type": "object",
    "required": ["name", "description", "version"],
    "properties": {
        "$schema": {"type": "string", "format": "uri"},
        "name": {
            "type": "string",
            "minLength": 3,
            "maxLength": 200,
            "pattern": r"^[a-zA-Z0-9.-]+/[a-zA-Z0-9._-]+$",
        },
        "title": {"type": "string", "minLength": 1, "maxLength": 100},
        "description": {"type": "string", "minLength": 1, "maxLength": 100},
        "version": {"type": "string", "maxLength": 255},
        "websiteUrl": {"type": "string", "format": "uri"},
        "repository": {
            "type": "object",
            "required": ["url", "source"],
            "properties": {
                "url": {"type": "string", "format": "uri"},
                "source": {"type": "string"},
                "id": {"type": "string"},
            },
        },
        "packages": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["registryType", "identifier", "transport"],
                "properties": {
                    "registryType": {"type": "string"},
                    "identifier": {"type": "string"},
                    "version": {"type": "string", "minLength": 1, "not": {"const": "latest"}},
                    "runtimeHint": {"type": "string"},
                    "transport": {
                        "type": "object",
                        "required": ["type"],
                        "properties": {
                            "type": {"enum": ["stdio"]},
                        },
                    },
                },
            },
        },
    },
}


def test_server_json_validates_against_mcp_registry_schema() -> None:
    """Validate server.json against the current MCP Registry schema constraints."""
    server_json = _load_server_json()

    _validate_server_json(server_json)


def test_server_json_declares_roastpilot_pypi_stdio_package() -> None:
    """Check server.json declares the expected RoastPilot PyPI stdio metadata."""
    server_json = _load_server_json()
    packages = server_json["packages"]

    assert server_json["$schema"] == SCHEMA_URI
    assert server_json["name"] == "io.github.syamaner/coffee-roaster-mcp"
    assert server_json["title"] == "RoastPilot"
    assert packages == [
        {
            "registryType": "pypi",
            "identifier": "coffee-roaster-mcp",
            "version": "0.1.0",
            "runtimeHint": "uvx",
            "transport": {"type": "stdio"},
        }
    ]


def test_server_json_schema_rejects_malformed_uri_fields() -> None:
    """Check schema validation enforces URI formats for registry metadata."""
    server_json = _load_server_json()
    server_json["websiteUrl"] = "not a uri"

    with pytest.raises(ValidationError):
        _validate_server_json(server_json)


def _load_server_json() -> dict[str, Any]:
    path = Path(__file__).resolve().parents[1] / "server.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _validate_server_json(server_json: dict[str, Any]) -> None:
    Draft7Validator(
        SERVER_JSON_SCHEMA,
        format_checker=URI_FORMAT_CHECKER,
    ).validate(server_json)  # type: ignore[reportUnknownMemberType]
