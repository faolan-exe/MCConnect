"""The socket server's own TLS certificate: created on the first start, kept in MCC_SOCKET_TLS_DIR.

The plugin trusts exactly this certificate by its SHA-256 fingerprint (tls-fingerprint in its config.yml; the
admin page shows the line). No certificate authority, no renewal: the certificate is valid for 20 years.
An own certificate (MCC_SOCKET_TLS_CERT/KEY, e.g. Let's Encrypt) replaces it; the plugins then check it like
HTTPS and need no fingerprint.

    python -m mc_socket.tlscert   # create (if missing) and print the fingerprint
"""
import datetime
import hashlib
import os
import ssl
import sys

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
CERT_FILE, KEY_FILE = "cert.pem", "key.pem"


def paths(directory):
    return os.path.join(directory, CERT_FILE), os.path.join(directory, KEY_FILE)


def ensure_certificate(directory, host="mcconnect"):
    """(cert path, key path) of the own certificate in the directory, created if missing."""
    cert_path, key_path = paths(directory)
    if os.path.isfile(cert_path) and os.path.isfile(key_path):
        return cert_path, key_path
    os.makedirs(directory, exist_ok=True)
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)])
    now = datetime.datetime.now(datetime.timezone.utc)
    certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
                   .serial_number(x509.random_serial_number())
                   .not_valid_before(now - datetime.timedelta(days=1)).not_valid_after(now + datetime.timedelta(days=365 * 20))
                   .add_extension(x509.SubjectAlternativeName([x509.DNSName(host)]), critical=False)
                   .sign(key, hashes.SHA256()))
    # the key first and only readable by us; the certificate last, so a half written pair is never used
    with open(os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb") as f:
        f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                  serialization.NoEncryption()))
    with open(cert_path, "wb") as f:
        f.write(certificate.public_bytes(serialization.Encoding.PEM))
    return cert_path, key_path


def fingerprint(cert_path):
    """SHA-256 fingerprint of the (first) certificate in a PEM file, as "AB:CD:...", like keytool shows it."""
    with open(cert_path) as f:
        pem = f.read()
    end = pem.index("-----END CERTIFICATE-----") + len("-----END CERTIFICATE-----")
    digest = hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem[pem.index("-----BEGIN CERTIFICATE-----"):end])).hexdigest()
    return ":".join(digest[i:i + 2] for i in range(0, len(digest), 2)).upper()


def own_fingerprint(directory):
    """Fingerprint of the own certificate in the directory, or None if the socket server has not created it yet."""
    cert_path = paths(directory)[0]
    return fingerprint(cert_path) if os.path.isfile(cert_path) else None


if __name__ == "__main__":
    from database import config
    path = ensure_certificate(sys.argv[1] if len(sys.argv) > 1 else config.SOCKET_TLS_DIR)[0]
    print(f"{path}\ntls-fingerprint: \"{fingerprint(path)}\"")
