"""Tests for private key validation and `vagrant ssh-config` import."""

from __future__ import annotations

import base64
import unittest

from cryptography.hazmat.primitives import serialization as S
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa

from mongopilot.automation.ssh import parse_ssh_config, target_for, validate_private_key
from mongopilot.crypto import SecretBox

SSH_CONFIG = """Host mongo1
  HostName 127.0.0.1
  User vagrant
  Port 2222
  UserKnownHostsFile /dev/null
  StrictHostKeyChecking no
  IdentityFile C:/Users/me/lab/.vagrant/machines/mongo1/virtualbox/private_key
  IdentitiesOnly yes
  LogLevel FATAL

Host mongo2
  HostName 192.168.56.12
  User vagrant
  Port 22
  IdentityFile "C:/Users/me/lab/.vagrant/machines/mongo2/virtualbox/private_key"
"""


class KeyTest(unittest.TestCase):
    def test_accepts_openssh_and_pem_keys(self):
        ed = ed25519.Ed25519PrivateKey.generate().private_bytes(S.Encoding.PEM, S.PrivateFormat.OpenSSH,
                                                                S.NoEncryption()).decode()
        self.assertTrue(validate_private_key(ed.replace("\n", "\r\n")).startswith("-----BEGIN OPENSSH PRIVATE KEY"))
        pem = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
            S.Encoding.PEM, S.PrivateFormat.TraditionalOpenSSL, S.NoEncryption()).decode()
        self.assertTrue(validate_private_key(pem).endswith("-----END RSA PRIVATE KEY-----\n"))

    def test_rejections(self):
        k = ed25519.Ed25519PrivateKey.generate()
        pub = k.public_key().public_bytes(S.Encoding.OpenSSH, S.PublicFormat.OpenSSH).decode() + " vagrant"
        enc_body = base64.b64encode(b"openssh-key-v1\x00" + (10).to_bytes(4, "big") + b"aes256-ctr" + b"\x00" * 40)
        enc = f"-----BEGIN OPENSSH PRIVATE KEY-----\n{enc_body.decode()}\n-----END OPENSSH PRIVATE KEY-----"
        cases = {
            "certificate": "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----",
            "public key": pub,
            "PuTTY": "PuTTY-User-Key-File-3: ssh-ed25519\nEncryption: none",
            "passphrase": enc,
            "Not a valid": "hello",
        }
        for expected, text in cases.items():
            with self.assertRaisesRegex(ValueError, expected):
                validate_private_key(text)


class SshConfigTest(unittest.TestCase):
    def test_parse_vagrant_ssh_config(self):
        hosts = parse_ssh_config(SSH_CONFIG)
        self.assertEqual([h["name"] for h in hosts], ["mongo1", "mongo2"])
        self.assertEqual((hosts[0]["hostname"], hosts[0]["port"], hosts[0]["forwarded"]),
                         ("host.docker.internal", 2222, True))
        self.assertEqual((hosts[1]["hostname"], hosts[1]["forwarded"]), ("192.168.56.12", False))
        self.assertTrue(hosts[1]["identity_file"].endswith("mongo2/virtualbox/private_key"))

    def test_vagrant_servers_do_not_pin_host_keys(self):
        box = SecretBox("k")
        srv = {"name": "m", "hostname": "h", "ssh_port": 22, "ssh_user": "vagrant", "ssh_key_enc": box.encrypt("K")}
        self.assertFalse(target_for({**srv, "kind": "vagrant"}, box).pin_host_key)
        self.assertTrue(target_for({**srv, "kind": "standard"}, box).pin_host_key)


if __name__ == "__main__":
    unittest.main()
