"""
Tests: container-engine detection (docker/podman compatibility).

Verifies partgraph.util.container, the leaf helper that makes every
container/compose invocation engine-agnostic so PartGraph runs on a pure
Podman host (no docker shim) as well as on Docker:

- PARTGRAPH_CONTAINER_ENGINE overrides auto-detection when set and present.
- An override pointing at a missing binary fails loudly (never silently picks
  a different engine).
- Auto-detection prefers podman, then falls back to docker.
- When neither engine is on PATH a clear ContainerEngineError is raised.
- compose_command() / engine_command() build the right argv prefix.

All detection tests are hermetic: PATH lookup (`which`) and the environment are
injected, so they pass in CI where no container engine is installed.
"""

from __future__ import annotations

import pytest

from partgraph.util import container


# ---------------------------------------------------------------------------
# Fake PATH lookups
# ---------------------------------------------------------------------------

def _which_for(*present: str):
    """Return a fake shutil.which that only finds the named executables."""
    available = set(present)

    def _which(name: str) -> str | None:
        return f"/usr/bin/{name}" if name in available else None

    return _which


# ---------------------------------------------------------------------------
# Environment-variable override
# ---------------------------------------------------------------------------

def test_env_override_selects_docker_even_when_podman_present() -> None:
    """Given PARTGRAPH_CONTAINER_ENGINE=docker and both engines on PATH.
    When we detect the engine.
    Then the override wins and 'docker' is returned (not the podman default).
    """
    engine = container.detect_engine(
        which=_which_for("docker", "podman"),
        environ={container.ENGINE_ENV_VAR: "docker"},
    )
    assert engine == "docker"


def test_env_override_selects_podman() -> None:
    """Given PARTGRAPH_CONTAINER_ENGINE=podman and only docker would auto-detect.
    When we detect the engine.
    Then the explicit override 'podman' is honoured.
    """
    engine = container.detect_engine(
        which=_which_for("docker", "podman"),
        environ={container.ENGINE_ENV_VAR: "podman"},
    )
    assert engine == "podman"


def test_env_override_is_whitespace_trimmed() -> None:
    """Given the override value has surrounding whitespace.
    When we detect the engine.
    Then the trimmed value is used for the PATH lookup.
    """
    engine = container.detect_engine(
        which=_which_for("podman"),
        environ={container.ENGINE_ENV_VAR: "  podman  "},
    )
    assert engine == "podman"


def test_env_override_missing_binary_raises() -> None:
    """Given PARTGRAPH_CONTAINER_ENGINE names a binary that is not on PATH.
    When we detect the engine.
    Then ContainerEngineError is raised (we never silently fall back).
    """
    with pytest.raises(container.ContainerEngineError) as exc:
        container.detect_engine(
            which=_which_for("docker", "podman"),
            environ={container.ENGINE_ENV_VAR: "nerdctl"},
        )
    assert container.ENGINE_ENV_VAR in str(exc.value)
    assert "nerdctl" in str(exc.value)


def test_empty_env_override_falls_through_to_autodetect() -> None:
    """Given the override is an empty string.
    When we detect the engine.
    Then it is treated as unset and auto-detection runs.
    """
    engine = container.detect_engine(
        which=_which_for("docker"),
        environ={container.ENGINE_ENV_VAR: ""},
    )
    assert engine == "docker"


def test_whitespace_only_env_override_falls_through() -> None:
    """Given the override is whitespace only.
    When we detect the engine.
    Then it is treated as unset and auto-detection runs.
    """
    engine = container.detect_engine(
        which=_which_for("podman"),
        environ={container.ENGINE_ENV_VAR: "   "},
    )
    assert engine == "podman"


# ---------------------------------------------------------------------------
# Environment-variable override — hostile / malformed values fail loudly
#
# A non-empty override is looked up verbatim on PATH as a single executable
# name. A value carrying internal whitespace, shell metacharacters or a missing
# relative path is therefore never found, so detection raises rather than
# splitting the value, shell-interpreting it, or silently substituting an
# auto-detected engine. The argv is always built list-style with shell=False,
# so even a metacharacter-laden value can never reach a shell. (SEC-1 / SEC-2)
# ---------------------------------------------------------------------------

def test_env_override_with_internal_whitespace_raises() -> None:
    """Given the override carries internal whitespace ('docker foo').
    When we detect the engine with both real engines on PATH.
    Then ContainerEngineError is raised: the value is looked up verbatim (never
    split into 'docker' plus arguments), so it is not found and we never fall
    back to an auto-detected engine.
    """
    with pytest.raises(container.ContainerEngineError) as exc:
        container.detect_engine(
            which=_which_for("docker", "podman"),
            environ={container.ENGINE_ENV_VAR: "docker foo"},
        )
    assert container.ENGINE_ENV_VAR in str(exc.value)


def test_env_override_shell_metacharacters_raises() -> None:
    """Given the override contains shell metacharacters ('docker ; rm -rf /').
    When we detect the engine with both real engines on PATH.
    Then ContainerEngineError is raised: the value is treated as one executable
    name looked up on PATH (never shell-interpreted), is not found, and we never
    silently fall back to an auto-detected engine.
    """
    with pytest.raises(container.ContainerEngineError) as exc:
        container.detect_engine(
            which=_which_for("docker", "podman"),
            environ={container.ENGINE_ENV_VAR: "docker ; rm -rf /"},
        )
    assert container.ENGINE_ENV_VAR in str(exc.value)


def test_env_override_relative_path_without_executable_raises() -> None:
    """Given the override is a relative path that is not on PATH ('./custom-engine').
    When we detect the engine and `which` resolves nothing.
    Then ContainerEngineError is raised rather than returning the unverified
    path, so a typo never selects a non-existent engine.
    """
    with pytest.raises(container.ContainerEngineError) as exc:
        container.detect_engine(
            which=lambda name: None,
            environ={container.ENGINE_ENV_VAR: "./custom-engine"},
        )
    assert container.ENGINE_ENV_VAR in str(exc.value)


# ---------------------------------------------------------------------------
# Auto-detection (podman-first)
# ---------------------------------------------------------------------------

def test_autodetect_prefers_podman_when_both_present() -> None:
    """Given no override and both docker and podman on PATH.
    When we detect the engine.
    Then podman is preferred (uses the real engine directly, not the docker
    shim, and the rootless engine when both are genuinely installed).
    """
    engine = container.detect_engine(
        which=_which_for("docker", "podman"),
        environ={},
    )
    assert engine == "podman"


def test_autodetect_falls_back_to_docker_when_only_docker() -> None:
    """Given no override and only docker on PATH.
    When we detect the engine.
    Then docker is used.
    """
    engine = container.detect_engine(
        which=_which_for("docker"),
        environ={},
    )
    assert engine == "docker"


def test_autodetect_uses_podman_when_only_podman() -> None:
    """Given no override and only podman on PATH.
    When we detect the engine.
    Then podman is used.
    """
    engine = container.detect_engine(
        which=_which_for("podman"),
        environ={},
    )
    assert engine == "podman"


def test_no_engine_available_raises_clear_error() -> None:
    """Given no override and neither engine on PATH.
    When we detect the engine.
    Then ContainerEngineError names both candidate engines so the user knows
    what to install.
    """
    with pytest.raises(container.ContainerEngineError) as exc:
        container.detect_engine(which=_which_for(), environ={})
    message = str(exc.value)
    assert "podman" in message
    assert "docker" in message


# ---------------------------------------------------------------------------
# argv builders
# ---------------------------------------------------------------------------

def test_compose_command_builds_engine_compose_prefix() -> None:
    """Given podman is the detected engine.
    When we build the compose command prefix.
    Then it is ['podman', 'compose'] (the v2 plugin form).
    """
    cmd = container.compose_command(
        which=_which_for("podman"),
        environ={},
    )
    assert cmd == ["podman", "compose"]


def test_compose_command_with_docker_override() -> None:
    """Given the docker override.
    When we build the compose command prefix.
    Then it is ['docker', 'compose'].
    """
    cmd = container.compose_command(
        which=_which_for("docker", "podman"),
        environ={container.ENGINE_ENV_VAR: "docker"},
    )
    assert cmd == ["docker", "compose"]


def test_engine_command_returns_single_element_prefix() -> None:
    """Given docker is the detected engine.
    When we build the bare engine command prefix.
    Then it is ['docker'] (for `docker port`, `docker ps`, ... in tests).
    """
    cmd = container.engine_command(
        which=_which_for("docker"),
        environ={},
    )
    assert cmd == ["docker"]
