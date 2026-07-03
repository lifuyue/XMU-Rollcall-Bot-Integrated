import os
import socket


def configure_network():
    """Apply process-local network preferences before HTTP requests start."""
    if not _env_truthy("XMU_ROLLCALL_FORCE_IPV4"):
        return

    try:
        import urllib3.util.connection as urllib3_connection
    except Exception:
        return

    urllib3_connection.allowed_gai_family = lambda: socket.AF_INET


def _env_truthy(name):
    value = os.environ.get(name, "")
    return str(value).strip().lower() in {"1", "true", "yes", "on", "y"}
