#!/usr/bin/env python3
"""
create-change.py — Reads a `change-request` YAML block from a PR body,
calls the Support Platform GraphQL API to create a Change, then posts
(or updates) an idempotent comment on the PR.

Requirements: Python 3 stdlib only (no third-party packages).
Exit codes: 0 = success or no change-request block present, 1 = error.
"""

import json
import os
import re
import sys
import urllib.error
import urllib.request
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

COMMENT_MARKER = "<!-- change-manager-bot -->"
DEFAULT_SUPPORT_API_URL = "https://support-api.apps.aa.intility.com/graphql"

GRAPHQL_MUTATION = """
mutation CreateChange($input: CreateChangeInput!) {
  createChange(input: $input) {
    id
    title
    startedOn
    endedOn
  }
}
""".strip()

# ---------------------------------------------------------------------------
# Simple YAML parser (stdlib only)
# ---------------------------------------------------------------------------


def _coerce_value(raw: str) -> Any:
    """Convert a raw YAML scalar string to an appropriate Python type."""
    stripped = raw.strip()

    # Quoted string — strip the quotes and return as-is
    if (stripped.startswith('"') and stripped.endswith('"')) or (
        stripped.startswith("'") and stripped.endswith("'")
    ):
        return stripped[1:-1]

    # Boolean
    if stripped.lower() == "true":
        return True
    if stripped.lower() == "false":
        return False

    # Null
    if stripped.lower() in ("null", "~", ""):
        return None

    # Integer
    try:
        return int(stripped)
    except ValueError:
        pass

    # Float
    try:
        return float(stripped)
    except ValueError:
        pass

    # Plain string
    return stripped


def parse_yaml(text: str) -> dict:
    """
    Parse a subset of YAML sufficient for the change-request block.

    Supported features:
    - Top-level scalar key: value
    - Nested dicts via indentation (one level deep is enough)
    - Simple list items starting with '  - '
    - Comments (lines beginning with #)
    - Quoted and unquoted scalars, booleans, integers

    Returns a nested dict/list structure.
    """
    lines = text.splitlines()
    result: dict = {}
    # Stack of (indent_level, container) tuples — we push nested dicts here.
    # For simplicity we handle exactly two levels (top-level + one nesting).
    current_dict: dict = result
    current_key: Optional[str] = None       # last top-level key seen
    current_indent: int = 0                 # indent of last mapping key
    list_key: Optional[str] = None          # key whose value is a list
    list_indent: int = -1                   # indent of the list items
    parent_dict: dict = result              # parent container for nested dict

    for raw_line in lines:
        # Skip blank lines and comments
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        # Determine indent level (number of leading spaces)
        indent = len(raw_line) - len(raw_line.lstrip(" "))

        # ---- List item -------------------------------------------------------
        if stripped.startswith("- "):
            item_raw = stripped[2:].strip()
            # Is this a mapping item (key: value inside a list)?
            if ":" in item_raw:
                key_part, _, val_part = item_raw.partition(":")
                # Start a new dict inside the list
                item_dict: dict = {key_part.strip(): _coerce_value(val_part)}
                if list_key and isinstance(current_dict.get(list_key), list):
                    current_dict[list_key].append(item_dict)
                    # Point current_dict at this new dict so subsequent
                    # indented keys can be added to it.
                    current_key = list_key
                    # We'll handle continuation below via normal key parsing
                    # on subsequent lines — store a reference.
                    # Use a small sentinel so we know we're inside a list-item dict.
                    current_dict["__last_list_item__"] = item_dict
            else:
                # Plain scalar list item
                value = _coerce_value(item_raw)
                if list_key and isinstance(current_dict.get(list_key), list):
                    current_dict[list_key].append(value)
            continue

        # ---- Mapping key: value ----------------------------------------------
        if ":" not in stripped:
            # Value continuation or something we don't handle — skip
            continue

        colon_pos = stripped.index(":")
        key = stripped[:colon_pos].strip()
        value_raw = stripped[colon_pos + 1:].strip()

        if indent == 0:
            # Top-level key
            # Clean up sentinel from previous list-item dict processing
            result.pop("__last_list_item__", None)
            current_dict = result
            parent_dict = result
            current_indent = 0
            list_key = None
            list_indent = -1

            if value_raw == "":
                # Key with no inline value — could be a nested dict or list
                result[key] = {}        # will be replaced if list items follow
                current_key = key
                current_dict = result
            else:
                result[key] = _coerce_value(value_raw)
                current_key = key

        else:
            # Indented key — belongs to a nested dict or a list-item dict
            # Check if we are inside a list-item dict
            sentinel = result.get("__last_list_item__")
            if sentinel is not None and isinstance(sentinel, dict) and indent > list_indent:
                # Additional keys for the most-recent list item mapping
                sentinel[key] = _coerce_value(value_raw)
            elif current_key is not None:
                # Nested dict under current_key
                parent = result.get(current_key)
                if not isinstance(parent, dict):
                    result[current_key] = {}
                result[current_key][key] = _coerce_value(value_raw)

        # If value_raw is empty this key might introduce a list — remember it
        if value_raw == "" and indent == 0:
            list_key = key
            list_indent = indent + 2   # expect list items indented by 2

        # If value_raw is empty at indent > 0, it could be a nested list key
        if value_raw == "" and indent > 0 and current_key is not None:
            nested = result.get(current_key, {})
            if isinstance(nested, dict):
                nested[key] = []
                list_key = key
                list_indent = indent + 2
                current_dict = nested

    # Remove internal sentinel before returning
    result.pop("__last_list_item__", None)

    # Post-process: any top-level key that was initialised as {} and never
    # populated as a nested dict but received list items via list_key tracking
    # is already correct.  Nothing more needed.
    return result


# ---------------------------------------------------------------------------
# Extraction helpers
# ---------------------------------------------------------------------------


def extract_change_request_block(pr_body: str) -> Optional[str]:
    """
    Return the raw YAML text inside the ```change-request ... ``` fence,
    or None if no such block is found.
    """
    pattern = r"```change-request\n(.*?)```"
    match = re.search(pattern, pr_body, re.DOTALL)
    if match:
        return match.group(1)
    return None


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
ISO_DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")


def validate_fields(data: dict) -> dict:
    """
    Validate parsed YAML fields. Returns the cleaned input dict ready for
    the GraphQL variables, or raises SystemExit(1) with a message.
    """
    errors = []

    # -- Required scalars --
    title = data.get("title")
    if not title:
        errors.append("Missing required field: title")

    owner_guid = data.get("owner_guid")
    if not owner_guid:
        errors.append("Missing required field: owner_guid")
    elif not UUID_RE.match(str(owner_guid)):
        errors.append(f"Invalid UUID for owner_guid: {owner_guid!r}")

    started_on = data.get("started_on")
    if not started_on:
        errors.append("Missing required field: started_on")
    elif not ISO_DATETIME_RE.match(str(started_on)):
        errors.append(f"started_on is not a valid ISO datetime: {started_on!r}")

    ended_on = data.get("ended_on")
    if not ended_on:
        errors.append("Missing required field: ended_on")
    elif not ISO_DATETIME_RE.match(str(ended_on)):
        errors.append(f"ended_on is not a valid ISO datetime: {ended_on!r}")

    impacts_all = data.get("impacts_all")
    if impacts_all is None:
        errors.append("Missing required field: impacts_all")
    elif not isinstance(impacts_all, bool):
        errors.append(f"impacts_all must be a boolean (true/false), got: {impacts_all!r}")

    if errors:
        for err in errors:
            print(f"[ERROR] {err}", file=sys.stderr)
        sys.exit(1)

    # -- Build the GraphQL input object --
    gql_input: dict = {
        "ownerGuid": str(owner_guid),
        "title": str(title),
        "startedOn": str(started_on),
        "endedOn": str(ended_on),
        "impactsAll": bool(impacts_all),
        "isInternal": bool(data.get("is_internal", True)),
    }

    description = data.get("description")
    if description:
        gql_input["description"] = str(description)

    # -- tickets.reference_numbers --
    tickets = data.get("tickets")
    if isinstance(tickets, dict):
        ref_nums = tickets.get("reference_numbers")
        if ref_nums and isinstance(ref_nums, list):
            try:
                gql_input["tickets"] = [{"referenceNumbers": [int(n) for n in ref_nums]}]
            except (TypeError, ValueError) as exc:
                print(f"[ERROR] tickets.reference_numbers must be a list of integers: {exc}", file=sys.stderr)
                sys.exit(1)

    # -- hyperlinks --
    hyperlinks = data.get("hyperlinks")
    if hyperlinks and isinstance(hyperlinks, list):
        built = []
        for idx, item in enumerate(hyperlinks):
            if not isinstance(item, dict):
                print(f"[ERROR] hyperlinks[{idx}] must be a mapping with 'name' and 'url'", file=sys.stderr)
                sys.exit(1)
            name = item.get("name")
            url = item.get("url")
            if not name or not url:
                print(f"[ERROR] hyperlinks[{idx}] is missing 'name' or 'url'", file=sys.stderr)
                sys.exit(1)
            built.append({"name": str(name), "url": str(url), "orderIndex": None})
        if built:
            gql_input["hyperlinks"] = built

    return gql_input


# ---------------------------------------------------------------------------
# GraphQL API call
# ---------------------------------------------------------------------------


def call_graphql(api_url: str, token: str, gql_input: dict) -> dict:
    """
    Execute the createChange GraphQL mutation.
    Returns the `createChange` result dict on success.
    Prints errors and calls sys.exit(1) on failure.
    """
    payload = {
        "query": GRAPHQL_MUTATION,
        "variables": {"input": gql_input},
    }
    body = json.dumps(payload).encode("utf-8")

    req = urllib.request.Request(
        url=api_url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
    )

    print(f"[INFO] Calling Support Platform API: {api_url}")
    try:
        with urllib.request.urlopen(req) as resp:
            response_text = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        print(f"[ERROR] HTTP {exc.code} from Support API: {error_body}", file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as exc:
        print(f"[ERROR] Failed to reach Support API: {exc.reason}", file=sys.stderr)
        sys.exit(1)

    try:
        response_data = json.loads(response_text)
    except json.JSONDecodeError as exc:
        print(f"[ERROR] Invalid JSON response from Support API: {exc}", file=sys.stderr)
        print(f"[DEBUG] Raw response: {response_text[:500]}", file=sys.stderr)
        sys.exit(1)

    if "errors" in response_data:
        print("[ERROR] GraphQL errors returned by Support API:", file=sys.stderr)
        for err in response_data["errors"]:
            print(f"  - {err.get('message', err)}", file=sys.stderr)
        sys.exit(1)

    change_data = response_data.get("data", {}).get("createChange")
    if not change_data:
        print(f"[ERROR] Unexpected API response — 'createChange' missing: {response_text[:500]}", file=sys.stderr)
        sys.exit(1)

    return change_data


# ---------------------------------------------------------------------------
# GitHub comment helpers
# ---------------------------------------------------------------------------


def github_api_request(method: str, url: str, token: str, body: Optional[dict] = None) -> Any:
    """
    Make a GitHub REST API request. Returns parsed JSON on success.
    Raises urllib.error.HTTPError / URLError on failure.
    """
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url=url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "User-Agent": "change-manager-workflow-bot/1.0",
        },
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode("utf-8"))


def build_comment_body(change: dict) -> str:
    """Format the PR comment from the createChange result."""
    return (
        f"{COMMENT_MARKER}\n"
        "✅ **Change created in Support Platform**\n\n"
        "| Field | Value |\n"
        "|-------|-------|\n"
        f"| **ID** | `{change['id']}` |\n"
        f"| **Title** | {change['title']} |\n"
        f"| **Starts** | {change['startedOn']} |\n"
        f"| **Ends** | {change['endedOn']} |\n\n"
        "> 🤖 This change was automatically created from the `change-request` block in this PR description."
    )


def upsert_pr_comment(repo: str, pr_number: str, token: str, body: str) -> None:
    """
    Find an existing bot comment (identified by COMMENT_MARKER) on the PR and
    update it, or create a new one if none exists.
    Non-fatal: prints a warning and returns instead of calling sys.exit.
    """
    base_url = f"https://api.github.com/repos/{repo}/issues/{pr_number}/comments"

    # 1. Fetch existing comments (paginated — handle up to 10 pages of 100)
    existing_comment_id: Optional[int] = None
    page = 1
    while True:
        list_url = f"{base_url}?per_page=100&page={page}"
        try:
            comments = github_api_request("GET", list_url, token)
        except (urllib.error.HTTPError, urllib.error.URLError) as exc:
            print(f"[WARNING] Could not list PR comments: {exc}", file=sys.stderr)
            return

        if not comments:
            break

        for comment in comments:
            if COMMENT_MARKER in comment.get("body", ""):
                existing_comment_id = comment["id"]
                break

        if existing_comment_id is not None or len(comments) < 100:
            break
        page += 1

    # 2. Update or create
    try:
        if existing_comment_id is not None:
            update_url = f"https://api.github.com/repos/{repo}/issues/comments/{existing_comment_id}"
            print(f"[INFO] Updating existing bot comment (id={existing_comment_id}) on PR #{pr_number}")
            github_api_request("PATCH", update_url, token, {"body": body})
            print("[INFO] Comment updated successfully.")
        else:
            print(f"[INFO] Creating new bot comment on PR #{pr_number}")
            github_api_request("POST", base_url, token, {"body": body})
            print("[INFO] Comment created successfully.")
    except (urllib.error.HTTPError, urllib.error.URLError) as exc:
        print(f"[WARNING] Could not post/update PR comment: {exc}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    # -- Read environment variables --
    pr_body = os.environ.get("PR_BODY", "")
    pr_number = os.environ.get("PR_NUMBER", "")
    repo = os.environ.get("REPO", "")
    github_token = os.environ.get("GITHUB_TOKEN", "")
    support_api_token = os.environ.get("SUPPORT_API_TOKEN", "")
    support_api_url = os.environ.get("SUPPORT_API_URL", DEFAULT_SUPPORT_API_URL)

    # -- Extract the change-request block --
    print("[INFO] Scanning PR body for a change-request block...")
    yaml_block = extract_change_request_block(pr_body)
    if yaml_block is None:
        print("[INFO] No change-request block found in PR body. Nothing to do.")
        sys.exit(0)

    print("[INFO] Found change-request block. Parsing YAML...")

    # -- Parse YAML --
    try:
        parsed = parse_yaml(yaml_block)
    except Exception as exc:  # noqa: BLE001
        print(f"[ERROR] Failed to parse change-request YAML: {exc}", file=sys.stderr)
        sys.exit(1)

    # -- Validate and build GraphQL input --
    print("[INFO] Validating fields...")
    gql_input = validate_fields(parsed)

    # -- Check required env vars for the API call --
    if not support_api_token:
        print("[ERROR] SUPPORT_API_TOKEN environment variable is not set.", file=sys.stderr)
        sys.exit(1)

    # -- Call the GraphQL API --
    change = call_graphql(support_api_url, support_api_token, gql_input)
    print(f"[INFO] Change created successfully! id={change['id']}, title={change['title']!r}")

    # -- Post/update GitHub PR comment --
    if not github_token:
        print("[WARNING] GITHUB_TOKEN not set — skipping PR comment.", file=sys.stderr)
        sys.exit(0)

    if not repo or not pr_number:
        print("[WARNING] REPO or PR_NUMBER not set — skipping PR comment.", file=sys.stderr)
        sys.exit(0)

    comment_body = build_comment_body(change)
    upsert_pr_comment(repo, pr_number, github_token, comment_body)

    print("[INFO] Done.")
    sys.exit(0)


if __name__ == "__main__":
    main()
