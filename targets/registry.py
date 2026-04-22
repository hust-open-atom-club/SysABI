from __future__ import annotations

import importlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

from core.capabilities import capabilities_from_config
from targets.base import (
    PACKAGED_PER_CASE_EXECUTION_MODE,
    SHARED_RUNTIME_BATCH_EXECUTION_MODE,
    TargetAdapter,
)


class TargetLookupError(LookupError):
    """Raised when a workflow references an unsupported target."""


TargetAdapterFactory = Callable[[], TargetAdapter]

_TARGET_ADAPTER_FACTORIES: dict[str, TargetAdapterFactory] = {}


def register_target_adapter(name: str, factory: TargetAdapterFactory) -> None:
    _TARGET_ADAPTER_FACTORIES[name] = factory


def active_target_name(cfg: dict[str, Any]) -> str:
    target = cfg.get("target")
    if isinstance(target, str) and target:
        return target
    return "linux"


def _discover_target_adapter_factory(target: str) -> TargetAdapterFactory:
    module_name = f"targets.{target}.adapter"
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        if exc.name not in {module_name, f"targets.{target}"}:
            raise
        raise TargetLookupError(f"unsupported target: {target}") from exc

    factory = getattr(module, "build_target_adapter", None)
    if callable(factory):
        return factory

    for value in vars(module).values():
        if isinstance(value, type) and value.__name__.endswith("TargetAdapter"):
            return value

    raise TargetLookupError(f"target adapter module {module_name} did not expose an adapter factory")


def _validate_target_adapter(adapter: TargetAdapter, cfg: dict[str, Any]) -> None:
    """Validate that the adapter satisfies the TargetAdapter protocol and batch consistency."""
    if not isinstance(adapter, TargetAdapter):
        raise TargetLookupError(
            f"target adapter for {adapter.name!r} does not satisfy the TargetAdapter protocol"
        )

    capabilities = capabilities_from_config(cfg)
    if capabilities.supports_batch_execution:
        modes = set(adapter.execution_modes(cfg))
        if PACKAGED_PER_CASE_EXECUTION_MODE in modes:
            try:
                payload = adapter.prepare_case_package_payload([], cfg, {})
            except Exception as exc:
                raise TargetLookupError(
                    f"target adapter for {adapter.name!r} declares batch support with "
                    f"packaged_per_case but prepare_case_package_payload failed: {exc}"
                ) from exc
            if payload is None:
                raise TargetLookupError(
                    f"target adapter for {adapter.name!r} declares batch support with "
                    f"packaged_per_case but prepare_case_package_payload returns None"
                )
        if SHARED_RUNTIME_BATCH_EXECUTION_MODE in modes:
            try:
                payload = adapter.prepare_batch_manifest_payload([], cfg, {})
            except Exception as exc:
                raise TargetLookupError(
                    f"target adapter for {adapter.name!r} declares batch support with "
                    f"shared_runtime_batch but prepare_batch_manifest_payload failed: {exc}"
                ) from exc
            if payload is None:
                raise TargetLookupError(
                    f"target adapter for {adapter.name!r} declares batch support with "
                    f"shared_runtime_batch but prepare_batch_manifest_payload returns None"
                )


def get_target_adapter(cfg: dict[str, Any]) -> TargetAdapter:
    target = active_target_name(cfg)
    factory = _TARGET_ADAPTER_FACTORIES.get(target)
    if factory is None:
        factory = _discover_target_adapter_factory(target)
        register_target_adapter(target, factory)
    adapter = factory()
    _validate_target_adapter(adapter, cfg)
    return adapter


def available_targets() -> tuple[str, ...]:
    return tuple(sorted(_TARGET_ADAPTER_FACTORIES))


from targets.linux.adapter import build_target_adapter as build_linux_target_adapter

register_target_adapter("linux", build_linux_target_adapter)
