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


def test_claude_plugin_runs_its_locked_project_with_a_sensitive_key():
    # The Claude directory refuses a PyPI version as a pin (a release can gain files);
    # it accepts the plugin's own uv project started with uv run --locked.
    plugin = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())
    assert plugin["version"] == PROJECT["version"]
    assert plugin["userConfig"]["api_key"]["sensitive"] is True
    market = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text())
    assert [p["name"] for p in market["plugins"]] == [plugin["name"]]
    server = json.loads((ROOT / ".mcp.json").read_text())["mcpServers"]["mdfi"]
    assert server["command"] == "uv"
    assert server["args"] == [
        "run", "--locked", "--no-dev", "--project", "${CLAUDE_PLUGIN_ROOT}", "mdfi-connect"
    ]
    assert server["env"] == {"CLAUDE_PLUGIN_OPTION_API_KEY": "${user_config.api_key}"}
    assert (ROOT / "uv.lock").is_file()


def test_package_links_the_public_repository():
    urls = PROJECT["urls"]
    assert urls["Repository"] == "https://github.com/TopTop-Oy/mdfi-connect"
    assert SERVER["repository"]["url"] == urls["Repository"]


def test_cursor_plugin_declares_the_key_and_runs_the_published_package():
    # Cursor's marketplace reads .cursor-plugin/plugin.json; the key is a declared
    # variable that users fill in Cursor's dashboard, never a value in the repo.
    plugin = json.loads((ROOT / ".cursor-plugin" / "plugin.json").read_text())
    assert plugin["name"] == "mdfi" and plugin["version"] == PROJECT["version"]
    assert (ROOT / plugin["logo"]).is_file()
    assert plugin["variables"]["required"] == ["MDFI_API_KEY"]
    # Inline, so Cursor does not pick up the Claude Code .mcp.json instead.
    server = plugin["mcpServers"]["mdfi"]
    assert server["command"] == "uvx" and server["args"] == ["mdfi-connect"]
    assert server["env"] == {"MDFI_API_KEY": "${MDFI_API_KEY}"}
