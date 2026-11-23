"""Test setup: an in-memory keychain so no test touches the real OS keychain, and a throwaway data folder so tests
never write (or encrypt with a test key) anything in a real install's data/ folder."""
import os
import shutil
import tempfile
from pathlib import Path

_fixtures = Path(__file__).resolve().parent / "fixtures"
_home = Path(tempfile.mkdtemp(prefix="jobhunter-test-home-"))
# Tests always use the fictional sample profile and CVs in tests/fixtures (Alex Example), never your own files,
# so they give the same result on every machine and in CI. config.example.yaml is the config.
for d in ("cv", "facts"):
    shutil.copytree(_fixtures / d, _home / d)
os.environ["JOBHUNTER_HOME"] = str(_home)

import keyring  # noqa: E402
from keyring.backend import KeyringBackend  # noqa: E402


class MemoryKeyring(KeyringBackend):
    priority = 1

    def __init__(self):
        super().__init__()
        self.store = {}

    def get_password(self, service, username):
        return self.store.get((service, username))

    def set_password(self, service, username, password):
        self.store[(service, username)] = password

    def delete_password(self, service, username):
        from keyring.errors import PasswordDeleteError
        if (service, username) not in self.store:
            raise PasswordDeleteError(username)
        del self.store[(service, username)]


keyring.set_keyring(MemoryKeyring())
