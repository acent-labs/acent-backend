"""
Prompt Registry Loader

Loads and renders versioned prompt templates from YAML files.
Supports Jinja2 templating for dynamic prompt generation.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from jinja2 import Environment, BaseLoader, TemplateSyntaxError

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).parent / "registry"


@dataclass
class PromptSpec:
    """Loaded prompt specification with metadata and templates."""

    id: str
    version: str
    description: str
    purpose: str
    system_prompt: str
    user_prompt_template: str
    model_defaults: Dict[str, Any] = field(default_factory=dict)
    input_schema: Optional[str] = None
    output_schema: Optional[str] = None
    known_failure_modes: List[Dict[str, str]] = field(default_factory=list)
    eval_checks: List[Dict[str, str]] = field(default_factory=list)

    _env: Optional[Any] = field(default=None, repr=False)

    def __post_init__(self):
        """Initialize Jinja2 environment."""
        self._env = Environment(loader=BaseLoader())

    def render(self, context: Dict[str, Any]) -> Tuple[str, str]:
        """
        Render system and user prompts with context.

        Args:
            context: Dictionary of variables to inject into templates

        Returns:
            Tuple of (system_prompt, user_prompt)
        """
        try:
            system = self._env.from_string(self.system_prompt).render(**context)
            user = self._env.from_string(self.user_prompt_template).render(**context)
            return system, user
        except TemplateSyntaxError as e:
            logger.error(f"Template syntax error: {e}")
            raise ValueError(f"Failed to render prompt template: {e}")

    @property
    def temperature(self) -> float:
        """Get temperature from model defaults."""
        return self.model_defaults.get("temperature", 0.3)

    @property
    def max_tokens(self) -> int:
        """Get max_tokens from model defaults."""
        return self.model_defaults.get("max_tokens", 2000)

    @property
    def json_mode(self) -> bool:
        """Get json_mode from model defaults."""
        return self.model_defaults.get("json_mode", True)


@lru_cache(maxsize=50)
def load_prompt(prompt_id: str) -> PromptSpec:
    """
    Load prompt template by ID from registry.

    Args:
        prompt_id: Unique identifier for the prompt (e.g., 'ticket_analysis_cot_v1')

    Returns:
        PromptSpec object with loaded configuration

    Raises:
        FileNotFoundError: If prompt file doesn't exist
        ValueError: If prompt file is malformed
    """
    prompt_path = PROMPTS_DIR / f"{prompt_id}.yaml"

    if not prompt_path.exists():
        raise FileNotFoundError(f"Prompt not found: {prompt_id} at {prompt_path}")

    try:
        with open(prompt_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as e:
        raise ValueError(f"Failed to parse prompt YAML: {e}")

    # Validate required fields
    required = ["id", "system_prompt", "user_prompt_template"]
    for field_name in required:
        if field_name not in data:
            raise ValueError(f"Prompt missing required field: {field_name}")

    return PromptSpec(
        id=data["id"],
        version=data.get("version", "1.0.0"),
        description=data.get("description", ""),
        purpose=data.get("purpose", ""),
        system_prompt=data["system_prompt"],
        user_prompt_template=data["user_prompt_template"],
        model_defaults=data.get("model_defaults", {}),
        input_schema=data.get("input_schema"),
        output_schema=data.get("output_schema"),
        known_failure_modes=data.get("known_failure_modes", []),
        eval_checks=data.get("eval_checks", []),
    )


def get_prompt(prompt_id: str) -> PromptSpec:
    """
    Get prompt template by ID (alias for load_prompt with caching).

    Args:
        prompt_id: Unique identifier for the prompt

    Returns:
        PromptSpec object
    """
    return load_prompt(prompt_id)


def list_prompts() -> List[str]:
    """
    List all available prompt IDs in the registry.

    Returns:
        List of prompt IDs (filenames without .yaml extension)
    """
    if not PROMPTS_DIR.exists():
        return []

    return [p.stem for p in PROMPTS_DIR.glob("*.yaml")]


def clear_prompt_cache() -> None:
    """Clear the prompt cache. Useful for testing or hot-reloading."""
    load_prompt.cache_clear()
