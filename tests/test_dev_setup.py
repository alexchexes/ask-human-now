"""Tests for contributor development setup documentation and config."""

import os
import re

ROOT_DIR = os.path.join(os.path.dirname(__file__), "..")


def _extract_dev_entries(pyproject_text: str, section_name: str) -> list[str]:
    """Extract string entries from a `dev = [...]` array within a TOML section."""
    in_section = False
    collecting = False
    entries: list[str] = []

    for raw_line in pyproject_text.splitlines():
        line = raw_line.strip()

        if line.startswith("[") and line.endswith("]"):
            in_section = line == section_name
            collecting = False
            continue

        if not in_section:
            continue

        if not collecting and line == "dev = [":
            collecting = True
            continue

        if collecting:
            if line == "]":
                break

            match = re.match(r'"([^"]+)"', line)
            if match:
                entries.append(match.group(1))

    return entries


def _package_names(entries: list[str]) -> set[str]:
    """Extract package names from simple requirement strings."""
    return {re.split(r"[<>=!~ ]", entry, maxsplit=1)[0] for entry in entries}


def test_dev_setup_tooling_is_consistent():
    """Keep uv dev groups aligned with the published dev extra."""
    pyproject_path = os.path.join(ROOT_DIR, "pyproject.toml")
    with open(pyproject_path, encoding="utf-8") as pyproject_file:
        pyproject_text = pyproject_file.read()

    extra_dev = _extract_dev_entries(pyproject_text, "[project.optional-dependencies]")
    group_dev = _extract_dev_entries(pyproject_text, "[dependency-groups]")

    assert _package_names(extra_dev) == {"pytest", "black", "isort", "mypy"}
    assert _package_names(group_dev) == _package_names(extra_dev)


def test_readme_documents_dev_install_extra():
    """Document the editable install path that includes contributor tools."""
    readme_path = os.path.join(ROOT_DIR, "README.md")
    with open(readme_path, encoding="utf-8") as readme_file:
        readme_text = readme_file.read()

    assert 'pip install -e ".[dev]"' in readme_text
