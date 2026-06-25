"""Tests for the credential envelope module (PBKDF2+Fernet passphrase encryption)."""
from __future__ import annotations

import base64

import pytest
from cryptography.fernet import Fernet, InvalidToken

from shared.model_snapshot.credential_envelope import (
    KDF_ITERATIONS,
    KDF_ITERATIONS_MAX,
    KDF_ITERATIONS_MIN,
    KDF_VERSION,
    EnvelopeError,
    build_envelope,
    fernet_from_envelope,
    re_encrypt,
    re_encrypt_b64,
)


class TestBuildEnvelope:
    def test_returns_fernet_and_envelope_dict(self):
        fernet, envelope = build_envelope("test-pass-12345")
        assert isinstance(fernet, Fernet)
        assert isinstance(envelope, dict)

    def test_envelope_has_required_keys(self):
        _, envelope = build_envelope("test-pass-12345")
        assert envelope["method"] == "passphrase-fernet"
        assert "salt" in envelope
        assert "kdf" in envelope

    def test_envelope_kdf_params(self):
        _, envelope = build_envelope("test-pass-12345")
        kdf = envelope["kdf"]
        assert kdf["algorithm"] == "PBKDF2HMAC"
        assert kdf["hash"] == "SHA256"
        assert kdf["iterations"] == KDF_ITERATIONS
        assert kdf["version"] == KDF_VERSION

    def test_salt_is_base64_decodable(self):
        _, envelope = build_envelope("test-pass-12345")
        salt_bytes = base64.b64decode(envelope["salt"])
        assert len(salt_bytes) == 16

    def test_different_calls_produce_different_salts(self):
        _, env1 = build_envelope("same-pass-12345")
        _, env2 = build_envelope("same-pass-12345")
        assert env1["salt"] != env2["salt"]

    def test_fernet_can_encrypt_and_decrypt(self):
        fernet, _ = build_envelope("test-pass-12345")
        plaintext = b"secret-credential-data"
        ciphertext = fernet.encrypt(plaintext)
        assert fernet.decrypt(ciphertext) == plaintext


class TestFernetFromEnvelope:
    def test_round_trip_with_same_passphrase(self):
        passphrase = "correct-horse-battery"
        fernet_original, envelope = build_envelope(passphrase)
        plaintext = b"my-database-password"
        ciphertext = fernet_original.encrypt(plaintext)

        fernet_restored = fernet_from_envelope(passphrase, envelope)
        assert fernet_restored.decrypt(ciphertext) == plaintext

    def test_wrong_passphrase_fails(self):
        fernet_original, envelope = build_envelope("correct-passphrase")
        ciphertext = fernet_original.encrypt(b"secret")

        fernet_wrong = fernet_from_envelope("wrong-passphrase!", envelope)
        with pytest.raises(InvalidToken):
            fernet_wrong.decrypt(ciphertext)

    def test_preserves_custom_iteration_count(self):
        _, envelope = build_envelope("test-pass-12345")
        envelope["kdf"]["iterations"] = KDF_ITERATIONS
        fernet = fernet_from_envelope("test-pass-12345", envelope)
        assert isinstance(fernet, Fernet)


class TestKdfClamp:
    """F-020-14: a bundle is untrusted input; KDF params must be clamped."""

    def test_rejects_cpu_dos_iteration_count(self):
        _, envelope = build_envelope("pw")
        envelope["kdf"]["iterations"] = 2_000_000_000
        with pytest.raises(EnvelopeError):
            fernet_from_envelope("pw", envelope)

    def test_rejects_weakening_iteration_count(self):
        _, envelope = build_envelope("pw")
        envelope["kdf"]["iterations"] = 1
        with pytest.raises(EnvelopeError):
            fernet_from_envelope("pw", envelope)

    def test_rejects_unknown_kdf_algorithm(self):
        _, envelope = build_envelope("pw")
        envelope["kdf"]["algorithm"] = "scrypt"
        with pytest.raises(EnvelopeError):
            fernet_from_envelope("pw", envelope)

    def test_accepts_in_range_iterations(self):
        for iters in (KDF_ITERATIONS_MIN, KDF_ITERATIONS, KDF_ITERATIONS_MAX):
            _, envelope = build_envelope("pw")
            envelope["kdf"]["iterations"] = iters
            assert isinstance(fernet_from_envelope("pw", envelope), Fernet)


class TestReEncrypt:
    def test_re_encrypts_between_two_fernet_keys(self):
        source_fernet = Fernet(Fernet.generate_key())
        target_fernet = Fernet(Fernet.generate_key())
        plaintext = b"database-credentials-json"

        encrypted_by_source = source_fernet.encrypt(plaintext)
        re_encrypted = re_encrypt(
            encrypted_by_source,
            source_fernet=source_fernet,
            target_fernet=target_fernet,
        )

        assert target_fernet.decrypt(re_encrypted) == plaintext
        with pytest.raises(InvalidToken):
            source_fernet.decrypt(re_encrypted)

    def test_wrong_source_key_raises(self):
        wrong_source = Fernet(Fernet.generate_key())
        real_source = Fernet(Fernet.generate_key())
        target = Fernet(Fernet.generate_key())

        ciphertext = real_source.encrypt(b"secret")
        with pytest.raises(InvalidToken):
            re_encrypt(ciphertext, source_fernet=wrong_source, target_fernet=target)


class TestReEncryptB64:
    def test_round_trip_base64(self):
        source_fernet = Fernet(Fernet.generate_key())
        target_fernet = Fernet(Fernet.generate_key())
        plaintext = b"api-key-value"

        encrypted = source_fernet.encrypt(plaintext)
        b64_blob = base64.b64encode(encrypted).decode("ascii")

        result_b64 = re_encrypt_b64(
            b64_blob,
            source_fernet=source_fernet,
            target_fernet=target_fernet,
        )

        re_encrypted_raw = base64.b64decode(result_b64)
        assert target_fernet.decrypt(re_encrypted_raw) == plaintext

    def test_result_is_ascii_string(self):
        source = Fernet(Fernet.generate_key())
        target = Fernet(Fernet.generate_key())
        blob = base64.b64encode(source.encrypt(b"data")).decode("ascii")

        result = re_encrypt_b64(blob, source_fernet=source, target_fernet=target)
        assert isinstance(result, str)
        result.encode("ascii")


class TestEnvelopeEndToEnd:
    def test_export_import_credential_round_trip(self):
        """Simulates: system key encrypts cred -> export re-encrypts to
        passphrase key -> import re-encrypts back to a new system key."""
        system_key_source = Fernet(Fernet.generate_key())
        system_key_target = Fernet(Fernet.generate_key())
        passphrase = "migration-passphrase"
        credential = b'{"host":"db.example.com","password":"s3cret"}'

        system_encrypted = system_key_source.encrypt(credential)

        export_fernet, envelope = build_envelope(passphrase)
        portable = re_encrypt(
            system_encrypted,
            source_fernet=system_key_source,
            target_fernet=export_fernet,
        )

        import_fernet = fernet_from_envelope(passphrase, envelope)
        reimported = re_encrypt(
            portable,
            source_fernet=import_fernet,
            target_fernet=system_key_target,
        )

        assert system_key_target.decrypt(reimported) == credential
