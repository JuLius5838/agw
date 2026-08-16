"""Unit tests for path resolution and permission-safe filesystem primitives."""

from __future__ import annotations

import hashlib
import os
import stat

import pytest

from agent_gateway import paths as paths_mod
from agent_gateway.paths import atomic_write_text, ensure_dir, get_paths

posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX permission semantics")


def test_defaults_under_home(tmp_path):
    p = get_paths({"HOME": str(tmp_path)})
    assert p.config_dir == tmp_path / ".config" / "agent-gateway"
    assert p.state_dir == tmp_path / ".local" / "state" / "agent-gateway"
    assert p.logs_dir == p.state_dir / "logs"
    assert p.credentials_dir == p.config_dir / "credentials"
    assert p.proxy_key_file == p.config_dir / "credentials" / "proxy-key"
    assert p.provider_credentials_dir("chatgpt") == p.credentials_dir / "chatgpt"
    assert p.shell_source_file("zsh") == p.config_dir / "shell" / "agw.zsh"


def test_external_subagent_paths_stay_under_state_root(tmp_path):
    paths = get_paths({"HOME": str(tmp_path)})
    assert paths.external_subagents_dir == paths.state_dir / "external-subagents"
    assert paths.compatibility_dir == paths.external_subagents_dir / "compatibility"
    assert paths.probe_dir == paths.external_subagents_dir / "probes"
    path = paths.compatibility_file("gpt-5.6-sol", "chatgpt")
    assert path.parent == paths.compatibility_dir / "chatgpt"
    assert path.name == hashlib.sha256(b"gpt-5.6-sol").hexdigest() + ".json"


def test_compatibility_filename_separates_names_that_differ_only_by_case(tmp_path):
    paths = get_paths({"HOME": str(tmp_path)})
    # Hashing the exact UTF-8 public name keeps distinct models distinct even on
    # a case-insensitive filesystem, where a raw name would collide.
    assert paths.compatibility_file("Model-A", "chatgpt") != paths.compatibility_file(
        "model-a", "chatgpt"
    )


@pytest.mark.parametrize("bad", ["", "..", "../escape", "Chatgpt", "chat gpt", "/abs", "-lead"])
def test_compatibility_file_rejects_an_unsafe_provider_segment(tmp_path, bad):
    paths = get_paths({"HOME": str(tmp_path)})
    with pytest.raises(ValueError, match="invalid provider path segment"):
        paths.compatibility_file("gpt-5.6-sol", bad)


def test_xdg_overrides(tmp_path):
    cfg, st = tmp_path / "cfg", tmp_path / "st"
    p = get_paths({"HOME": str(tmp_path), "XDG_CONFIG_HOME": str(cfg), "XDG_STATE_HOME": str(st)})
    assert p.config_dir == cfg / "agent-gateway"
    assert p.state_dir == st / "agent-gateway"


def test_xdg_relative_value_is_ignored(tmp_path):
    # Per the XDG spec, a relative XDG_*_HOME must be ignored (fall back to default).
    p = get_paths({"HOME": str(tmp_path), "XDG_CONFIG_HOME": "relative/path"})
    assert p.config_dir == tmp_path / ".config" / "agent-gateway"


@posix_only
def test_ensure_dir_is_0700(tmp_path):
    d = tmp_path / "a" / "b"
    ensure_dir(d)
    assert d.is_dir()
    assert stat.S_IMODE(d.stat().st_mode) == 0o700


@posix_only
def test_atomic_write_is_0600(tmp_path):
    f = tmp_path / "secret"
    atomic_write_text(f, "hello")
    assert f.read_text() == "hello"
    assert stat.S_IMODE(f.stat().st_mode) == 0o600


def test_atomic_write_overwrites_and_leaves_no_temp(tmp_path):
    f = tmp_path / "secret"
    atomic_write_text(f, "one")
    atomic_write_text(f, "two")
    assert f.read_text() == "two"
    # No leftover temp files from the atomic rename.
    assert list(tmp_path.glob(".secret.*")) == []


def test_packaged_default_models_loads():
    text = paths_mod.packaged_default_models_yaml()
    assert "default_model" in text
    assert "gpt-5.6-sol" in text
