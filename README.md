
# GitHub Repository Maintenance Agent

This repository provides an AI-powered agent for triaging failed Dependabot pull requests across your GitHub repositories. The agent uses [Pydantic AI](https://ai.pydantic.dev/) for LLM-based decisions and the GitHub API for repository, PR, and issue management. It can:

- Find all repositories where you are an owner, maintainer, or collaborator (optionally filtered by organization)
- For each open Dependabot PR with a failed check, create a new actionable issue
- Assign the issue to GitHub Copilot (if available)
- Avoid duplicate issues for the same PR
- Log all actions for transparency

## Features

- **Organization filtering:** Use the `--org` flag to process only repos in a specific organization (e.g., Azure-Samples)
- **Pattern filtering:** Use the `--filter-pattern` flag to process only repos matching a name pattern
- **Dry-run mode:** Use the `--dry-run` flag to preview actions without making changes
- **Rich logging:** See which repos and PRs are processed, and which issues are created or skipped

## Getting Started

### Prerequisites

- [Python 3.10+](https://www.python.org/downloads/)
- [Git](https://git-scm.com/)

### Setup

1. Clone the repository:

    ```sh
    git clone https://github.com/pamelafox/github-repo-maintainer-agent.git
    cd github-repo-maintainer-agent
    ```

2. Create and activate a virtual environment:

    ```sh
    python -m venv .venv
    source .venv/bin/activate  # On Windows: .venv\Scripts\activate
    ```

3. Install dependencies:

    ```sh
    pip install -r requirements.txt
    ```

4. Set up your environment variables:

    - Create a `.env` file or export the following in your shell:

      - `GITHUB_TOKEN` (required): A GitHub personal access token with access to your repositories. You can create one in your GitHub account settings under Developer settings > Personal access tokens. Make sure it can read Pull requests, Commits, and can read/write issues.

    For Azure OpenAI usage, you must also set:
      - `AZURE_OPENAI_ENDPOINT`: Your Azure OpenAI endpoint URL (e.g. `https://<your-resource>.openai.azure.com`)
      - `AZURE_OPENAI_DEPLOYMENT`: The deployment name for your model (e.g. `gpt-4o`)
      - `AZURE_OPENAI_MODEL` (optional): The model name (e.g. `gpt-4o`)
      - `AZURE_OPENAI_API_VERSION` (optional): The API version (e.g. `2024-02-15-preview`)

    See the output of the Azure provisioning step or the comments in `infra/` for the exact variable names and values to use.

## Usage

Run the agent with various options:

```sh
python agent.py [--dry-run] [--org ORG_NAME] [--filter-pattern PATTERN]
```

Examples:

- Dry run for all repos you own or maintain:
  ```sh
  python agent.py --dry-run
  ```
- Process only repos in the Azure-Samples org:
  ```sh
  python agent.py --org Azure-Samples
  ```
- Process only repos matching a pattern:
  ```sh
  python agent.py --filter-pattern openai-chat-app
  ```

## How It Works

1. Lists all repositories you own, maintain, or collaborate on (optionally filtered by org or pattern)
2. For each repo, finds open Dependabot PRs
3. For each PR, checks for failed CI runs
4. If a failed PR does not already have a triage issue, creates one and assigns Copilot (if available)
5. Logs all actions and skips duplicates

## Azure OpenAI Integration

If you want to use Azure OpenAI for LLM-based triage, provision resources using the Bicep files in the `infra/` directory and set the appropriate environment variables in your `.env` file. See the comments in `infra/` for details.

## Resources

- [Pydantic AI Documentation](https://ai.pydantic.dev/)
- [GitHub REST API Docs](https://docs.github.com/en/rest)
- [GitHub GraphQL API Docs](https://docs.github.com/en/graphql)

