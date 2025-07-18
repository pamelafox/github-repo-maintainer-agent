
# GitHub Repository Maintenance Agent

This repository provides an AI-powered agent for triaging failed Dependabot pull requests across your GitHub repositories. The agent uses [Pydantic AI](https://ai.pydantic.dev/) for LLM-based decisions and the GitHub API for repository, PR, and issue management. It can:

- Find all repositories where you are an owner, maintainer, or collaborator (optionally filtered by organization)
- For each open Dependabot PR with a failed check, create a new actionable issue
- Assign the issue to GitHub Copilot (if available)
- Avoid duplicate issues for the same PR
- Log all actions for transparency

- [Features](#features)
- [Getting started](#getting-started)
  - [GitHub Codespaces](#github-codespaces)
  - [VS Code Dev Containers](#vs-code-dev-containers)
  - [Local environment](#local-environment)
- [Configuring GitHub access](#configuring-github-access)
- [Configuring Azure OpenAI](#configuring-azure-openai)
- [Running the agent](#running-the-agent)
- [Resources](#resources)

## Features

- **Organization filtering:** Use the `--org` flag to process only repos in a specific organization (e.g., Azure-Samples)
- **Pattern filtering:** Use the `--filter-pattern` flag to process only repos matching a name pattern
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

For Azure OpenAI usage, you must also set:

    - `AZURE_OPENAI_ENDPOINT`: Your Azure OpenAI endpoint URL (e.g. `https://<your-resource>.openai.azure.com`)
    - `AZURE_OPENAI_DEPLOYMENT`: The deployment name for your model (e.g. `gpt-4o`)
    - `AZURE_OPENAI_MODEL` (optional): The model name (e.g. `gpt-4o`)
    - `AZURE_OPENAI_API_VERSION` (optional): The API version (e.g. `2024-02-15-preview`)

See the output of the Azure provisioning step or the comments in `infra/` for the exact variable names and values to use.

## Running the agent

Run the agent with various options:

```sh
python agent.py [--dry-run] [--org ORG_NAME] [--filter-pattern PATTERN]
```

Examples:

- Dry run for all repos you own or maintain:

  ```sh
  python agent.py --dry-run
  ```

- Process only repos in a particular GitHub organization:

  ```sh
  python agent.py --org Your-Org-Name
  ```

- Process only repos matching a pattern:

  ```sh
  python agent.py --filter-pattern your-repo-name
  ```

This is how it works:

1. Lists all repositories you own, maintain, or collaborate on (optionally filtered by org or pattern)
2. For each repo, finds open Dependabot PRs
3. For each PR, checks for failed CI runs
4. If a failed PR does not already have a triage issue, creates one and assigns Copilot (if available)
5. Logs all actions and skips duplicates

## Resources

- [Pydantic AI Documentation](https://ai.pydantic.dev/)
- [GitHub REST API Docs](https://docs.github.com/en/rest)
- [GitHub GraphQL API Docs](https://docs.github.com/en/graphql)