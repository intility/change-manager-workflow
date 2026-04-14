# change-manager-workflow

Reusable GitHub Actions workflow that creates a [Support Platform](https://support-api.apps.aa.intility.com/graphql) change when a PR contains a `change-request` block.

## Usage

Copy `.github/workflows/caller-template.yml` into your gitops repo and rename it:

```yaml
jobs:
  create-change:
    uses: intility/change-manager-workflow/.github/workflows/create-change.yml@main
    secrets: inherit
    with:
      pr-body:   ${{ github.event.pull_request.body }}
      pr-number: ${{ github.event.pull_request.number }}
      pr-title:  ${{ github.event.pull_request.title }}
      pr-url:    ${{ github.event.pull_request.html_url }}
      pr-author: ${{ github.event.pull_request.user.login }}
      repo:      ${{ github.repository }}
```

## PR format

Add a `change-request` block to the PR description. Remove it entirely for PRs that are not tracked changes.

~~~markdown
```change-request
title: "Short title (4–60 chars)"

# Owner — pick one, or omit to default to the PR author
owner_upn: "john.doe@intility.com"
# owner_guid: "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"

started_on: "2026-04-20T22:00:00Z"
ended_on:   "2026-04-20T23:00:00Z"
impacts_all: false

# Optional
# description: "..."
# assets:
#   - "14ea6945-a8ec-4cd9-8ae8-a3d8c0778a2c"  # Intility Developer Platform
# tickets:
#   reference_numbers: [12345]
# hyperlinks:
#   - name: "Runbook"
#     url: "https://wiki.example.com/runbook"
```
~~~

On PR open or edit the workflow creates the change and posts a comment with the change ID.

## Secrets

Set once at org level — calling repos need nothing:

| Secret | Description |
|--------|-------------|
| `CHANGE_WORKFLOW_AZURE_TENANT_ID` | Azure AD tenant ID |
| `CHANGE_WORKFLOW_AZURE_CLIENT_ID` | App registration client ID |
| `CHANGE_WORKFLOW_AZURE_CLIENT_SECRET` | App registration client secret |

The app registration needs **application permissions** on the Support Platform API (`api://6563f833-21a5-4b8a-90ee-37a36cf8f667`): `Processes.Modify` and `Users.Read`, both with admin consent.

## Optional inputs

| Input | Default |
|-------|---------|
| `support-api-url` | `https://support-api.apps.aa.intility.com/graphql` |
| `support-api-scope` | `api://6563f833-21a5-4b8a-90ee-37a36cf8f667/.default` |
