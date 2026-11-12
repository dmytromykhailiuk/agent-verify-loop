"""Transport registry. Config `transport`: auto | <name>; extra modules from config `transport_modules` and env
`VLOOP_TRANSPORTS` (file paths, os.pathsep-separated). An extra module registers itself by calling `register()` on
import or by defining `TRANSPORT` (a Transport subclass or instance).

Hybrid rule (`select`): the configured transport (auto → the highest-priority available one among those with
`auto = True`, i.e. herdr inside Herdr at depth 0, else agent) runs every role, except browser QA on a transport that
cannot attach an MCP server (`supports_browser = False`, the in-session agent), which runs headless."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

from .base import HookContext, SpawnResult, Transport

MODULES_ENV = "VLOOP_TRANSPORTS"
AUTO = "auto"
_REGISTRY: dict[str, Transport] = {}
_loaded: set[str] = set()


def register(transport: Transport | type) -> Transport:
    inst = transport() if isinstance(transport, type) else transport
    if not isinstance(inst, Transport) or not inst.name:
        raise TypeError("a transport must be a named Transport")
    _REGISTRY[inst.name] = inst
    return inst


def names() -> list[str]:
    return sorted(_REGISTRY)


def get(name: str) -> Transport:
    if name not in _REGISTRY:
        raise KeyError(f"unknown transport: {name!r} (registered: {', '.join(names())})")
    return _REGISTRY[name]


def module_paths(cfg: dict | None) -> list[str]:
    out = [str(p) for p in ((cfg or {}).get("transport_modules") or []) if isinstance(p, str) and p]
    out += [p for p in (os.environ.get(MODULES_ENV) or "").split(os.pathsep) if p]
    return out


def load_modules(cfg: dict | None) -> list[str]:
    errors = []
    for raw in module_paths(cfg):
        path = str(Path(raw).expanduser().resolve())
        if path in _loaded:
            continue
        _loaded.add(path)
        try:
            spec = importlib.util.spec_from_file_location(f"vloop_transport_{len(_loaded)}", path)
            if spec is None or spec.loader is None:
                raise ImportError(f"cannot load {path}")
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            if getattr(mod, "TRANSPORT", None) is not None:
                register(mod.TRANSPORT)
        except Exception as e:  # noqa: BLE001 - a broken extension must not break vloop
            errors.append(f"{path}: {e}")
            from .. import util

            util.log_error("transports.load", e)
    return errors


def configured(cfg: dict | None) -> Transport:
    """The session transport: config `transport` when registered, else auto."""
    cfg = cfg or {}
    load_modules(cfg)
    want = str(cfg.get("transport") or AUTO)
    if want != AUTO and want in _REGISTRY:
        return _REGISTRY[want]
    if want != AUTO:
        from .. import util

        util.log_error("transports.select", KeyError(f"unknown transport {want!r}; using auto"))
    ranked = sorted((t for t in _REGISTRY.values() if t.auto), key=lambda t: t.priority, reverse=True)
    for t in ranked:
        if t.available(cfg):
            return t
    return _REGISTRY["agent"]


def select(cfg: dict | None, role: str | None = None, browser: bool = False) -> Transport:
    t = configured(cfg)
    if role == "qa" and browser and not t.supports_browser:
        return _REGISTRY["headless"]
    return t


def for_role(ctx: dict, cfg: dict | None, role: str, plan_role: dict | None = None) -> Transport:
    """The transport of one role in a run: the run's recorded session transport, then the hybrid rule."""
    name = (ctx or {}).get("transport")
    base = {**(cfg or {}), "transport": name} if name else (cfg or {})
    return select(base, role, browser=((plan_role or {}).get("scope") == "browser"))


def hook_context(payload: dict | None, cfg: dict | None = None) -> HookContext:
    """Who a hook event belongs to: which session records it, is it a verifier child, is the gate off. The configured
    transport is asked first, then every other registered one by priority, because a child reports to its parent
    whichever transport would be selected inside it (herdr is never selected at depth >= 1). The first one that
    recognises a child session (or asks to suppress the gate) wins; otherwise the payload's own session."""
    try:
        first = [configured(cfg)]
    except Exception as e:  # noqa: BLE001 - a broken extension must not break hooks
        from .. import util

        util.log_error("transports.hook_context.select", e)
        first = []
    rest = sorted((t for t in _REGISTRY.values() if t not in first), key=lambda t: t.priority, reverse=True)
    for t in first + rest:
        try:
            hc = t.hook_context(payload or {})
        except Exception as e:  # noqa: BLE001 - a broken extension must not break hooks
            from .. import util

            util.log_error(f"transports.hook_context.{t.name}", e)
            continue
        if hc.is_child or hc.suppress_gate:
            return hc
    return HookContext(target_sid=str((payload or {}).get("session_id") or ""))


def strip_env() -> list[str]:
    """Env names no child process of vloop (triage, headless verifiers) may inherit, from every transport."""
    out: list[str] = []
    for t in _REGISTRY.values():
        for k in t.child_env_strip():
            if k not in out:
                out.append(k)
    return out


def child_env(extra: dict | None = None) -> dict:
    drop = set(strip_env())
    env = {k: v for k, v in os.environ.items() if k not in drop}
    env.update(extra or {})
    return env


def locate_transcript(info: dict | None) -> Path | None:
    name = (info or {}).get("transport")
    t = _REGISTRY.get(name) if isinstance(name, str) else None
    return t.locate_transcript(info or {}) if t else None


def _builtins() -> None:
    from .agent import AgentTransport
    from .headless import HeadlessTransport
    from .herdr import HerdrTransport

    register(AgentTransport())
    register(HeadlessTransport())
    register(HerdrTransport())


_builtins()

__all__ = ["HookContext", "SpawnResult", "Transport", "register", "get", "names", "select", "configured", "for_role",
           "load_modules", "hook_context", "strip_env", "child_env", "locate_transcript"]
