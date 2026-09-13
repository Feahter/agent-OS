"""Test isolation for Agent OS.

Several components resolve their state root from ``AGENT_OS_HOME`` and fall
back to ``~/.agent-os``. Without this fixture a unit test could write telemetry
or state into the developer's real home directory, so the whole session is
redirected into a temporary directory.
"""

import os
import tempfile

import pytest

from grapheng import telemetry


@pytest.fixture(autouse=True, scope="session")
def isolated_agent_os_home():
    with tempfile.TemporaryDirectory(prefix="grapheng-test-home-") as directory:
        previous = os.environ.get("AGENT_OS_HOME")
        os.environ["AGENT_OS_HOME"] = directory
        telemetry.configure(home=None)
        try:
            yield directory
        finally:
            if previous is None:
                os.environ.pop("AGENT_OS_HOME", None)
            else:
                os.environ["AGENT_OS_HOME"] = previous
