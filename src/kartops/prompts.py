"""Prompt loading + {{var}} / {{var=default}} substitution."""
from __future__ import annotations

import re
from functools import cache
from importlib import resources

_VAR = re.compile(r"\{\{\s*([a-zA-Z_]+)\s*(?:=\s*([^}]*?))?\s*\}\}")


@cache
def _load(name: str) -> str:
    return resources.files("kartops").joinpath("prompts", f"{name}.md").read_text(encoding="utf-8")


def render(name: str, variables: dict[str, str]) -> str:
    def sub(m: re.Match) -> str:
        key, default = m.group(1), m.group(2)
        if key in variables:
            return str(variables[key])
        if default is not None:
            return default
        raise KeyError(f"prompt '{name}' needs variable '{key}'")

    return _VAR.sub(sub, _load(name))


def system_blocks(node: str, variables: dict[str, str]) -> tuple[str, str]:
    """(BASE, NODE) rendered. BASE is identical across nodes so it can be prompt-cached
    (language is kept out of BASE caching concerns by passing a fixed value)."""
    return render("base", variables), render(node, variables)
