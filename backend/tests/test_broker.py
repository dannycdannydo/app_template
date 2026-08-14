"""Tests for the broker configuration shared by API and worker."""

import dramatiq
import pytest
from dramatiq.brokers.redis import RedisBroker
from dramatiq.brokers.stub import StubBroker

from app import broker as broker_module
from app.main import create_app
from app.modules.files import tasks as files_tasks
from app.modules.notifications import tasks as notifications_tasks


def test_create_app_installs_network_free_broker_in_test_profile() -> None:
    """The test application cannot publish into a developer's Redis queues."""
    create_app()
    assert isinstance(dramatiq.get_broker(), StubBroker)


def test_development_profile_builds_redis_broker(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-test API and worker processes retain the real Redis broker."""
    settings = type(
        "DevelopmentSettings",
        (),
        {"app_env": "development", "redis_url": "redis://localhost:6379/0"},
    )()
    monkeypatch.setattr(broker_module, "get_settings", lambda: settings)

    assert isinstance(broker_module.build_broker(), RedisBroker)


def test_module_level_actors_are_bound_to_stub_broker_during_tests() -> None:
    """Import-time actor binding cannot escape into the development broker."""
    assert isinstance(files_tasks.process_file_actor.broker, StubBroker)
    assert isinstance(notifications_tasks.send_notification_email_actor.broker, StubBroker)


def test_tests_can_override_the_application_broker() -> None:
    """Test fixtures retain control by installing their StubBroker afterwards."""
    create_app()
    test_broker = StubBroker()
    dramatiq.set_broker(test_broker)
    assert dramatiq.get_broker() is test_broker
