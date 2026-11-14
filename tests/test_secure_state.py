import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cryptography.fernet import Fernet

from scripts import secure_state


class SecureStateTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.private = root / "private"
        self.cache = root / "cache"
        for name, value in (("PRIVATE_DIR", self.private), ("CACHE_DIR", self.cache)):
            patcher = mock.patch.object(secure_state, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.key = Fernet.generate_key().decode("ascii")
        environment = mock.patch.dict(
            os.environ, {"BOT_STATE_ENCRYPTION_KEY": self.key}
        )
        environment.start()
        self.addCleanup(environment.stop)

    def _write_both(self):
        self.private.mkdir(parents=True, exist_ok=True)
        state = b'{"settings":{"codec":"h264"},"secret":"state-secret"}'
        session = b"\x80session-secret\x00sqlite-data"
        (self.private / "bot-state.json").write_bytes(state)
        (self.private / "bot-mtproto.session").write_bytes(session)
        return state, session

    def test_round_trip_both_files(self):
        state, session = self._write_both()
        secure_state.save()
        (self.private / "bot-state.json").unlink()
        (self.private / "bot-mtproto.session").unlink()

        secure_state.restore()

        self.assertEqual((self.private / "bot-state.json").read_bytes(), state)
        self.assertEqual((self.private / "bot-mtproto.session").read_bytes(), session)

    def test_missing_files_are_okay(self):
        with mock.patch.dict(os.environ, {"BOT_STATE_ENCRYPTION_KEY": ""}):
            secure_state.restore()
            secure_state.save()

        self.private.mkdir()
        state = b"state only"
        (self.private / "bot-state.json").write_bytes(state)
        secure_state.save()
        self.assertTrue((self.cache / "bot-state.json.enc").is_file())
        self.assertFalse((self.cache / "bot-mtproto.session.enc").exists())
        (self.private / "bot-state.json").unlink()
        secure_state.restore()
        self.assertEqual((self.private / "bot-state.json").read_bytes(), state)

    def test_missing_key_fails_without_overwriting_files(self):
        self.private.mkdir()
        state_path = self.private / "bot-state.json"
        state_path.write_bytes(b"secret state")
        with mock.patch.dict(os.environ, {"BOT_STATE_ENCRYPTION_KEY": ""}):
            with self.assertRaisesRegex(secure_state.SecureStateError, "required"):
                secure_state.save()
        self.assertFalse((self.cache / "bot-state.json.enc").exists())

        secure_state.save()
        state_path.write_bytes(b"current state")
        with mock.patch.dict(os.environ, {"BOT_STATE_ENCRYPTION_KEY": ""}):
            with self.assertRaisesRegex(secure_state.SecureStateError, "required"):
                secure_state.restore()
        self.assertEqual(state_path.read_bytes(), b"current state")

    def test_wrong_key_fails_without_overwriting_plaintext(self):
        state, _ = self._write_both()
        secure_state.save()
        state_path = self.private / "bot-state.json"
        state_path.write_bytes(b"current state")
        wrong_key = Fernet.generate_key().decode("ascii")

        with mock.patch.dict(os.environ, {"BOT_STATE_ENCRYPTION_KEY": wrong_key}):
            with self.assertRaisesRegex(
                secure_state.SecureStateError, "wrong key or corrupt"
            ):
                secure_state.restore()

        self.assertEqual(state_path.read_bytes(), b"current state")
        self.assertNotEqual(state_path.read_bytes(), state)

    def test_tamper_fails_before_any_plaintext_is_replaced(self):
        self._write_both()
        secure_state.save()
        state_path = self.private / "bot-state.json"
        session_path = self.private / "bot-mtproto.session"
        state_path.write_bytes(b"current state")
        session_path.write_bytes(b"current session")
        encrypted_path = self.cache / "bot-mtproto.session.enc"
        tampered = bytearray(encrypted_path.read_bytes())
        tampered[-1] ^= 1
        encrypted_path.write_bytes(tampered)

        with self.assertRaisesRegex(
            secure_state.SecureStateError, "wrong key or corrupt"
        ):
            secure_state.restore()

        self.assertEqual(state_path.read_bytes(), b"current state")
        self.assertEqual(session_path.read_bytes(), b"current session")

    def test_cache_contains_only_encrypted_files(self):
        state, session = self._write_both()
        secure_state.save()

        self.assertEqual(
            {path.name for path in self.cache.iterdir()},
            {"bot-state.json.enc", "bot-mtproto.session.enc"},
        )
        for name, plaintext in (("bot-state.json", state), ("bot-mtproto.session", session)):
            self.assertFalse((self.cache / name).exists())
            encrypted = (self.cache / f"{name}.enc").read_bytes()
            self.assertNotIn(plaintext, encrypted)


if __name__ == "__main__":
    unittest.main()
