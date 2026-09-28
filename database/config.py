"""Central configuration, read from environment variables.

Defaults match the local docker setup in docker/db/docker-compose.yml.
"""
import os


def _env(name, default=None):
    value = os.environ.get(name)
    return value if value not in (None, "") else default


DB_CONFIG = {
    "host": _env("MCC_DB_HOST", "localhost"),
    "port": int(_env("MCC_DB_PORT", "5432")),
    "dbname": _env("MCC_DB_NAME", "mcConnect-TestDB-1"),
    "user": _env("MCC_DB_USER", "admin"),
    "password": _env("MCC_DB_PASSWORD", "admin"),
}
DB_POOL_MIN = int(_env("MCC_DB_POOL_MIN", "1"))
DB_POOL_MAX = int(_env("MCC_DB_POOL_MAX", "20"))
# Timezone timestamps are returned in (used for display on the website).
TIMEZONE = _env("MCC_TIMEZONE", "Europe/Berlin")

# Base domain the web app is served under; servers live on <subdomain>.<BASE_DOMAIN>.
BASE_DOMAIN = _env("MCC_BASE_DOMAIN", "mc.t-auer.local:5000")
# Scheme used when building absolute links (e.g. in emails).
PUBLIC_SCHEME = _env("MCC_PUBLIC_SCHEME", "http")

SOCKET_HOST = _env("MCC_SOCKET_HOST", "0.0.0.0")
SOCKET_PORT = int(_env("MCC_SOCKET_PORT", "9991"))
# The plugin connection is TLS only. Normally the socket server creates its own certificate in SOCKET_TLS_DIR on
# the first start and the plugins pin its fingerprint (mc_socket/tlscert.py; the web server reads the fingerprint
# from the same directory for the admin page). Optional: an own certificate (PEM, e.g. Let's Encrypt).
SOCKET_TLS_DIR = _env("MCC_SOCKET_TLS_DIR", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "certs"))
SOCKET_TLS_CERT = _env("MCC_SOCKET_TLS_CERT")
SOCKET_TLS_KEY = _env("MCC_SOCKET_TLS_KEY")

SMTP_HOST = _env("MCC_SMTP_HOST")
SMTP_PORT = int(_env("MCC_SMTP_PORT", "587"))
SMTP_USER = _env("MCC_SMTP_USER")
SMTP_PASSWORD = _env("MCC_SMTP_PASSWORD")

# Address the plugin connects to, shown on the manage page (defaults to the base domain).
PLUGIN_PUBLIC_HOST = _env("MCC_PLUGIN_PUBLIC_HOST", BASE_DOMAIN.split(":")[0])
PLUGIN_PUBLIC_PORT = int(_env("MCC_PLUGIN_PUBLIC_PORT", str(SOCKET_PORT)))
# Plugin jar offered for download; defaults to the local maven build output.
PLUGIN_JAR = _env("MCC_PLUGIN_JAR")

# Operator details for the legal pages (Impressum / Datenschutz).
# MCC_LEGAL_ADDRESS lines are separated by ";".
LEGAL_NAME = _env("MCC_LEGAL_NAME")
LEGAL_ADDRESS = [line.strip() for line in _env("MCC_LEGAL_ADDRESS", "").split(";") if line.strip()]
LEGAL_EMAIL = _env("MCC_LEGAL_EMAIL")
LEGAL_PHONE = _env("MCC_LEGAL_PHONE")

# Uploaded server images (banner, gallery).
UPLOAD_DIR = _env("MCC_UPLOAD_DIR", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "uploads"))
