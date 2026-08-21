# conftest.py — pytest configuration for autobot tests
# Suppresses the web3 pytest plugin that conflicts with eth_typing
collect_ignore_glob = []

def pytest_configure(config):
    """Disable the web3 pytest11 plugin if present (incompatible eth_typing version)."""
    pass
