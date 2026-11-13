"""The listings (PyPI, the MCP registry, the Claude Code plugin) describe the same release."""

import json
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).parent.parent
PROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
SERVER = json.loads((ROOT / "server.json").read_text())
NAME = "io.github.TopTop-Oy/mdfi-connect"


def test_registry_name_matches_the_github_oidc_namespace():
    # The registry grants io.github.<repository_owner>/* to a GitHub Actions run and
    # matches it case-sensitively; the owner claim is "TopTop-Oy".
    assert SERVER["name"] == NAME


def test_readme_carries_the_registry_ownership_line():
    # The registry checks the PyPI description (this README) for "mcp-name: <name>"
    # followed by a boundary.
    assert re.search(rf"mcp-name: {re.escape(NAME)}(\s|-->)", (ROOT / "README.md").read_text())


def test_server_json_points_at_this_package_and_version():
    (package,) = SERVER["packages"]
    assert package["registryType"] == "pypi"
    assert package["identifier"] == PROJECT["name"]
    assert package["version"] == SERVER["version"] == PROJECT["version"]
    assert package["transport"] == {"type": "stdio"}
    env = {e["name"]: e for e in package["environmentVariables"]}
    assert env["MDFI_API_KEY"]["isRequired"] and env["MDFI_API_KEY"]["isSecret"]
    assert env["MDFI_API_URL"]["default"] == "https://api.mdfi.ai"


def test_plugin_runs_the_published_package_with_the_users_key():
    plugin = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())
    assert plugin["version"] == PROJECT["version"]
    market = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text())
    assert [p["name"] for p in market["plugins"]] == [plugin["name"]]
    server = json.loads((ROOT / ".mcp.json").read_text())["mcpServers"]["mdfi"]
    assert server["command"] == "uvx" and server["args"] == ["mdfi-connect"]
    # The key is read from the user's environment, never written into the repository.
    assert server["env"]["MDFI_API_KEY"] == "${MDFI_API_KEY}"


def test_package_links_the_public_repository():
    urls = PROJECT["urls"]
    assert urls["Repository"] == "https://github.com/TopTop-Oy/mdfi-connect"
    assert SERVER["repository"]["url"] == urls["Repository"]
