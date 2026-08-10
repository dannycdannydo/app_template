"""Tests for the Redis broker configuration shared by API and worker."""

import dramatiq
from dramatiq.brokers.redis import RedisBroker
from dramatiq.brokers.stub import StubBroker

from app.main import create_app


def test_create_app_installs_redis_broker() -> None:
    """API-side ``Actor.send`` publishes to Redis rather than a StubBroker."""
    create_app()
    assert isinstance(dramatiq.get_broker(), RedisBroker)


def test_tests_can_override_the_application_broker() -> None:
    """Test fixtures retain control by installing their StubBroker afterwards."""
    create_app()
    test_broker = StubBroker()
    dramatiq.set_broker(test_broker)
    assert dramatiq.get_broker() is test_broker
