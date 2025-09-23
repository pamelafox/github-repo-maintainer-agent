# GitHub Repository Maintenance Agent

This repository provides an AI-powered agent for triaging failed Dependabot pull requests across your GitHub repositories. The agent uses [Pydantic AI](https://ai.pydantic.dev/) for LLM-based decisions and the GitHub API for repository, PR, and issue management.

* [Features](#features)
* [Getting started](#getting-started)
* [Configuring GitHub access](#configuring-github-access)
* [Configuring Azure OpenAI](#configuring-azure-openai)
* [Running the agent](#running-the-agent)
* [Resources](#resources)

## Features

The agent can...

- Find all repositories where you are an owner, maintainer, or collaborator (optionally filtered by organization)
- For each open Dependabot PR with a failed check, create a new actionable issue
- **Check for specific code patterns** in repository files and create issues when matches are found
- Assign issues to GitHub Copilot (if available)
- Avoid duplicate issues for the same PR or code pattern
- Log all actions for transparency

It includes...

- **Organization filtering:** Use the `--org` flag to process only repos in a specific organization (e.g., Azure-Samples)
- **Repository targeting:** Use the `--repo` flag to process only a specific repository by name
- **YAML configuration:** Use the `--repos-yaml` flag to specify a YAML file that lists personal and organization repositories to process
- **Dry-run mode:** Use the `--dry-run` flag to preview actions without making changes
- **Rich logging:** See which repos and PRs are processed, and which issues are created or skipped

## Getting started

You have a few options for getting started with this repository.
The quickest way to get started is GitHub Codespaces, since it will setup everything for you, but you can also [set it up locally](#local-environment).

### GitHub Codespaces

You can run this repository virtually by using GitHub Codespaces. The button will open a web-based VS Code instance in your browser:

1. Open the repository (this may take several minutes):

    [![Open in GitHub Codespaces](https://github.com/codespaces/badge.svg)](https://codespaces.new/Azure-Samples/python-ai-agent-frameworks-demos)

2. Open a terminal window
3. Continue with the steps to run the examples

### VS Code Dev Containers

A related option is VS Code Dev Containers, which will open the project in your local VS Code using the [Dev Containers extension](https://marketplace.visualstudio.com/items?itemName=ms-vscode-remote.remote-containers):

1. Start Docker Desktop (install it if not already installed)
2. Open the project:

    [![Open in Dev Containers](https://img.shields.io/static/v1?style=for-the-badge&label=Dev%20Containers&message=Open&color=blue&logo=visualstudiocode)](https://vscode.dev/redirect?url=vscode://ms-vscode-remote.remote-containers/cloneInVolume?url=https://github.com/Azure-Samples/python-ai-agent-frameworks-demos)

3. In the VS Code window that opens, once the project files show up (this may take several minutes), open a terminal window.
4. Continue with the steps to run the examples

### Local environment

1. Make sure the following tools are installed:

    - [Python 3.10+](https://www.python.org/downloads/)
    - Git

2. Clone the repository:

    ```shell
    git clone https://github.com/Azure-Samples/github-repo-maintainer-agent
    cd github-repo-maintainer-agent
    ```

3. Set up a virtual environment:

    ```shell
    python -m venv venv
    source venv/bin/activate  # On Windows: venv\Scripts\activate
    ```

4. Install the requirements:

    ```shell
    pip install -r requirements.txt
    ```

## Configuring GitHub access

1. Go to your GitHub account settings.
2. Click on "Developer settings" in the left sidebar.
3. Click on "Personal access tokens" in the left sidebar.
4. Click on "Fine-grained tokens" .
5. Click on "Generate new token".
6. Give your token a name and give it access to your repositories. For this project, you need to select the following permissions:
   - `Contents` > `Read and write`
   - `Pull requests` > `Read`
   - `Issues` > `Read and write`
   - `Models`: If you want to use GitHub Models instead of Azure OpenAI for any LLM calls.

7. Click on "Generate token".
8. Copy the generated token into the `.env` file or export it as an environment variable as `GITHUB_TOKEN`.

## Configuring Azure OpenAI

This agent optionally uses an LLM to analyze failed CI runs and create issues. You can use free GitHub Models for this or deploy your own Azure OpenAI instance.

This project includes infrastructure as code (IaC) to provision an Azure OpenAI deployment of "gpt-4o". The IaC is defined in the `infra` directory and uses the Azure Developer CLI to provision the resources.

1. Make sure the [Azure Developer CLI (azd)](https://aka.ms/install-azd) is installed.

2. Login to Azure:

    ```shell
    azd auth login
    ```

    For GitHub Codespaces users, if the previous command fails, try:

   ```shell
    azd auth login --use-device-code
    ```

3. Provision the OpenAI account:

    ```shell
    azd provision
    ```

    It will prompt you to provide an `azd` environment name (like "agents-demos"), select a subscription from your Azure account, and select a location. Then it will provision the resources in your account.

4. Once the resources are provisioned, you should now see a local `.env` file with all the environment variables needed to run the scripts.

5. To delete the resources, run:

    ```shell
    azd down
    ```

## Check your environment

You should now have a `.env` file with the following variables:

    - `GITHUB_TOKEN` (required): A GitHub personal access token with access to your repositories. You can create one in your GitHub account settings under Developer settings > Personal access tokens. Make sure it can read Pull requests, Commits, and can read/write issues.

### Organization-specific GitHub tokens

For improved security when working with multiple organizations, you can configure organization-specific GitHub tokens. The agent will automatically select the appropriate token based on the organization you're targeting:

    - `GITHUB_TOKEN`: Default token for personal repositories and general use
    - `GITHUB_TOKEN_<ORG_NAME>`: Token specific to an organization (replace `<ORG_NAME>` with the actual organization name in uppercase)

**Examples:**
    - `GITHUB_TOKEN_AZURE_SAMPLES`: Token specifically for Azure-Samples organization repositories
    - `GITHUB_TOKEN_MICROSOFT`: Token specifically for Microsoft organization repositories

When you use `--org Azure-Samples`, the agent will automatically use `GITHUB_TOKEN_AZURE_SAMPLES` if it exists, falling back to the default `GITHUB_TOKEN` if not found. This allows you to use different tokens with different permission scopes for different organizations.

For Azure OpenAI usage, you must also set:

    - `AZURE_OPENAI_ENDPOINT`: Your Azure OpenAI endpoint URL (e.g. `https://<your-resource>.openai.azure.com`)
    - `AZURE_OPENAI_DEPLOYMENT`: The deployment name for your model (e.g. `gpt-4o`)
    - `AZURE_OPENAI_MODEL` (optional): The model name (e.g. `gpt-4o`)

See the output of the Azure provisioning step or the comments in `infra/` for the exact variable names and values to use.

## Running the agent

The agent supports two main commands:

### Dependabot Command (Default)

Check Dependabot PRs and create issues for failures. This is the default behavior when no command is specified.

**Basic syntax:**

```sh
python agent.py [dependabot] [OPTIONS]
```

**Common options:**

* `--dry-run`: Log actions without making changes
* `--exclude-archived`: Exclude archived repositories (default: True)
* `--repo REPO_NAME`: Target specific repository by name
* `--org ORG_NAME`: Only include repos in this organization (e.g. Azure-Samples)
* `--repos-yaml YAML_PATH`: Path to YAML file that lists repositories to process

**Examples:**

* Dry run for all repos you own or maintain:

  ```sh
  python agent.py --dry-run
  # or explicitly:
  python agent.py dependabot --dry-run
  ```

* Process only repos in a particular GitHub organization:

  ```sh
  python agent.py --org Azure-Samples
  ```

* Process a specific repository:

  ```sh
  python agent.py --repo rag-postgres-openai-python
  ```

* Process a specific repository in a specific organization:

  ```sh
  python agent.py --org Azure-Samples --repo rag-postgres-openai-python
  ```

* Process repos listed in a YAML configuration file:

  ```sh
  python agent.py --repos-yaml repos.yaml
  ```

### Code Pattern Checking Command

Check for specific code patterns in repository files and create issues when matches are found.

**Basic syntax:**

```sh
python agent.py code-check --config CONFIG_FILE [OPTIONS]
```

**Required options:**

* `--config CONFIG_FILE`: Path to YAML file containing code check configurations

**Examples:**

* Check code patterns using a configuration file:

  ```sh
  python agent.py code-check --config code_checks.yaml
  ```

* Dry run with code pattern checking:

  ```sh
  python agent.py code-check --config code_checks.yaml --dry-run
  ```

* Check code patterns only in Azure-Samples organization:

  ```sh
  python agent.py code-check --config code_checks.yaml --org Azure-Samples
  ```

This mode will:

1. Load code check configurations from a YAML file
2. For each repository, check specified files for pattern matches
3. Create issues when patterns are found (avoiding duplicates)
4. Support both regex patterns and literal string matching

#### Code Check Configuration

Create a YAML file (e.g., `code_checks.yaml`) with the following structure:

```yaml
code_checks:
  - file_path: "requirements.txt"
    pattern: "flask==1\\..*"
    issue_title: "Outdated Flask version detected"
    issue_description: |
      This repository is using an outdated version of Flask (v1.x). 
      Please consider upgrading to Flask 2.x or later.
    labels:
      - "dependencies"
      - "security"
    assignees:
      - "copilot-swe-agent"
```

Each code check configuration includes:

* `file_path`: Path to a specific file to check (relative to repository root)
* `directory_path`: Path to a directory to check all files within (alternative to file_path)
* `file_pattern`: Regex pattern to filter filenames when using directory_path (optional)
* `search_repo`: If true, search entire repository using GitHub's search API (alternative to file_path/directory_path)
* `pattern`: Regex pattern or literal string to search for
* `issue_title`: Title for the issue to create when pattern is found
* `issue_description`: Description for the issue
* `labels`: List of labels to apply to the issue (optional)
* `assignees`: List of users to assign the issue to (optional)

**Note:** Must specify exactly one of: `file_path`, `directory_path`, or `search_repo: true`.

Examples:

* Check a specific file: Use `file_path: "requirements.txt"`
* Check all files in a directory: Use `directory_path: ".github/workflows"`
* Check only YAML files in a directory: Use `directory_path: ".github/workflows"` and `file_pattern: "\\.ya?ml$"`
* Search entire repository: Use `search_repo: true` (uses GitHub's search API for fast repository-wide searches)

An example configuration file is provided at `code_checks.yaml.example`.

You can ask GitHub Copilot to write a code check YAML based off an existing issue using a prompt like:

> Add a new code check file based off #file:code_checks.yaml.example that will look for azure.yaml files with pipeline section in them. If so, it should create an issue like this one #fetch https://github.com/Azure-Samples/openai-chat-app-quickstart/issues/327

### Using a YAML configuration file

You can specify repositories to process using a YAML file with the following structure:

```yaml
# Personal repositories to process (under your GitHub username)
personal:
  - sample-repo
  - another-sample-repo

# Organization repositories to process
organizations:
  # Organization name
  my-organization:
    - org-repo1
    - org-repo2
  
  # Another organization
  another-org:
    - project-repo
    - another-project-repo
```

An example configuration file is provided at `repos.yaml.example`. Create your own `repos.yaml` file based on this example.

## How it works

### Dependabot Command

1. Lists all repositories you own, maintain, or collaborate on (optionally filtered by org or pattern)
2. For each repo, finds open Dependabot PRs
3. For each PR, checks for failed CI runs
4. If a failed PR does not already have a triage issue, creates one and assigns Copilot (if available)
5. Logs all actions and skips duplicates

### Code-Check Command

1. Load code check configurations from a YAML file
2. For each repository, check specified files or directories for pattern matches
3. Create issues when patterns are found (avoiding duplicates)
4. Support both regex patterns and literal string matching

## Resources

* [Pydantic AI Documentation](https://ai.pydantic.dev/)
* [GitHub REST API Docs](https://docs.github.com/en/rest)
* [GitHub GraphQL API Docs](https://docs.github.com/en/graphql)
