"""MCP Registry server.json metadata coverage."""

import json
from pathlib import Path
from typing import Any

from jsonschema import Draft7Validator  # type: ignore[import-untyped]

SCHEMA_URI = "https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json"

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

    Draft7Validator(SERVER_JSON_SCHEMA).validate(server_json)  # type: ignore[reportUnknownMemberType]


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


def _load_server_json() -> dict[str, Any]:
    path = Path(__file__).resolve().parents[1] / "server.json"
    return json.loads(path.read_text(encoding="utf-8"))
