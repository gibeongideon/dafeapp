"""
Encryption tests — 3 cases, no mocks needed.
"""

from django.test import TestCase, override_settings

from cloud.encryption import FieldEncryptor


@override_settings(FIELD_ENCRYPTION_KEY="HhC9AeGmYdlCNhCQ3JkHgSnMRFZLYpbMJb7SLxHRi1g=")
class EncryptionTests(TestCase):

    def test_encrypt_decrypt_roundtrip(self):
        """Encrypting then decrypting must return the original value."""
        original = "my-super-secret-password"
        ciphertext = FieldEncryptor.encrypt(original)
        self.assertNotEqual(ciphertext, original)
        self.assertEqual(FieldEncryptor.decrypt(ciphertext), original)

    def test_different_inputs_produce_different_ciphertext(self):
        """Two different values must not produce the same ciphertext."""
        c1 = FieldEncryptor.encrypt("password-one")
        c2 = FieldEncryptor.encrypt("password-two")
        self.assertNotEqual(c1, c2)

    def test_empty_string_is_handled(self):
        """Encrypting an empty string must return it unchanged (no-op)."""
        result = FieldEncryptor.encrypt("")
        self.assertEqual(result, "")
        self.assertEqual(FieldEncryptor.decrypt(""), "")
