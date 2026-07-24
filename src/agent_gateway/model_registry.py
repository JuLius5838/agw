"""External model registry and Claude Code picker identities.

A registry is parsed from YAML (the packaged team default or the user's
``models.yaml``) into immutable :class:`ModelEntry` values, then validated against
the hybrid-router invariants:

  * ``name`` is the exact routable model name given to Claude Code (no provider prefix).
  * ``display_name`` is an optional picker-only label and never affects routing.
  * ``upstream_model`` starts with its provider's prefix.
  * ``mode`` is ``responses`` or ``chat``.
  * exactly one *active* entry per public name.
  * ``default_model`` is optional; without it Claude Code keeps its native default.
  * picker ids use an internal ``anthropic.agw.*`` prefix solely to pass Claude
    Code's gateway discovery filter.

No role aliases, wildcards, implicit fallback, or provider load balancing.
"""

from __future__ import annotations

import re
from enum import StrEnum

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator, model_validator

from agent_gateway.errors import ConfigError, ModelUnavailableError
from agent_gateway.paths import Paths, packaged_default_models_yaml, read_text
from agent_gateway.providers import PROVIDER_PREFIX, Provider


class ModelMode(StrEnum):
    """The upstream API shape a model speaks."""

    responses = "responses"
    chat = "chat"


class ModelEntry(BaseModel):
    """One immutable public-name → upstream mapping candidate."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    display_name: str | None = None
    provider: Provider
    upstream_model: str
    mode: ModelMode
    enabled: bool = False

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        if not value or value != value.strip():
            raise ValueError("model name must be non-empty and not padded with whitespace")
        if "/" in value:
            raise ValueError(
                f"public model name '{value}' must not contain a provider prefix ('/')"
            )
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]*", value):
            raise ValueError(
                "public model name may contain only letters, digits, dot, underscore, colon, "
                "and hyphen"
            )
        if value.lower().startswith(("claude", "anthropic")):
            raise ValueError(
                "external model names must not overlap native Claude/Anthropic model names"
            )
        return value

    @field_validator("display_name")
    @classmethod
    def _validate_display_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value or value != value.strip():
            raise ValueError("display_name must be non-empty and not padded with whitespace")
        if len(value) > 80 or any(ord(character) < 32 for character in value):
            raise ValueError("display_name must be at most 80 printable characters")
        return value

    @model_validator(mode="after")
    def _validate_upstream_prefix(self) -> ModelEntry:
        prefix = PROVIDER_PREFIX[self.provider]
        if not self.upstream_model.startswith(prefix):
            raise ValueError(
                f"upstream_model '{self.upstream_model}' must start with '{prefix}' "
                f"for provider '{self.provider.value}'"
            )
        return self


PICKER_PREFIX = "anthropic.agw"


class ModelRegistry:
    """A validated set of external model entries and an optional startup default."""

    def __init__(self, default_model: str | None, models: tuple[ModelEntry, ...]) -> None:
        self.default_model = default_model
        self.models = models
        self._validate()

    def _validate(self) -> None:
        active = [m for m in self.models if m.enabled]

        # Duplicate active public names are the central safety check: a public name
        # must map to exactly one provider on a machine — never silently multiplexed.
        by_name: dict[str, list[ModelEntry]] = {}
        for entry in active:
            by_name.setdefault(entry.name, []).append(entry)
        duplicates = {name: entries for name, entries in by_name.items() if len(entries) > 1}
        if duplicates:
            details = "; ".join(
                f"'{name}' is active for providers: "
                + ", ".join(sorted(e.provider.value for e in entries))
                for name, entries in sorted(duplicates.items())
            )
            raise ConfigError(
                f"duplicate active model names: {details}",
                hint=(
                    "A public name may own only one provider. Disable all but one "
                    "candidate, or run `agw setup --provider-owner NAME=PROVIDER`."
                ),
            )

        if self.default_model is not None and self.default_model not in by_name:
            known = {m.name for m in self.models}
            if self.default_model in known:
                raise ConfigError(
                    f"default_model '{self.default_model}' exists but is not active.",
                    hint="Enable it, or point default_model at an active model.",
                )
            raise ConfigError(
                f"default_model '{self.default_model}' is not a configured model.",
                hint="Set default_model to one of the active model names.",
            )

    def active_models(self) -> tuple[ModelEntry, ...]:
        """Active entries, sorted by public name (deterministic ordering)."""
        return tuple(sorted((m for m in self.models if m.enabled), key=lambda m: m.name))

    def inactive_models(self) -> tuple[ModelEntry, ...]:
        return tuple(sorted((m for m in self.models if not m.enabled), key=lambda m: m.name))

    def get_active(self, name: str) -> ModelEntry:
        """Return the active entry for ``name`` or raise :class:`ModelUnavailableError`.

        The gateway never falls back to another model: an inactive, unknown, or
        removed name fails with its public name (and provider, when known) visible.
        """
        for entry in self.models:
            if entry.name == name and entry.enabled:
                return entry
        for entry in self.models:
            if entry.name == name and not entry.enabled:
                raise ModelUnavailableError(
                    f"model '{name}' (provider '{entry.provider.value}') is "
                    "configured but not active.",
                    hint="Enable it in models.yaml or run `agw setup`.",
                )
        raise ModelUnavailableError(
            f"model '{name}' is not configured.",
            hint="Run `agw models list` to see available models.",
        )

    def default_entry(self) -> ModelEntry | None:
        return self.get_active(self.default_model) if self.default_model is not None else None

    @staticmethod
    def picker_id(entry: ModelEntry) -> str:
        """Return the hidden discovery id for an external model."""
        return f"{PICKER_PREFIX}.{entry.provider.value}.{entry.name}"

    def picker_models(self) -> tuple[tuple[str, str], ...]:
        """Return ``(hidden id, picker label)`` pairs for active models."""
        return tuple(
            (self.picker_id(entry), entry.display_name or entry.name)
            for entry in self.active_models()
        )

    def resolve_routed_model(self, name: str) -> ModelEntry | None:
        """Resolve an exact public name or hidden picker id to an external route."""
        for entry in self.active_models():
            if name == entry.name or name == self.picker_id(entry):
                return entry
        return None


def load_registry_text(text: str) -> ModelRegistry:
    """Parse and validate a registry from YAML text."""
    try:
        raw = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"could not parse model registry: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("model registry must be a top-level mapping.")

    default_model = raw.get("default_model")
    if default_model is not None and (not isinstance(default_model, str) or not default_model):
        raise ConfigError("model registry 'default_model' must be a non-empty string or null.")

    models_raw = raw.get("models")
    if not isinstance(models_raw, list):
        raise ConfigError("model registry must contain a 'models' list.")

    entries: list[ModelEntry] = []
    for index, item in enumerate(models_raw):
        if not isinstance(item, dict):
            raise ConfigError(f"model entry #{index + 1} must be a mapping.")
        try:
            entries.append(ModelEntry.model_validate(item))
        except ValidationError as exc:
            raise ConfigError(f"invalid model entry #{index + 1}: {exc}") from exc

    return ModelRegistry(default_model, tuple(entries))


def load_registry(paths: Paths) -> ModelRegistry:
    """Load the user's ``models.yaml`` registry."""
    path = paths.models_file
    if not path.exists():
        raise ConfigError(
            "model registry not found.",
            hint="Run `agw setup` to install the default model registry.",
        )
    return load_registry_text(read_text(path))


def load_default_registry() -> ModelRegistry:
    """Load the packaged, credential-free team default registry."""
    return load_registry_text(packaged_default_models_yaml())
