"""Secret storage abstraction with keychain + encrypted file backends."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import platform
import shutil
import subprocess
from pathlib import Path


class SecretStore:
    """Store secrets in OS keychain when available, else encrypted file."""

    def __init__(
        self,
        *,
        namespace: str = "miniclaw",
        backend: str | None = None,
        home: Path | None = None,
    ):
        self.namespace = namespace
        resolved_backend = backend if backend is not None else os.environ.get("MINICLAW_SECRETS_BACKEND", "auto")
        self.backend = (resolved_backend or "auto").strip().lower()
        self.home = Path(home) if home else Path.home()
        self._auto_mode = self.backend == "auto"
        self._file_backend: EncryptedFileBackend | None = None

        if self.backend not in {"auto", "keychain", "file"}:
            self.backend = "auto"
            self._auto_mode = True

        keychain = KeychainBackend(namespace=self.namespace)
        if self.backend == "keychain":
            if not keychain.available:
                raise RuntimeError("Requested keychain backend but no supported keychain tool is available.")
            if not keychain.is_usable():
                raise RuntimeError("Requested keychain backend is unavailable in the current session.")
            self._impl: KeychainBackend | EncryptedFileBackend = keychain
            return
        if self.backend == "file":
            self._impl = self._get_file_backend()
            return

        # auto
        if keychain.available and keychain.is_usable():
            self._impl = keychain
        else:
            self._impl = self._get_file_backend()

    @property
    def backend_name(self) -> str:
        return self._impl.backend_name

    def _get_file_backend(self) -> "EncryptedFileBackend":
        if self._file_backend is None:
            self._file_backend = EncryptedFileBackend(home=self.home, namespace=self.namespace)
        return self._file_backend

    def _maybe_fail_over(self) -> bool:
        if not self._auto_mode:
            return False
        if isinstance(self._impl, KeychainBackend) and not self._impl.is_usable():
            self._impl = self._get_file_backend()
            return True
        return False

    def get(self, key: str) -> str | None:
        value = self._impl.get(key)
        if value is None and self._maybe_fail_over():
            return self._impl.get(key)
        return value

    def set(self, key: str, value: str) -> bool:
        ok = self._impl.set(key, value)
        if not ok and self._maybe_fail_over():
            return self._impl.set(key, value)
        return ok

    def delete(self, key: str) -> bool:
        ok = self._impl.delete(key)
        if not ok and self._maybe_fail_over():
            return self._impl.delete(key)
        return ok

    def has(self, key: str) -> bool:
        value = self.get(key)
        return bool(value)


class KeychainBackend:
    """Keychain backend using `security` (macOS) or `secret-tool` (Linux)."""

    def __init__(self, *, namespace: str = "miniclaw", system_name: str | None = None):
        self.namespace = namespace
        self.system_name = system_name or platform.system()
        self._security = shutil.which("security") if self.system_name == "Darwin" else None
        self._secret_tool = shutil.which("secret-tool") if self.system_name == "Linux" else None
        self.available = bool(self._security or self._secret_tool)
        self.backend_name = "keychain"

    def _service(self, key: str) -> str:
        return f"{self.namespace}:{key}"

    def is_usable(self) -> bool:
        if not self.available:
            return False
        try:
            if self._security:
                proc = subprocess.run(
                    [
                        self._security,
                        "find-generic-password",
                        "-a",
                        self.namespace,
                        "-s",
                        self._service("__probe__"),
                        "-w",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                # 0=found, 44=item-not-found. Both imply keychain is reachable.
                return proc.returncode in {0, 44}

            if self._secret_tool:
                if not os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
                    return False
                proc = subprocess.run(
                    [self._secret_tool, "lookup", "service", self.namespace, "key", "__probe__"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                # 0=found; 1=not found. Either is usable if no tool/session error text is emitted.
                if proc.returncode in {0, 1} and not (proc.stderr or "").strip():
                    return True
                return False
        except Exception:
            return False
        return False

    def get(self, key: str) -> str | None:
        if self._security:
            try:
                proc = subprocess.run(
                    [
                        self._security,
                        "find-generic-password",
                        "-a",
                        self.namespace,
                        "-s",
                        self._service(key),
                        "-w",
                    ],
                    capture_output=True,
                    text=True,
                )
            except Exception:
                return None
            if proc.returncode != 0:
                return None
            return proc.stdout.strip()

        if self._secret_tool:
            try:
                proc = subprocess.run(
                    [self._secret_tool, "lookup", "service", self.namespace, "key", key],
                    capture_output=True,
                    text=True,
                )
            except Exception:
                return None
            if proc.returncode != 0:
                return None
            return proc.stdout.strip()
        return None

    def set(self, key: str, value: str) -> bool:
        if self._security:
            try:
                proc = subprocess.run(
                    [
                        self._security,
                        "add-generic-password",
                        "-a",
                        self.namespace,
                        "-s",
                        self._service(key),
                        "-w",
                        value,
                        "-U",
                    ],
                    capture_output=True,
                    text=True,
                )
            except Exception:
                return False
            return proc.returncode == 0

        if self._secret_tool:
            try:
                proc = subprocess.run(
                    [
                        self._secret_tool,
                        "store",
                        "--label",
                        f"{self.namespace}:{key}",
                        "service",
                        self.namespace,
                        "key",
                        key,
                    ],
                    input=value + "\n",
                    capture_output=True,
                    text=True,
                )
            except Exception:
                return False
            return proc.returncode == 0
        return False

    def delete(self, key: str) -> bool:
        if self._security:
            try:
                proc = subprocess.run(
                    [
                        self._security,
                        "delete-generic-password",
                        "-a",
                        self.namespace,
                        "-s",
                        self._service(key),
                    ],
                    capture_output=True,
                    text=True,
                )
            except Exception:
                return False
            return proc.returncode == 0

        if self._secret_tool:
            try:
                proc = subprocess.run(
                    [self._secret_tool, "clear", "service", self.namespace, "key", key],
                    capture_output=True,
                    text=True,
                )
            except Exception:
                return False
            return proc.returncode == 0
        return False


class EncryptedFileBackend:
    """
    Encrypted file backend for headless environments.

    Encryption scheme:
    - Master key from env or local key file.
    - Per-write random salt + nonce.
    - Scrypt key derivation + HMAC-SHA256 stream cipher.
    - HMAC tag for integrity.
    """

    def __init__(self, *, home: Path, namespace: str = "miniclaw"):
        self.home = home
        self.namespace = namespace
        self.data_dir = self.home / ".miniclaw"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.secrets_file = self.data_dir / "secrets.enc.json"
        self.key_file = self.data_dir / "secrets.key"
        self.backend_name = "encrypted_file"
        self._master_key = self._load_master_key()

    def get(self, key: str) -> str | None:
        data = self._read_data()
        value = data.get(key)
        if isinstance(value, str):
            return value
        return None

    def set(self, key: str, value: str) -> bool:
        data = self._read_data()
        data[key] = value
        self._write_data(data)
        return True

    def delete(self, key: str) -> bool:
        data = self._read_data()
        if key not in data:
            return False
        data.pop(key, None)
        self._write_data(data)
        return True

    def _load_master_key(self) -> bytes:
        env_key = (os.environ.get("MINICLAW_SECRETS_MASTER_KEY") or "").strip()
        if env_key:
            return env_key.encode("utf-8")

        if self.key_file.exists():
            raw = self.key_file.read_bytes().strip()
            try:
                return base64.urlsafe_b64decode(raw)
            except Exception:
                return raw

        key = os.urandom(32)
        self.key_file.write_bytes(base64.urlsafe_b64encode(key))
        try:
            os.chmod(self.key_file, 0o600)
        except Exception:
            pass
        return key

    def _read_data(self) -> dict[str, str]:
        if not self.secrets_file.exists():
            return {}
        try:
            payload = json.loads(self.secrets_file.read_text(encoding="utf-8"))
            return self._decrypt_payload(payload)
        except Exception:
            return {}

    def _write_data(self, data: dict[str, str]) -> None:
        payload = self._encrypt_payload(data)
        self.secrets_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        try:
            os.chmod(self.secrets_file, 0o600)
        except Exception:
            pass

    def _encrypt_payload(self, data: dict[str, str]) -> dict[str, str | int]:
        plaintext = json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        salt = os.urandom(16)
        nonce = os.urandom(16)
        key = hashlib.scrypt(self._master_key, salt=salt, n=2**14, r=8, p=1, dklen=32)
        ciphertext = self._xor_stream(plaintext, key=key, nonce=nonce)
        tag = hmac.new(key, nonce + ciphertext, hashlib.sha256).digest()
        return {
            "v": 1,
            "salt": base64.b64encode(salt).decode("ascii"),
            "nonce": base64.b64encode(nonce).decode("ascii"),
            "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
            "tag": base64.b64encode(tag).decode("ascii"),
        }

    def _decrypt_payload(self, payload: dict) -> dict[str, str]:
        if int(payload.get("v") or 0) != 1:
            return {}

        salt = base64.b64decode(payload["salt"])
        nonce = base64.b64decode(payload["nonce"])
        ciphertext = base64.b64decode(payload["ciphertext"])
        tag = base64.b64decode(payload["tag"])
        key = hashlib.scrypt(self._master_key, salt=salt, n=2**14, r=8, p=1, dklen=32)
        expected = hmac.new(key, nonce + ciphertext, hashlib.sha256).digest()
        if not hmac.compare_digest(tag, expected):
            return {}
        plaintext = self._xor_stream(ciphertext, key=key, nonce=nonce)
        data = json.loads(plaintext.decode("utf-8"))
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _xor_stream(data: bytes, *, key: bytes, nonce: bytes) -> bytes:
        out = bytearray(len(data))
        offset = 0
        counter = 0
        while offset < len(data):
            block = hmac.new(key, nonce + counter.to_bytes(8, "big"), hashlib.sha256).digest()
            take = min(len(block), len(data) - offset)
            for idx in range(take):
                out[offset + idx] = data[offset + idx] ^ block[idx]
            offset += take
            counter += 1
        return bytes(out)
