"""Self-signed certificate for local development and tests (the socket server only speaks TLS).

Production uses a real certificate (MCC_SOCKET_TLS_CERT/KEY). Without one the socket server creates a
self-signed certificate here and logs its fingerprint; a plugin connects to it with
`tls-fingerprint: <fingerprint>` in its config.yml.

    python -m mc_socket.devcert   # create (if missing) and print the fingerprint
"""
import hashlib
import os
import ssl
import subprocess
import sys

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DEV_CERT_DIR = os.path.join(PROJECT_ROOT, "certs")


def ensure_dev_certificate(directory=DEV_CERT_DIR, host="localhost"):
    """(cert path, key path) of a self-signed certificate, created with openssl if missing."""
    cert, key = os.path.join(directory, "dev-cert.pem"), os.path.join(directory, "dev-key.pem")
    if not (os.path.isfile(cert) and os.path.isfile(key)):
        os.makedirs(directory, exist_ok=True)
        try:
            subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "3650",
                            "-subj", f"/CN={host}", "-addext", f"subjectAltName=DNS:{host},IP:127.0.0.1",
                            "-keyout", key, "-out", cert], check=True, capture_output=True)
        except (OSError, subprocess.CalledProcessError) as e:
            raise RuntimeError("No TLS certificate configured (MCC_SOCKET_TLS_CERT/KEY) and openssl could not "
                               f"create a development certificate: {e}") from e
    return cert, key


def fingerprint(cert_path):
    """SHA-256 fingerprint of the (first) certificate in a PEM file, as "AB:CD:...", like keytool shows it."""
    with open(cert_path) as f:
        pem = f.read()
    end = pem.index("-----END CERTIFICATE-----") + len("-----END CERTIFICATE-----")
    digest = hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem[pem.index("-----BEGIN CERTIFICATE-----"):end])).hexdigest()
    return ":".join(digest[i:i + 2] for i in range(0, len(digest), 2)).upper()


if __name__ == "__main__":
    path = ensure_dev_certificate(*(sys.argv[1:2] or []))[0]
    print(f"{path}\ntls-fingerprint: {fingerprint(path)}")
