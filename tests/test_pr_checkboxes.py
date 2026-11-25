"""PR description checkbox parsing for selecting e2e flows."""

import json

import pytest

from bitbox_e2e import cli, config


def test_checked_subset_returns_only_checked_flows_in_canonical_order():
    body = """
    Fix signing flow.

    ### E2E tests
    - [ ] `device` — scenarios and API suites
    - [x] `rabby` — Rabby extension
    - [X] wc — WalletConnect
    - [ ] app
    """
    assert config.parse_pr_checkboxes(body, "firmware") == ["wc", "rabby"]


def test_unchecked_checklist_returns_empty_list():
    body = """
    ### E2E tests
    - [ ] device
    - [ ] app
    """
    assert config.parse_pr_checkboxes(body, "firmware") == []


def test_body_without_e2e_checkboxes_defaults_to_all_repo_flows():
    body = """
    Regular PR checklist:
    - [x] Updated changelog
    - [ ] Added unit tests
    """
    assert config.parse_pr_checkboxes(body, "firmware") == ["device", "app", "wc", "rabby"]
    assert config.parse_pr_checkboxes(None, "app") == ["app", "wc"]


def test_flows_invalid_for_repo_are_ignored():
    body = """
    - [x] device
    - [x] rabby
    - [x] wc
    """
    assert config.parse_pr_checkboxes(body, "app") == ["wc"]


def test_checkboxes_inside_html_comments_and_code_fences_are_ignored():
    body = """
    <!--
    - [x] rabby
    -->
    ```markdown
    - [x] wc
    ```
    - [x] device
    - [ ] app
    """
    assert config.parse_pr_checkboxes(body, "firmware") == ["device"]


def test_cli_flows_with_pr_body_file(tmp_path, capsys: pytest.CaptureFixture[str]):
    pr_md = tmp_path / "pr.md"
    pr_md.write_text("* [x] `app`: BitBoxApp tests\n* [ ] `wc`\n")
    args = cli.build_parser().parse_args(["flows", "api-go", "--pr-body", str(pr_md)])
    assert cli.dispatch(args) == 0
    assert json.loads(capsys.readouterr().out) == ["app"]
