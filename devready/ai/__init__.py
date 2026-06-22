"""AI-assisted README parsing.

Exposes :func:`parse_readme` and the :class:`ReadmeInsights` data structure.
"""

from .readme_parser import ReadmeInsights, parse_readme

__all__ = ["parse_readme", "ReadmeInsights"]
