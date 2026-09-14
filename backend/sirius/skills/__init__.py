"""
Skill registry — the tools agents can call.

A Skill is an Anthropic tool (name + description + JSON-schema input) bound to a
Python handler. The agent loop turns registered skills into the `tools` array,
dispatches `tool_use` blocks to handlers, and feeds results back to the model.
Handlers may emit their own events (scan_finding, terraform_step, …).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class Skill:
    name: str
    description: str
    input_schema: dict
    handler: Callable[..., Any]

    def to_tool(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }

    def run(self, **kwargs) -> Any:
        return self.handler(**kwargs)


class SkillRegistry:
    def __init__(self) -> None:
        self._skills: dict[str, Skill] = {}

    def register(self, skill: Skill) -> Skill:
        self._skills[skill.name] = skill
        return skill

    def skill(self, name: str, description: str, input_schema: dict | None = None):
        """Decorator: register a function as a skill."""
        def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
            self.register(
                Skill(
                    name=name,
                    description=description,
                    input_schema=input_schema or {"type": "object", "properties": {}},
                    handler=fn,
                )
            )
            return fn
        return deco

    def get(self, name: str) -> Skill | None:
        return self._skills.get(name)

    def tools(self, names: list[str]) -> list[dict]:
        return [self._skills[n].to_tool() for n in names if n in self._skills]

    def names(self) -> list[str]:
        return list(self._skills)


# Global registry populated by feature modules on import.
registry = SkillRegistry()
