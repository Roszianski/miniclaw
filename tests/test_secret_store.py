import os

from miniclaw.secrets import ScopedSecretStore
from miniclaw.secrets.store import SecretStore


def test_secret_store_encrypted_file_round_trip(tmp_path) -> None:
    store = SecretStore(namespace="test", backend="file", home=tmp_path)
    assert store.backend_name == "encrypted_file"

    assert store.set("skill:github:env:GITHUB_TOKEN", "abc123") is True
    assert store.get("skill:github:env:GITHUB_TOKEN") == "abc123"
    assert store.has("skill:github:env:GITHUB_TOKEN") is True

    # Re-open store to ensure persistence works.
    store2 = SecretStore(namespace="test", backend="file", home=tmp_path)
    assert store2.get("skill:github:env:GITHUB_TOKEN") == "abc123"
    assert store2.delete("skill:github:env:GITHUB_TOKEN") is True
    assert store2.get("skill:github:env:GITHUB_TOKEN") is None


def test_secret_store_file_backend_does_not_store_plaintext(tmp_path) -> None:
    store = SecretStore(namespace="test", backend="file", home=tmp_path)
    value = "top-secret-value"
    store.set("x", value)
    raw = (tmp_path / ".miniclaw" / "secrets.enc.json").read_text(encoding="utf-8")
    assert value not in raw


def test_secret_store_auto_falls_back_to_encrypted_file_when_no_keychain(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("MINICLAW_SECRETS_BACKEND", "auto")
    monkeypatch.setattr("miniclaw.secrets.store.platform.system", lambda: "Linux")
    monkeypatch.setattr("miniclaw.secrets.store.shutil.which", lambda _name: None)

    store = SecretStore(namespace="test", backend="auto", home=tmp_path)
    assert store.backend_name == "encrypted_file"


def test_secret_store_auto_prefers_keychain_when_available(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MINICLAW_SECRETS_BACKEND", "auto")
    monkeypatch.setattr("miniclaw.secrets.store.platform.system", lambda: "Darwin")
    monkeypatch.setattr(
        "miniclaw.secrets.store.shutil.which",
        lambda name: "/usr/bin/security" if name == "security" else None,
    )
    monkeypatch.setattr("miniclaw.secrets.store.KeychainBackend.is_usable", lambda _self: True)

    store = SecretStore(namespace="test", backend="auto", home=tmp_path)
    assert store.backend_name == "keychain"


def test_secret_store_auto_falls_back_when_keychain_unusable(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MINICLAW_SECRETS_BACKEND", "auto")
    monkeypatch.setattr("miniclaw.secrets.store.platform.system", lambda: "Darwin")
    monkeypatch.setattr(
        "miniclaw.secrets.store.shutil.which",
        lambda name: "/usr/bin/security" if name == "security" else None,
    )
    monkeypatch.setattr("miniclaw.secrets.store.KeychainBackend.is_usable", lambda _self: False)

    store = SecretStore(namespace="test", backend="auto", home=tmp_path)
    assert store.backend_name == "encrypted_file"


def test_secret_store_auto_runtime_failover_to_file(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MINICLAW_SECRETS_BACKEND", "auto")
    monkeypatch.setattr("miniclaw.secrets.store.platform.system", lambda: "Darwin")
    monkeypatch.setattr(
        "miniclaw.secrets.store.shutil.which",
        lambda name: "/usr/bin/security" if name == "security" else None,
    )

    state = {"checks": 0}

    def fake_usable(_self) -> bool:
        state["checks"] += 1
        return state["checks"] == 1

    monkeypatch.setattr("miniclaw.secrets.store.KeychainBackend.is_usable", fake_usable)
    monkeypatch.setattr("miniclaw.secrets.store.KeychainBackend.set", lambda _self, _key, _value: False)

    store = SecretStore(namespace="test", backend="auto", home=tmp_path)
    assert store.backend_name == "keychain"

    assert store.set("k", "v") is True
    assert store.backend_name == "encrypted_file"
    assert store.get("k") == "v"


def test_secret_store_uses_master_key_env_for_file_backend(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MINICLAW_SECRETS_MASTER_KEY", "unit-test-master-key")
    store = SecretStore(namespace="test", backend="file", home=tmp_path)
    store.set("k", "v")

    # Key file should still be absent when env master key is supplied.
    assert not (tmp_path / ".miniclaw" / "secrets.key").exists()
    assert store.get("k") == "v"

    monkeypatch.delenv("MINICLAW_SECRETS_MASTER_KEY", raising=False)
    os.environ.pop("MINICLAW_SECRETS_MASTER_KEY", None)


def test_scoped_secret_store_isolates_agent_scopes(tmp_path) -> None:
    base = SecretStore(namespace="test", backend="file", home=tmp_path)
    a = ScopedSecretStore(base, scope="agent-a")
    b = ScopedSecretStore(base, scope="agent-b")

    assert a.set("skill:github:env:GITHUB_TOKEN", "aaa")
    assert a.get("skill:github:env:GITHUB_TOKEN") == "aaa"
    assert b.get("skill:github:env:GITHUB_TOKEN") is None

    assert b.set("skill:github:env:GITHUB_TOKEN", "bbb")
    assert b.get("skill:github:env:GITHUB_TOKEN") == "bbb"
    assert a.get("skill:github:env:GITHUB_TOKEN") == "aaa"

    # OAuth/provider keys remain global by default.
    assert a.set("oauth:openai", "token-a")
    assert b.get("oauth:openai") == "token-a"
