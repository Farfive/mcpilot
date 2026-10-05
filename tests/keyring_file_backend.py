"""Test-only keyring backend (a JSON file), so tests never touch the real OS keychain.

Selected in subprocesses with PYTHON_KEYRING_BACKEND=keyring_file_backend.FileKeyring.
"""

import json
import os
from pathlib import Path

from keyring.backend import KeyringBackend
from keyring.errors import PasswordDeleteError


class FileKeyring(KeyringBackend):
    priority = 1

    def _path(self) -> Path:
        return Path(os.environ["MCPILOT_TEST_KEYRING"])

    def _load(self) -> dict:
        return json.loads(self._path().read_text()) if self._path().exists() else {}

    def get_password(self, service, username):
        return self._load().get(f"{service}/{username}")

    def set_password(self, service, username, password):
        data = self._load()
        data[f"{service}/{username}"] = password
        self._path().write_text(json.dumps(data))

    def delete_password(self, service, username):
        data = self._load()
        if data.pop(f"{service}/{username}", None) is None:
            raise PasswordDeleteError("missing")
        self._path().write_text(json.dumps(data))
