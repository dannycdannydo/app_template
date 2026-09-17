"""Assert the safety-critical Redis policy in a resolved Compose document."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any


def _command(service: dict[str, Any]) -> list[str]:
    command = service.get("command", [])
    if isinstance(command, str):
        return command.split()
    return [str(part) for part in command]


def _volume_sources(service: dict[str, Any]) -> set[str]:
    return {
        str(volume["source"])
        for volume in service.get("volumes", [])
        if isinstance(volume, dict) and volume.get("type") == "volume"
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=("local", "production"), required=True)
    parser.parse_args()
    document = json.load(sys.stdin)
    services = document["services"]
    broker = services["redis-broker"]
    rate_limit = services["redis-rate-limit"]
    broker_command = _command(broker)
    rate_limit_command = _command(rate_limit)

    assert "--appendonly" in broker_command and "yes" in broker_command
    assert "--maxmemory-policy" in broker_command and "noeviction" in broker_command
    assert "--maxmemory-policy" in rate_limit_command and "allkeys-lru" in rate_limit_command
    broker_volumes = _volume_sources(broker)
    rate_limit_volumes = _volume_sources(rate_limit)
    assert len(broker_volumes) == 1
    assert len(rate_limit_volumes) == 1
    assert broker_volumes.isdisjoint(rate_limit_volumes)


if __name__ == "__main__":
    main()
