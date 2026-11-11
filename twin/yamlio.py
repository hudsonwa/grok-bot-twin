"""YAML for the few small files the plugin reads and writes (a bot's config.yaml and profile.yaml), with whatever
the Hermes this plugin runs in ships.

Older Hermes releases happen to carry PyYAML (`import yaml`). A plain Hermes 0.23 has none: it ships its own YAML
module (`hermes_yaml`, built on ruamel.yaml). A plugin that asks for PyYAML alone fails quietly on such an install,
on every system: a mirrored twin gets no display name and no default model, and the model check says
"cannot tell". `module()` returns whichever is there,
with the two calls the plugin uses:

    safe_load(text) -> plain dicts, lists, strings, numbers
    safe_dump(data, default_flow_style=False, sort_keys=True, allow_unicode=True) -> str

or None when there is none (the callers then say "cannot tell" or leave the file alone, as before).
"""
from __future__ import annotations

from io import StringIO
from typing import Any, Optional

_WIDE = 2 ** 31 - 1  # one line per value: ruamel can change a quoted value when it folds a long line


class _HermesYaml:
    """Hermes' own YAML module behind the two calls above."""

    name = "hermes_yaml"

    def __init__(self, mod: Any) -> None:
        self._mod = mod

    def safe_load(self, text: Any) -> Any:
        return self._mod.safe_load(text)

    def safe_dump(self, data: Any, default_flow_style: bool = False, sort_keys: bool = True,
                  allow_unicode: bool = True) -> str:
        return self._mod.safe_dump(data, default_flow_style=default_flow_style, sort_keys=sort_keys,
                                   allow_unicode=allow_unicode, width=_WIDE)


class _Ruamel:
    """PyYAML's two calls on ruamel.yaml itself, the way Hermes configures it: the safe loader and dumper, YAML 1.1
    rules (so a text such as "off" is quoted and stays a text), block style, keys in the order given. Used when a
    Hermes carries ruamel.yaml but its own module cannot be imported from where the plugin runs."""

    name = "ruamel.yaml"

    @staticmethod
    def _yaml() -> Any:
        from ruamel.yaml import YAML
        from ruamel.yaml.resolver import VersionedResolver

        class Yaml11(VersionedResolver):
            @property
            def processing_version(self) -> tuple:
                return (1, 1)

        y = YAML(typ="safe", pure=True)
        y.Resolver = Yaml11
        return y

    @classmethod
    def safe_load(cls, text: Any) -> Any:
        y = cls._yaml()
        y.version = (1, 1)
        return y.load(text)

    @classmethod
    def safe_dump(cls, data: Any, default_flow_style: bool = False, sort_keys: bool = True,
                  allow_unicode: bool = True) -> str:
        y = cls._yaml()
        y.default_flow_style = default_flow_style
        y.allow_unicode = allow_unicode
        y.width = _WIDE
        y.sort_base_mapping_type_on_output = sort_keys
        out = StringIO()
        y.dump(data, out)
        return out.getvalue()


def module() -> Optional[Any]:
    """PyYAML when this Hermes has it, else Hermes' own YAML module, else ruamel.yaml, else None. Looked up on
    every call (cheap: all are cached imports), so a test can take any of them away."""
    try:
        import yaml  # optional
        if hasattr(yaml, "safe_load") and hasattr(yaml, "safe_dump"):
            return yaml
    except ImportError:
        pass
    try:
        import hermes_yaml  # part of every current Hermes
        if hasattr(hermes_yaml, "safe_load") and hasattr(hermes_yaml, "safe_dump"):
            return _HermesYaml(hermes_yaml)
    except ImportError:
        pass
    try:
        import ruamel.yaml  # noqa: F401
        return _Ruamel
    except ImportError:
        return None
