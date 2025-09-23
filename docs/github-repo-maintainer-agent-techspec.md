# GitHub Repository Maintainer Agent - Technical Specification

## Overview

This document describes a step-by-step implementation plan for the GitHub Repository Maintainer Agent defined in `docs/github-repo-maintainer-agent-prd.md`. The agent discovers owned repositories, identifies failing Dependabot PRs, analyzes failures, and creates actionable issues.

This spec provides all details an LLM needs to implement the feature without reading the PRD, including modules, data models, API interactions, LLM integration using Pydantic AI, configuration, and workflow logic. No source code is included.

## Assumptions

- Python 3.10+ environment with `pyproject.toml`/`requirements.txt` managing dependencies.
- GitHub Personal Access Token or App credentials provided via environment variables.
- OpenAI API key provided via environment variable.
- Use of `PyGithub` for GitHub REST SDK or `httpx` for direct REST calls if PyGithub unavailable.
- Use of `pydantic` for data models and `pydantic-ai` (Pydantic AI) for structured LLM outputs.
- Agent executed as a CLI command `repo_manager.py run` or similar.

## Architecture Overview

- **CLI Entrypoint**: parse arguments (e.g., `--dry-run`, repo filters)
- **GitHub Client Module**: encapsulate GitHub API calls
- **LLM Client Module**: encapsulate OpenAI chat and function calls via Pydantic AI
- **Core Agent Module**: orchestrates discovery, analysis, and issue creation
- **Data Models**: Pydantic classes for repositories, pull requests, check runs, failure analysis, issue payloads
- **Logging and Metrics**: structured logging and timing metrics

## Modules and Components

1. **github_client.py**  

2. **models.py**  
   - `class GitHubClient` wrapping:
     - `list_owned_repos()` pagination handling
     - `list_dependabot_prs(repo)` filtering by `author='dependabot[bot]'`
     - `get_pr_check_runs(repo, pr_number)` retrieving check runs via REST or SDK
     - `create_issue(repo, IssuePayload)` using SDK or REST
     - `assign_issue(repo, issue_number, assignees)`
     - `find_existing_issues(repo, pr_number)`
     - All methods return Pydantic models

3. **llm_client.py**  
   - `Repository`  with `name`, `owner`, `archived`  
   - `PullRequest`  with `number`, `author`, `url`  
   - `CheckRun`  with `name`, `status`, `conclusion`, `url`  
   - `FailureAnalysis`  with `type`, `summary`, `related_logs`  
   - `IssuePayload`  with `title`, `body`, `labels`, `assignees`  

4. **agent.py**  
   - `class LLMClient` using Pydantic AI:
     - Define function schema model: `AnalyzeFailureInput` and `AnalyzeFailureOutput`
     - Call `OpenAI().chat.completions.create(...)` with `functions=[schema]` to categorize failures
     - Parse response into `FailureAnalysis` model

5. **cli.py**  
   - `class RepoMaintainerAgent` orchestrating:
     1. Discover repositories via `GitHubClient.list_owned_repos()`
     2. Filter out archived or excluded repos
     3. For each repo:
        - List Dependabot PRs
        - For each PR:
          - Fetch check runs
          - Identify failures by conclusion/status
          - If failures exist:
            - Analyze with `LLMClient.analyze_failure(...)`
            - Check for existing issue or duplicate
            - Construct `IssuePayload` using analysis and PR context
            - Create or update issue
            - Assign to Copilot user

6. **logger.py**  

7. **cli.py**  
   - Use `argparse` to parse flags (`--dry-run`, `--exclude-archived`, `--filter-pattern`) and invoke `RepoMaintainerAgent.run()`

## Data Models (Pydantic)

- **Repository**  
  - `name: str`  
  - `owner: str`  
  - `archived: bool`

- **PullRequest**  
  - `number: int`  
  - `author: str`  
  - `url: HttpUrl`

- **CheckRun**  
  - `name: str`  
  - `status: Literal['queued','in_progress','completed']`  
  - `conclusion: Literal['success','failure','cancelled','timed_out','action_required']`

- **AnalyzeFailureInput**  
  - `pr_url: HttpUrl`  
  - `check_runs: list[CheckRun]`

- **AnalyzeFailureOutput**  
  - `type: Literal['dependency_conflict','build_error','test_failure','install_error','other']`  
  - `summary: str`  
  - `related_logs: list[str]`

- **IssuePayload**  
  - `title: str`  
  - `body: str`  
  - `labels: list[str]`  
  - `assignees: list[str]`

## Workflow Steps

1. **Initialize Config & Clients**  
   - Load config, set `dry_run` flag  
   - Instantiate `GitHubClient(token)`, `LLMClient(api_key)`

2. **Repository Discovery**  
   - Call `github_client.list_owned_repos()`  
   - Filter by `!repo.archived` and user-provided patterns

3. **Dependabot PR Enumeration**  
   - For each repo, call `list_dependabot_prs(repo)`
   - Skip if empty

4. **Check Run Analysis**  
   - For each PR, call `get_pr_check_runs(repo, pr.number)`
   - Filter runs: `conclusion in ['failure','cancelled','timed_out']`
   - Skip PRs with no failing runs

5. **LLM-Powered Failure Analysis**  
   - Prepare `AnalyzeFailureInput` with check run details
   - Call `llm_client.analyze_failure(model='gpt-4o', input=model_instance)`
   - Receive `AnalyzeFailureOutput`

6. **Issue Management**  
   - Call `github_client.find_existing_issues(repo, pr.number)`
   - If none, build `IssuePayload`:
     - Title: `[Dependabot][{repo.name}#{pr.number}] {analysis.type}`
     - Body: include PR link, analysis.summary, instructions, related logs
     - Labels: `['dependabot-agent']` plus failure type
     - Assignees: `['github-copilot']`
   - Call `github_client.create_issue(repo, payload)` (or `update_issue` if duplicate)

7. **Assign and Log**  
   - Assign issue if created
   - Log successful creation or update with metrics

8. **Dry-Run Support**  
   - If `--dry-run`, log actions instead of executing API calls

## Configuration and Environment

- **Environment Variables**:
  - API_HOST=azure
    AZURE_TENANT_ID=
    AZURE_OPENAI_SERVICE=
    AZURE_OPENAI_ENDPOINT=
    AZURE_OPENAI_CHAT_DEPLOYMENT=gpt-4o
    AZURE_OPENAI_CHAT_MODEL=gpt-4o

-- **Config File** (YAML/JSON):
  
  ```yaml
  exclude_archived: true
  include_pattern: '.*'
  dry_run: false
  labels:
    - 'dependabot-agent'
  assignee: 'github-copilot'
  ```

## CLI and Execution Flow

1. `repo_manager.py run [--dry-run] [--filter <pattern>]`
2. Instantiate clients
3. Run `RepoMaintainerAgent.process_all()`
4. Summary report printed: repos scanned, PRs checked, issues created/updated

---

This technical spec ensures every acceptance criterion is addressed and provides a clear blueprint for implementation using Pydantic AI, structured data models, and recommended Python libraries.
