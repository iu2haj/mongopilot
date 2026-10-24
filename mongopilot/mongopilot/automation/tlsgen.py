"""TLS material for managed clusters: a private CA with server certificates, or validation of
certificates uploaded by the user."""

from __future__ import annotations

import datetime as dt
import ipaddress
import re
from typing import Any

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _pem_key(key) -> str:
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                             serialization.NoEncryption()).decode()


def generate_ca(name: str, years: int = 10) -> tuple[str, str]:
    """Return (certificate PEM, private key PEM) of a new private CA."""
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.ORGANIZATION_NAME, "MongoPilot"),
                         x509.NameAttribute(NameOID.COMMON_NAME, f"MongoPilot CA {name}"[:64])])
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(_now() - dt.timedelta(minutes=5))
            .not_valid_after(_now() + dt.timedelta(days=365 * years))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True,
                                         content_commitment=False, key_encipherment=False, data_encipherment=False,
                                         key_agreement=False, encipher_only=False, decipher_only=False), critical=True)
            .sign(key, hashes.SHA256()))
    return cert.public_bytes(serialization.Encoding.PEM).decode(), _pem_key(key)


def _san(names: list[str]) -> list[x509.GeneralName]:
    out: list[x509.GeneralName] = []
    for n in dict.fromkeys(n for n in names if n):
        try:
            out.append(x509.IPAddress(ipaddress.ip_address(n)))
        except ValueError:
            out.append(x509.DNSName(n))
    return out


def issue_server_cert(ca_cert_pem: str, ca_key_pem: str, names: list[str], years: int = 5) -> str:
    """Return a PEM bundle (certificate + private key) valid as server and cluster member certificate."""
    ca_cert = x509.load_pem_x509_certificate(ca_cert_pem.encode())
    ca_key = serialization.load_pem_private_key(ca_key_pem.encode(), password=None)
    key = ec.generate_private_key(ec.SECP256R1())
    cert = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.ORGANIZATION_NAME, "MongoPilot"),
                                     x509.NameAttribute(NameOID.COMMON_NAME, names[0][:64])]))
            .issuer_name(ca_cert.subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(_now() - dt.timedelta(minutes=5))
            .not_valid_after(_now() + dt.timedelta(days=365 * years))
            .add_extension(x509.SubjectAlternativeName(_san(names + ["localhost", "127.0.0.1"])), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH, ExtendedKeyUsageOID.CLIENT_AUTH]),
                           critical=False)
            .sign(ca_key, hashes.SHA256()))
    return cert.public_bytes(serialization.Encoding.PEM).decode() + _pem_key(key)


def _certs(pem: str) -> list[x509.Certificate]:
    try:
        return x509.load_pem_x509_certificates(pem.encode())
    except ValueError:
        return []


def inspect_ca(pem: str) -> dict[str, Any]:
    certs = _certs(pem or "")
    if not certs:
        raise ValueError("The CA file does not contain a PEM certificate (-----BEGIN CERTIFICATE-----)")
    c = certs[0]
    return {"subject": c.subject.rfc4514_string(), "not_after": c.not_valid_after_utc.isoformat()}


def inspect_server_pem(pem: str, ca_pem: str, expected_names: list[str]) -> dict[str, Any]:
    """Validate a certificate+key bundle: key present and matching, signed by the CA, SAN covering the host."""
    certs = _certs(pem or "")
    if not certs:
        raise ValueError("No certificate found: the file must contain the certificate followed by its private key")
    if "PRIVATE KEY-----" not in pem:
        raise ValueError("No private key found: append the unencrypted private key to the certificate (one PEM file)")
    if "ENCRYPTED" in pem:
        raise ValueError("The private key is encrypted: MongoPilot needs an unencrypted key")
    cert = certs[0]
    block = re.search(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", pem, re.S)
    try:
        key = serialization.load_pem_private_key(block.group(0).encode(), password=None)
    except Exception as exc:
        raise ValueError(f"The private key cannot be read: {exc}") from None
    pub = lambda k: k.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)  # noqa: E731
    if pub(key.public_key()) != pub(cert.public_key()):
        raise ValueError("The private key does not match the certificate")
    if cert.not_valid_after_utc < _now():
        raise ValueError(f"The certificate expired on {cert.not_valid_after_utc:%Y-%m-%d}")
    ca_certs = _certs(ca_pem or "")
    if ca_certs:
        try:
            cert.verify_directly_issued_by(ca_certs[0])
        except Exception:
            raise ValueError("The certificate is not signed by the CA you provided") from None
    try:
        sans = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        names = [str(n) for n in sans.get_values_for_type(x509.DNSName)] + \
                [str(n) for n in sans.get_values_for_type(x509.IPAddress)]
    except x509.ExtensionNotFound:
        names = []
    missing = [n for n in expected_names if n not in names]
    return {"subject": cert.subject.rfc4514_string(), "names": names, "missing_names": missing,
            "not_after": cert.not_valid_after_utc.isoformat()}
