"""The protocol client's own tests use no Home Assistant classes, so they run here too: the coverage of the integration measured by
this suite then includes the pairing, authentication, reconnect and sequencing paths that need a misbehaving or absent device."""
import pytest

from tests.test_client import PairTests, SessionTests  # noqa: F401  (collected by pytest from this module)

pytestmark = pytest.mark.usefixtures("socket_enabled")      # the device in these tests listens on loopback
