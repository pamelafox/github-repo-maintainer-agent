import io
import logging
import os
import re
import zipfile
from pathlib import Path
from typing import Any

import httpx
import yaml
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from models import CheckRun, CodeMatchResult, DirectoryItem, FileContent, IssuePayload, PullRequest, RepositoriesConfig, Repository

logger = logging.getLogger("repo_maintainer_agent")
logger.setLevel(logging.INFO)

GITHUB_GRAPHQL_URL = "https://api.github.com/graphql"

GITHUB_API_URL = "https://api.github.com"


class GitHubRateLimitError(Exception):
    """Exception raised when GitHub API rate limit is exceeded."""
    pass


def should_retry_github_search(exception):
    """Determine if we should retry a GitHub search API call based on the exception."""
    # Only retry on 403 rate limit errors
    if isinstance(exception, httpx.HTTPStatusError):
        return exception.response.status_code == 403
    return False


def get_github_token_for_org(org: str | None = None) -> str:
    """Get the appropriate GitHub token for the given organization.
    
    Args:
        org: The organization name (e.g., "Azure-Samples"). If None, uses personal token.
        
    Returns:
        The GitHub token to use.
        
    Raises:
        ValueError: If the required token is not found in environment variables.
    """
    if org is None:
        # Use personal token
        token_key = "GITHUB_TOKEN_PERSONAL"
        if token_key not in os.environ:
            # Fallback to generic GITHUB_TOKEN
            token_key = "GITHUB_TOKEN"
            if token_key not in os.environ:
                raise ValueError("GITHUB_TOKEN_PERSONAL or GITHUB_TOKEN environment variable is required for personal repositories")
    else:
        # Use organization-specific token
        # Convert org name to environment variable format (e.g., Azure-Samples -> AZURE_SAMPLES)
        org_env_name = org.upper().replace("-", "_")
        token_key = f"GITHUB_TOKEN_{org_env_name}"
        if token_key not in os.environ:
            # Fallback to generic GITHUB_TOKEN
            token_key = "GITHUB_TOKEN"
            if token_key not in os.environ:
                raise ValueError(f"{token_key} or GITHUB_TOKEN environment variable is required for organization '{org}'")
    
    token = os.environ[token_key]
    if not token:
        raise ValueError(f"GitHub token '{token_key}' is empty")
    
    logger.info(f"Using GitHub token from {token_key} for {'personal repositories' if org is None else f'organization: {org}'}")
    return token

class GitHubClient:
    async def get_repo_and_copilot_ids(self, repo):
        """Fetch the repository node ID and Copilot's node ID for assignment via GraphQL."""
        headers = {"Authorization": f"Bearer {self.auth_token}", "Accept": "application/vnd.github+json"}
        # 1. Get repo ID and Copilot's node ID
        query = '''
        query($owner: String!, $name: String!) {
          repository(owner: $owner, name: $name) {
            id
            suggestedActors(capabilities: [CAN_BE_ASSIGNED], first: 100) {
              nodes {
                login
                __typename
                ... on Bot { id }
              }
            }
          }
        }
        '''
        variables = {"owner": repo.owner, "name": repo.name}
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(GITHUB_GRAPHQL_URL, headers=headers, json={"query": query, "variables": variables})
                resp.raise_for_status()
                data = resp.json()
            repo_id = data["data"]["repository"]["id"]
            copilot_node = next((n for n in data["data"]["repository"]["suggestedActors"]["nodes"] if n["login"] == "copilot-swe-agent"), None)
            if not copilot_node or not copilot_node.get("id"):
                raise RuntimeError("Copilot is not assignable in this repository.")
            return repo_id, copilot_node["id"]
        except httpx.TimeoutException as e:
            logger.error(f"Timeout occurred while fetching repo and copilot IDs: {e}")
            raise RuntimeError(f"Failed to fetch repo and copilot IDs due to timeout: {e}")
        except httpx.HTTPStatusError as e:
            logger.error(f"HTTP error occurred while fetching repo and copilot IDs: {e}")
            raise RuntimeError(f"Failed to fetch repo and copilot IDs due to HTTP error: {e}")
        except Exception as e:
            logger.error(f"Unexpected error occurred while fetching repo and copilot IDs: {e}")
            raise RuntimeError(f"Failed to fetch repo and copilot IDs: {e}")

    async def create_issue_graphql(self, repo, issue):
        """Create an issue and assign Copilot using the GraphQL API, with improved error handling."""
        repo_id, copilot_id = await self.get_repo_and_copilot_ids(repo)
        headers = {"Authorization": f"Bearer {self.auth_token}", "Accept": "application/vnd.github+json"}
        mutation = '''
        mutation($input: CreateIssueInput!) {
          createIssue(input: $input) {
            issue {
              id
              number
              title
              url
            }
          }
        }
        '''
        input_obj = {
            "repositoryId": repo_id,
            "title": issue.title,
            "body": issue.body,
            "assigneeIds": [copilot_id],
            # labels can be added via labelIds, but requires label node IDs
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(GITHUB_GRAPHQL_URL, headers=headers, json={"query": mutation, "variables": {"input": input_obj}})
                resp.raise_for_status()
                data = resp.json()
            # Improved error handling
            if "errors" in data:
                logger.error(f"GraphQL error creating issue: {data['errors']}")
                raise RuntimeError(f"GraphQL error creating issue: {data['errors']}")
            issue_data = data.get("data", {}).get("createIssue", {}).get("issue")
            if not issue_data:
                logger.error(f"GraphQL createIssue returned no issue object: {data}")
                raise RuntimeError(f"GraphQL createIssue returned no issue object: {data}")
            return {
                "number": issue_data["number"],
                "html_url": issue_data["url"]
            }
        except httpx.TimeoutException as e:
            logger.error(f"Timeout occurred while creating issue via GraphQL: {e}")
            raise RuntimeError(f"Failed to create issue via GraphQL due to timeout: {e}")
        except httpx.HTTPStatusError as e:
            logger.error(f"HTTP error occurred while creating issue via GraphQL: {e}")
            raise RuntimeError(f"Failed to create issue via GraphQL due to HTTP error: {e}")
        except Exception as e:
            logger.error(f"Unexpected error occurred while creating issue via GraphQL: {e}")
            raise RuntimeError(f"Failed to create issue via GraphQL: {e}")
    def __init__(self, auth_token: str | None = None, org: str | None = None):
        """Initialize GitHubClient with appropriate token for the organization.
        
        Args:
            auth_token: Optional explicit token to use. If None, will auto-select based on org.
            org: Organization name (e.g., "Azure-Samples"). If None, uses personal token.
        """
        if auth_token is None:
            auth_token = get_github_token_for_org(org)
        
        self.auth_token = auth_token
        self.org = org
        self.headers = {
            "Authorization": f"Bearer {auth_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        # Default timeout of 30 seconds for regular API calls
        # For logs and other larger files, use a longer timeout of 60 seconds
        self.timeout = httpx.Timeout(30.0)
        self.long_timeout = httpx.Timeout(60.0)
        
    def _get_next_url_from_link_header(self, link_header: str) -> str | None:
        """Extract the 'next' URL from a GitHub API Link header.
        
        GitHub's REST API uses Link headers for pagination. This method extracts the URL
        for the next page from the Link header if it exists.
        
        Args:
            link_header: The value of the Link header from a GitHub API response
            
        Returns:
            The URL for the next page, or None if there is no next page
        """
        if not link_header:
            return None
        
        next_pattern = re.compile(r'<([^>]*)>;\s*rel="next"')
        next_url_match = next_pattern.search(link_header)
        
        if next_url_match:
            return next_url_match.group(1)  # Extract the URL for the next page
        
        return None

    async def list_repos_from_yaml(self, yaml_path: str) -> list[Repository]:
        """Load repositories from a YAML configuration file."""
        try:
            yaml_path_obj = Path(yaml_path)
            if not yaml_path_obj.exists():
                logger.warning(f"YAML file not found: {yaml_path}")
                return []
            
            with open(yaml_path_obj) as f:
                config_data = yaml.safe_load(f)
            
            config = RepositoriesConfig.model_validate(config_data)
            repositories = []
            
            # Process personal repositories
            if config.personal:
                logger.info(f"Processing {len(config.personal)} personal repositories")
                username = await self.get_authenticated_username()
                for repo_name in config.personal:
                    # Get repository info to check if archived
                    try:
                        repo_info = await self.get_repository_info(username, repo_name)
                        repositories.append(Repository(
                            name=repo_name,
                            owner=username,
                            archived=repo_info.get("archived", False),
                            is_personal=True  # Mark as personal repo
                        ))
                    except Exception as e:
                        logger.warning(f"Error fetching personal repository {repo_name}: {e}")
            
            # Process organization repositories
            if config.organizations:
                for org_name, repo_list in config.organizations.items():
                    logger.info(f"Processing {len(repo_list)} repositories from organization {org_name}")
                    for repo_name in repo_list:
                        try:
                            repo_info = await self.get_repository_info(org_name, repo_name)
                            repositories.append(Repository(
                                name=repo_name,
                                owner=org_name,
                                archived=repo_info.get("archived", False),
                                is_personal=False  # Mark as organization repo
                            ))
                        except Exception as e:
                            logger.warning(f"Error fetching repository {org_name}/{repo_name}: {e}")
            
            return repositories
        except Exception as e:
            logger.error(f"Error loading repositories from YAML: {e}")
            return []
    
    async def get_authenticated_username(self) -> str:
        """Get the username of the authenticated user."""
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.get(f"{GITHUB_API_URL}/user", headers=self.headers)
                resp.raise_for_status()
                data = resp.json()
                return data["login"]
        except Exception as e:
            logger.error(f"Error getting authenticated username: {e}")
            raise RuntimeError(f"Failed to get authenticated username: {e}")
    
    async def get_repository_info(self, owner: str, repo: str) -> dict:
        """Get information about a repository."""
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.get(f"{GITHUB_API_URL}/repos/{owner}/{repo}", headers=self.headers)
                resp.raise_for_status()
                return resp.json()
        except Exception as e:
            logger.error(f"Error getting repository info for {owner}/{repo}: {e}")
            raise RuntimeError(f"Failed to get repository info for {owner}/{repo}: {e}")

    async def list_owned_repos(self, org: str | None = None) -> list[Repository]:
        """List all repos owned by the authenticated user, or where user is a collaborator/maintainer. Optionally filter by org using /orgs/{org}/repos."""
        repos = []
        
        if org:
            # List all repos in the org, sorted by updated date in descending order
            url = f"{GITHUB_API_URL}/orgs/{org}/repos?per_page=100&sort=updated&direction=desc"
        else:
            # Get user's repositories (owned, collaborator, and organization member)
            url = f"{GITHUB_API_URL}/user/repos?affiliation=owner,collaborator,organization_member&per_page=100"
        
        # Calculate the cutoff date (one year ago)
        from datetime import datetime, timedelta, timezone
        cutoff_date = datetime.now(timezone.utc) - timedelta(days=10)  # Using 365 days for one year
        
        while url:
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    resp = await client.get(url, headers=self.headers)
                    resp.raise_for_status()
                    data = resp.json()
                    
                    # Flag to determine if we should stop fetching more pages
                    stop_fetching = False
                    
                    # Process repos from current page
                    for repo in data:
                        # Check if the repo is older than one year (for org repos)
                        if org and "updated_at" in repo:
                            updated_at = datetime.fromisoformat(repo["updated_at"].replace("Z", "+00:00"))
                            if updated_at < cutoff_date:
                                logger.info(f"Stopping fetch: found repo last updated at {updated_at}, which is more than a year ago")
                                stop_fetching = True
                                break
                        
                        # Only include if user has admin, maintain, or write permission
                        permissions = repo.get("permissions", {})
                        if permissions.get("admin") or permissions.get("maintain") or permissions.get("push"):                            
                            repos.append(Repository(
                                name=repo["name"],
                                owner=repo["owner"]["login"],
                                archived=repo["archived"],
                                is_personal=(org is None)  # If no org specified, these are personal repos
                            ))
                    
                    # If we found a repo older than the cutoff date, stop fetching more pages
                    if stop_fetching:
                        break
                    
                    # Check for Link header to see if there are more pages
                    url = self._get_next_url_from_link_header(resp.headers.get('link', ''))
                        
            except httpx.TimeoutException as e:
                logger.warning(f"Timeout occurred while fetching repos: {e}")
                break
            except httpx.HTTPStatusError as e:
                logger.warning(f"HTTP error occurred while fetching repos: {e}")
                break
            except Exception as e:
                logger.warning(f"Unexpected error occurred while fetching repos: {e}")
                break
                
        return repos

    async def list_dependabot_prs(self, repo: Repository) -> list[PullRequest]:
        """List all open PRs created by dependabot for a given repository."""
        prs = []
        url = f"{GITHUB_API_URL}/repos/{repo.owner}/{repo.name}/pulls?state=open&per_page=100"
        
        while url:
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    resp = await client.get(url, headers=self.headers)
                    resp.raise_for_status()
                    data = resp.json()
                    
                    # Process PRs from current page
                    for pr in data:
                        if pr["user"]["login"].startswith("dependabot"):
                            prs.append(PullRequest(
                                number=pr["number"], 
                                author=pr["user"]["login"], 
                                url=pr["html_url"], 
                                title=pr["title"]
                            ))
                    
                    # Check for Link header to see if there are more pages
                    url = self._get_next_url_from_link_header(resp.headers.get('link', ''))
                        
            except httpx.TimeoutException as e:
                logger.warning(f"Timeout occurred while fetching PRs from {repo.owner}/{repo.name}: {e}")
                break
            except httpx.HTTPStatusError as e:
                logger.warning(f"HTTP error occurred while fetching PRs from {repo.owner}/{repo.name}: {e}")
                break
            except Exception as e:
                logger.warning(f"Unexpected error occurred while fetching PRs from {repo.owner}/{repo.name}: {e}")
                break
                
        return prs

    async def get_pr_check_runs(self, repo: Repository, pr_number: int) -> list[CheckRun]:
        logger.info(f"Fetching check runs for PR #{pr_number} in {repo.owner}/{repo.name}")
        # Get the head SHA for the PR
        url_pr = f"{GITHUB_API_URL}/repos/{repo.owner}/{repo.name}/pulls/{pr_number}"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.get(url_pr, headers=self.headers)
                resp.raise_for_status()
                pr_data = resp.json()
                sha = pr_data["head"]["sha"]
        except httpx.TimeoutException as e:
            logger.warning(f"Timeout occurred while fetching PR #{pr_number}: {e}")
            return []
        except httpx.HTTPStatusError as e:
            logger.warning(f"HTTP error occurred while fetching PR #{pr_number}: {e}")
            return []
        except Exception as e:
            logger.warning(f"Unexpected error occurred while fetching PR #{pr_number}: {e}")
            return []
        
        # Get check runs for the SHA
        url = f"{GITHUB_API_URL}/repos/{repo.owner}/{repo.name}/commits/{sha}/check-runs"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.get(url, headers=self.headers)
                resp.raise_for_status()
                data = resp.json()
                
                runs = []
                # Flag to track if we've already found logs for a failed check run
                found_logs_for_failed_check = False
                
                # Process check runs in order, but prioritize finding logs for a failed check
                for run in data.get("check_runs", []):
                    logger.info(f"Found check run: {run['name']} with status={run['status']}, conclusion={run['conclusion']}")
                    has_output = 'output' in run and run.get('output') and 'text' in run.get('output', {})
                    
                    check_run = CheckRun(
                        name=run["name"],
                        status=run["status"],
                        conclusion=run["conclusion"] or "action_required",
                        url=run["html_url"] if "html_url" in run else None,
                    )
                    if has_output:
                        check_run.output = run["output"]
                    
                    # For failed check runs, try to get the full logs from workflow jobs
                    # but only if we haven't already found logs for another failed check
                    if check_run.conclusion in ["failure", "timed_out"] and not found_logs_for_failed_check:
                        workflow_run_id = None
                        if check_suite_id := run.get("check_suite", {}).get("id"):
                            workflow_runs = await self.get_workflow_runs_by_check_suite(repo, check_suite_id)
                            if workflow_runs:
                                most_recent_run = workflow_runs[0]
                                workflow_run_id = most_recent_run.get("id")
                            
                        if workflow_run_id:
                            logs_content = await self.get_workflow_logs(repo, workflow_run_id)
                            if logs_content:
                                z = zipfile.ZipFile(io.BytesIO(logs_content))
                                log_files = [f for f in z.namelist() if f.endswith('.txt')]
                                
                                if log_files:
                                    logs = []
                                    for log_file in log_files:
                                        job_name = log_file.split('/')[-1].replace('.txt', '')
                                        log_content = z.read(log_file).decode('utf-8', errors='replace')
                                        
                                        # First try to extract the last lines before process completion
                                        last_lines = self._extract_last_lines_before_completion(log_content)
                                        if last_lines:
                                            logger.info(f"Extracted last lines for job {job_name}")
                                            logs.append(f"=== JOB: {job_name} ===\n{last_lines}\n")
                                        else: # Send whole logs
                                            logs.append(f"=== JOB: {job_name} ===\n{log_content}\n")
                                    
                                    job_logs = "\n".join(logs)
                                    
                                    logger.info(f"Found workflow logs for failed run, total size: {len(job_logs)} bytes")
                                    if not check_run.output:
                                        check_run.output = {}
                                    check_run.output["text"] = job_logs
                                    # Mark that we've found logs for a failed check
                                    found_logs_for_failed_check = True
                    runs.append(check_run)

                logger.info(f"Total check runs found: {len(runs)}")
                if not runs:
                    logger.warning(f"No check runs found for PR #{pr_number} with SHA {sha}")
                return runs
        except httpx.TimeoutException as e:
            logger.warning(f"Timeout occurred while fetching check runs for PR #{pr_number}: {e}")
            return []
        except httpx.HTTPStatusError as e:
            logger.warning(f"HTTP error occurred while fetching check runs for PR #{pr_number}: {e}")
            return []
        except Exception as e:
            logger.warning(f"Unexpected error occurred while fetching check runs for PR #{pr_number}: {e}")
            return []

    async def find_existing_issues(self, repo: Repository, pr_number: int) -> list[Any]:
        """Find existing issues (not PRs) related to a specific PR number."""
        logger.info(f"Looking for existing open issues (not PRs) for PR #{pr_number} in {repo.owner}/{repo.name}")
        found = []
        url = f"{GITHUB_API_URL}/repos/{repo.owner}/{repo.name}/issues?state=open&per_page=100"
        
        # Pattern to match PR number in issue titles
        pr_pattern = re.compile(rf"Dependabot( PR)? #{pr_number}( | to )")
        
        while url:
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    resp = await client.get(url, headers=self.headers)
                    resp.raise_for_status()
                    data = resp.json()
                    
                    # Process issues from current page
                    for issue in data:
                            
                        # Match titles like 'Dependabot PR #123 to upgrade ... failed CI' or 'Dependabot #123 to upgrade ... failed CI'
                        if pr_pattern.search(issue["title"]):
                            found.append(issue)
                    
                    # Check for Link header to see if there are more pages
                    url = self._get_next_url_from_link_header(resp.headers.get('link', ''))
                        
            except httpx.TimeoutException as e:
                logger.warning(f"Timeout occurred while finding existing issues for PR #{pr_number}: {e}")
                break
            except httpx.HTTPStatusError as e:
                logger.warning(f"HTTP error occurred while finding existing issues for PR #{pr_number}: {e}")
                break
            except Exception as e:
                logger.warning(f"Unexpected error occurred while finding existing issues for PR #{pr_number}: {e}")
                break
                
        logger.info(f"Found {len(found)} matching issues for PR #{pr_number}")
        if found:
            for issue in found:
                logger.info(f"  Issue #{issue['number']}: {issue['title']} (State: {issue.get('state', 'unknown')}, URL: {issue.get('html_url', 'unknown')})")
                
        return found

    async def create_issue(self, repo: Repository, payload: IssuePayload) -> Any:
        url = f"{GITHUB_API_URL}/repos/{repo.owner}/{repo.name}/issues"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(url, headers=self.headers, json=payload.dict())
                resp.raise_for_status()
                return resp.json()
        except httpx.TimeoutException as e:
            logger.error(f"Timeout occurred while creating issue: {e}")
            raise RuntimeError(f"Failed to create issue due to timeout: {e}")
        except httpx.HTTPStatusError as e:
            logger.error(f"HTTP error occurred while creating issue: {e}")
            raise RuntimeError(f"Failed to create issue due to HTTP error: {e}")
        except Exception as e:
            logger.error(f"Unexpected error occurred while creating issue: {e}")
            raise RuntimeError(f"Failed to create issue: {e}")

    async def assign_issue(self, repo: Repository, issue_number: int, assignees: list[str]) -> Any:
        url = f"{GITHUB_API_URL}/repos/{repo.owner}/{repo.name}/issues/{issue_number}/assignees"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(url, headers=self.headers, json={"assignees": assignees})
                resp.raise_for_status()
                return resp.json()
        except httpx.TimeoutException as e:
            logger.error(f"Timeout occurred while assigning issue #{issue_number}: {e}")
            raise RuntimeError(f"Failed to assign issue due to timeout: {e}")
        except httpx.HTTPStatusError as e:
            logger.error(f"HTTP error occurred while assigning issue #{issue_number}: {e}")
            raise RuntimeError(f"Failed to assign issue due to HTTP error: {e}")
        except Exception as e:
            logger.error(f"Unexpected error occurred while assigning issue #{issue_number}: {e}")
            raise RuntimeError(f"Failed to assign issue: {e}")

    async def get_workflow_jobs(self, repo: Repository, run_id: int) -> list[dict]:
        """Fetch jobs for a specific workflow run."""
        logger.info(f"Fetching workflow jobs for run {run_id} in {repo.owner}/{repo.name}")
        url = f"{GITHUB_API_URL}/repos/{repo.owner}/{repo.name}/actions/runs/{run_id}/jobs"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.get(url, headers=self.headers)
                resp.raise_for_status()
                data = resp.json()
                return data.get("jobs", [])
        except httpx.TimeoutException as e:
            logger.warning(f"Timeout occurred while fetching jobs for run {run_id}: {e}")
            return []
        except httpx.HTTPStatusError as e:
            logger.warning(f"HTTP error occurred while fetching jobs for run {run_id}: {e}")
            return []
        except Exception as e:
            logger.warning(f"Unexpected error occurred while fetching jobs for run {run_id}: {e}")
            return []

    async def get_job_logs(self, repo: Repository, job_id: int) -> str:
        """Download logs for a specific job."""
        logger.info(f"Downloading logs for job {job_id} in {repo.owner}/{repo.name}")
        url = f"{GITHUB_API_URL}/repos/{repo.owner}/{repo.name}/actions/jobs/{job_id}/logs"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.get(url, headers=self.headers, follow_redirects=True)
                resp.raise_for_status()
                logs = resp.text
                logger.info(f"Downloaded logs for job {job_id}, size: {len(logs)} bytes")
                return logs
        except httpx.TimeoutException as e:
            logger.warning(f"Timeout occurred while downloading logs for job {job_id}: {e}")
            return f"Failed to download logs due to timeout: {e}"
        except httpx.HTTPStatusError as e:
            logger.warning(f"HTTP error occurred while downloading logs for job {job_id}: {e}")
            return f"Failed to download logs due to HTTP error: {e}"
        except Exception as e:
            logger.warning(f"Unexpected error occurred while downloading logs for job {job_id}: {e}")
            return f"Failed to download logs: {e}"

    async def get_workflow_run_logs(self, repo: Repository, run_id: int) -> str:
        """Fetch and combine logs from all failed jobs in a workflow run."""
        jobs = await self.get_workflow_jobs(repo, run_id)
        logs = []
        
        for job in jobs:
            if job.get("conclusion") in ["failure", "cancelled", "timed_out"]:
                job_id = job.get("id")
                if job_id is None:
                    logger.warning(f"Job ID is missing for job with conclusion {job.get('conclusion')}")
                    continue
                    
                job_name = job.get("name", "Unknown")
                logger.info(f"Fetching logs for failed job: {job_name} (ID: {job_id})")
                
                job_logs = await self.get_job_logs(repo, job_id)
                if job_logs:
                    logs.append(f"=== JOB: {job_name} (Status: {job.get('conclusion')}) ===\n{job_logs}\n")
        
        if logs:
            return "\n".join(logs)
        return ""

    async def get_workflow_logs(self, repo: Repository, run_id: int):
        """Get the logs for a workflow run.
        This is a direct implementation of the GitHub API endpoint for workflow logs.
        This method returns the raw ZIP content which can be saved or processed.
        """
        url = f"{GITHUB_API_URL}/repos/{repo.full_name}/actions/runs/{run_id}/logs"
        
        try:
            async with httpx.AsyncClient(timeout=self.long_timeout) as client:
                # First check if the run exists and is accessible
                run_url = f"{GITHUB_API_URL}/repos/{repo.full_name}/actions/runs/{run_id}"
                run_response = await client.get(run_url, headers=self.headers)
                
                if run_response.status_code == 404:
                    logger.warning(f"Workflow run {run_id} not found (404)")
                    return None
                
                run_response.raise_for_status()
                run_data = run_response.json()
                
                # Log important details about the run                
                # Only attempt to get logs if the run has completed
                if run_data.get('status') not in ['completed', 'failure', 'cancelled', 'timed_out']:
                    logger.warning(f"Workflow run {run_id} is not completed (status: {run_data.get('status')}), logs may not be available")
                
                # Now try to get the logs
                response = await client.get(url, headers=self.headers, follow_redirects=False)
                
                if response.status_code == 404:
                    logger.warning(f"Logs for workflow run {run_id} not found (404)")
                    return None
                    
                # Logs endpoint returns a redirect to a download URL
                if response.status_code == 302:
                    download_url = response.headers.get("location")
                    if download_url:
                        try:
                            # Create a new client with a longer timeout for downloading the zip file
                            download_timeout = httpx.Timeout(60.0, connect=10.0)
                            async with httpx.AsyncClient(timeout=download_timeout) as download_client:
                                logs_response = await download_client.get(download_url, follow_redirects=True)
                                logs_response.raise_for_status()
                                return logs_response.content
                        except Exception as e:
                            logger.error(f"Error downloading logs from redirect URL: {e}")
                            return None
                    else:
                        logger.warning(f"No redirect location found in response headers for workflow run {run_id}")
                        return None
                else:
                    logger.warning(f"Expected 302 redirect for workflow run {run_id} logs, got {response.status_code}")
                    return None
                
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                logger.warning(f"Workflow run {run_id} or its logs not found (404)")
            else:
                logger.error(f"HTTP error fetching workflow logs for {repo.full_name} run {run_id}: {e}")
            return None
        except httpx.RequestError as e:
            logger.error(f"Request error fetching workflow logs for {repo.full_name} run {run_id}: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error fetching workflow logs for {repo.full_name} run {run_id}: {e}")
            return None

    async def get_workflow_runs_by_check_suite(self, repo: Repository, check_suite_id: int):
        """Try to find workflow runs associated with a check suite.
        This is a best effort attempt since GitHub API doesn't have a direct endpoint 
        for this relationship.
        """
        # First approach: Use the check-suites API endpoint to see if it contains workflow info
        check_suite_url = f"{GITHUB_API_URL}/repos/{repo.full_name}/check-suites/{check_suite_id}"
        
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(check_suite_url, headers=self.headers)
                response.raise_for_status()

                check_suite_data = response.json()
                
                # Look for workflow-related fields in the check suite data
                for key in check_suite_data.keys():
                    if "workflow" in key.lower():
                        # If there's a workflow ID, we might be able to find the run
                        if isinstance(check_suite_data.get(key), dict) and check_suite_data.get(key).get("id"):
                            workflow_id = check_suite_data.get(key).get("id")
                            # Now try to find the most recent run for this workflow
                            return await self._get_recent_workflow_runs_by_workflow_id(repo, workflow_id)
        
        except (httpx.RequestError, httpx.HTTPStatusError) as e:
            logger.warning(f"Error fetching check suite details: {e}")
            # Fall through to the next approach
        
        # Second approach: Get recent workflow runs and look for a match
        url = f"{GITHUB_API_URL}/repos/{repo.full_name}/actions/runs?per_page=20"
        
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(url, headers=self.headers)
                response.raise_for_status()
                
                data = response.json()
                workflow_runs = data.get("workflow_runs", [])
                
                # Look for runs that might be associated with this check suite
                matching_runs = []
                recent_runs = []
                
                for run in workflow_runs:
                    run_id = run.get("id")
                    if not run_id:
                        continue
                        
                    # Store all recent runs for fallback
                    recent_runs.append(run)
                    
                    # Look for any field that might connect to the check suite
                    if run.get("check_suite_id") == check_suite_id:
                        matching_runs.append(run)
                
                # Verify that the workflow runs are valid by checking their URL
                if matching_runs:
                    valid_runs = await self._validate_workflow_runs(repo, matching_runs)
                    if valid_runs:
                        return valid_runs
                
                # If no matching runs found, return the recent runs as a fallback
                logger.info(f"No exact matches found, returning {len(recent_runs)} recent workflow runs as fallback")
                valid_runs = await self._validate_workflow_runs(repo, recent_runs)
                return valid_runs
                
        except (httpx.RequestError, httpx.HTTPStatusError) as e:
            logger.error(f"Error fetching workflow runs for {repo.full_name}: {e}")
            return []
            
    async def _get_recent_workflow_runs_by_workflow_id(self, repo: Repository, workflow_id: int):
        """Get recent runs for a specific workflow ID."""
        url = f"{GITHUB_API_URL}/repos/{repo.full_name}/actions/workflows/{workflow_id}/runs?per_page=5"
        
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(url, headers=self.headers)
                response.raise_for_status()
                
                data = response.json()
                workflow_runs = data.get("workflow_runs", [])
                
                if workflow_runs:
                    logger.info(f"Found {len(workflow_runs)} recent runs for workflow ID {workflow_id}")
                    return await self._validate_workflow_runs(repo, workflow_runs)
                else:
                    logger.warning(f"No runs found for workflow ID {workflow_id}")
                    return []
                    
        except (httpx.RequestError, httpx.HTTPStatusError) as e:
            logger.warning(f"Error fetching workflow runs for workflow ID {workflow_id}: {e}")
            return []
            
    async def _validate_workflow_runs(self, repo: Repository, workflow_runs: list):
        """Validate that workflow runs exist and are accessible."""
        valid_runs = []
        
        for run in workflow_runs:
            run_id = run.get("id")
            if not run_id:
                continue
                
            # Check if the logs endpoint exists for this run
            url = f"{GITHUB_API_URL}/repos/{repo.full_name}/actions/runs/{run_id}"
            
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    response = await client.get(url, headers=self.headers)
                    if response.status_code == 200:
                        valid_runs.append(run)
                    else:
                        logger.warning(f"Workflow run {run_id} returned status code {response.status_code}")
            except Exception as e:
                logger.warning(f"Error validating workflow run {run_id}: {e}")
                
        return valid_runs
        
    async def get_file_content(self, repo: Repository, file_path: str, ref: str = "main") -> FileContent | None:
        """Fetch the content of a specific file from a repository.
        
        Args:
            repo: The repository to fetch from
            file_path: Path to the file in the repository
            ref: The git reference (branch, tag, or commit SHA) to fetch from
            
        Returns:
            FileContent object if the file exists, None otherwise
        """
        try:
            url = f"{GITHUB_API_URL}/repos/{repo.owner}/{repo.name}/contents/{file_path}"
            params = {"ref": ref}
            
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.get(url, headers=self.headers, params=params)
                resp.raise_for_status()
                data = resp.json()
            
            # Handle files that are too large or binary files
            if data.get("type") != "file":
                logger.warning(f"Path {file_path} is not a file in {repo.full_name}")
                return None
                
            if data.get("encoding") == "base64":
                import base64
                content = base64.b64decode(data["content"]).decode("utf-8", errors="ignore")
            else:
                content = data.get("content", "")
            
            return FileContent(
                path=file_path,
                content=content,
                sha=data["sha"]
            )
            
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return None
            logger.error(f"HTTP error fetching file {file_path} from {repo.full_name}: {e}")
            raise
        except Exception as e:
            logger.error(f"Error fetching file {file_path} from {repo.full_name}: {e}")
            raise

    async def get_directory_contents(self, repo: Repository, directory_path: str, ref: str = "main") -> list[DirectoryItem]:
        """List the contents of a directory in a repository.
        
        Args:
            repo: The repository to fetch from
            directory_path: Path to the directory in the repository
            ref: The git reference (branch, tag, or commit SHA) to fetch from
            
        Returns:
            List of DirectoryItem objects representing files and subdirectories
        """
        try:
            url = f"{GITHUB_API_URL}/repos/{repo.owner}/{repo.name}/contents/{directory_path}"
            params = {"ref": ref}
            
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.get(url, headers=self.headers, params=params)
                resp.raise_for_status()
                data = resp.json()
            
            # Handle single file vs directory
            if isinstance(data, dict):
                logger.warning(f"Path {directory_path} is not a directory in {repo.full_name}")
                return []
            
            # Parse directory contents
            items = []
            for item in data:
                items.append(DirectoryItem(
                    name=item["name"],
                    path=item["path"],
                    type=item["type"],
                    sha=item["sha"],
                    size=item.get("size"),
                    download_url=item.get("download_url")
                ))
            
            return items
            
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return []
            logger.error(f"HTTP error fetching directory {directory_path} from {repo.full_name}: {e}")
            raise
        except Exception as e:
            logger.error(f"Error fetching directory {directory_path} from {repo.full_name}: {e}")
            raise

    async def get_files_in_directory(self, repo: Repository, directory_path: str, file_pattern: str | None = None, ref: str = "main") -> list[FileContent]:
        """Get the content of all files in a directory, optionally filtered by pattern.
        
        Args:
            repo: The repository to fetch from
            directory_path: Path to the directory in the repository
            file_pattern: Optional regex pattern to filter filenames
            ref: The git reference (branch, tag, or commit SHA) to fetch from
            
        Returns:
            List of FileContent objects for all matching files
        """
        import re
        
        # Get directory contents
        items = await self.get_directory_contents(repo, directory_path, ref)
        
        # Filter for files only
        files = [item for item in items if item.type == "file"]
        
        # Apply filename pattern filter if specified
        if file_pattern:
            try:
                pattern_re = re.compile(file_pattern, re.IGNORECASE)
                files = [f for f in files if pattern_re.search(f.name)]
            except re.error as e:
                logger.error(f"Invalid file pattern '{file_pattern}': {e}")
                return []
        
        # Fetch content for each file
        file_contents = []
        for file_item in files:
            try:
                file_content = await self.get_file_content(repo, file_item.path, ref)
                if file_content:
                    file_contents.append(file_content)
            except Exception as e:
                logger.warning(f"Failed to fetch content for {file_item.path}: {e}")
                continue
        
        return file_contents

    def check_code_pattern(self, file_content: FileContent, pattern: str) -> CodeMatchResult:
        """Check if a file contains a specific code pattern.
        
        Args:
            file_content: The file content to search in
            pattern: The pattern to search for (can be a regex or literal string)
            
        Returns:
            CodeMatchResult with match information
        """
        import re
        
        lines = file_content.content.splitlines()
        matched_lines = []
        line_numbers = []
        
        # Try as regex first, fall back to literal string search
        try:
            pattern_re = re.compile(pattern, re.IGNORECASE)
            for i, line in enumerate(lines, 1):
                if pattern_re.search(line):
                    matched_lines.append(line.strip())
                    line_numbers.append(i)
        except re.error:
            # Pattern is not valid regex, use literal string search
            for i, line in enumerate(lines, 1):
                if pattern.lower() in line.lower():
                    matched_lines.append(line.strip())
                    line_numbers.append(i)
        
        return CodeMatchResult(
            file_path=file_content.path,
            pattern=pattern,
            matched=len(matched_lines) > 0,
            line_numbers=line_numbers,
            matched_lines=matched_lines
        )

    async def issue_exists_with_title(self, repo: Repository, title: str) -> bool:
        """Determine whether an open issue already exists with the given title.

        Args:
            repo: Repository being scanned.
            title: Exact issue title to look for.

        Returns:
            True when an open issue (not PR) with the matching title exists.
        """
        url = f"{GITHUB_API_URL}/repos/{repo.owner}/{repo.name}/issues?state=open&per_page=100"

        while url:
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    resp = await client.get(url, headers=self.headers)
                    resp.raise_for_status()
                    data = resp.json()

                for issue in data:
                    if issue.get("pull_request"):
                        continue
                    if issue.get("title") == title:
                        logger.info(
                            "Found existing open issue #%s with title '%s' in %s",
                            issue.get("number"),
                            title,
                            repo.full_name,
                        )
                        return True

                url = self._get_next_url_from_link_header(resp.headers.get('link', ''))
            except httpx.TimeoutException as exc:
                logger.warning(
                    "Timeout encountered while checking existing issues for %s: %s",
                    repo.full_name,
                    exc,
                )
                return False
            except httpx.HTTPStatusError as exc:
                logger.error(
                    "HTTP error while checking existing issues for %s: %s",
                    repo.full_name,
                    exc,
                )
                return False
            except Exception as exc:  # noqa: BLE001 - log unexpected failure
                logger.error(
                    "Unexpected error while checking existing issues for %s: %s",
                    repo.full_name,
                    exc,
                )
                return False

        return False

    def _extract_last_lines_before_completion(self, log_content: str, line_count: int = 20) -> str:
        """Extract the last N lines before 'Process completed' in a log file."""
        # Look for "Process completed" message
        process_completed_pattern = re.compile(r'##\[error\]Process completed with exit code \d+')
        match = process_completed_pattern.search(log_content)
        
        if match:
            # Get the position of the match
            pos = match.start()
            
            # Get the content before the match
            content_before = log_content[:pos].strip()
            
            # Split into lines and take the last N lines
            lines = content_before.splitlines()
            if len(lines) <= line_count:
                return content_before
            
            return "\n".join(lines[-line_count:])
        
        # If "Process completed" not found, return the last 20 lines
        lines = log_content.splitlines()
        if len(lines) <= line_count:
            return log_content
        
        return "\n".join(lines[-line_count:])
    
    @retry(
        retry=retry_if_exception(should_retry_github_search),
        stop=stop_after_attempt(3),  # Try up to 3 times total
        wait=wait_exponential(multiplier=60, min=60, max=900),  # Start with 60s, then 120s, 240s (capped at 15min)
        reraise=True
    )
    async def search_code_in_repo(
        self,
        repo: Repository,
        query: str,
        content_pattern: str | None = None,
    ) -> list[CodeMatchResult]:
        """Search for code patterns in a repository using GitHub's search API.
        
        Args:
            repo: The repository to search in
            query: The search query/pattern to look for
            
        Returns:
            List of CodeMatchResult objects containing file paths and match details
        """
        try:
            # Construct the search query to limit to the specific repository
            search_query = f"{query} repo:{repo.owner}/{repo.name}"
            url = f"{GITHUB_API_URL}/search/code"
            params = {
                "q": search_query,
                "per_page": 100  # Maximum allowed per page
            }
            
            results = []
            
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.get(url, headers=self.headers, params=params)
                resp.raise_for_status()
                data = resp.json()
            
            # Process search results
            for item in data.get("items", []):
                file_path = item.get("path", "")
                
                # Get the actual file content to find exact matches
                file_content = await self.get_file_content(repo, file_path)
                if not file_content:
                    continue

                match_result = self.check_code_pattern(
                    file_content,
                    content_pattern or query,
                )
                if match_result.matched:
                    results.append(match_result)
            
            logger.info(f"Found {len(results)} files with matches for '{query}' in {repo.full_name}")
            return results
            
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 422:
                # Unprocessable Entity - often means no results or invalid query
                logger.info(f"No search results found for '{query}' in {repo.full_name}")
                return []
            elif e.response.status_code == 403:
                # Rate limit exceeded - this will trigger retry via tenacity
                logger.warning(f"Search API rate limit exceeded for {repo.full_name}, retrying...")
                # Extract rate limit info from response headers if available
                rate_limit_remaining = e.response.headers.get('x-ratelimit-remaining', 'unknown')
                rate_limit_reset = e.response.headers.get('x-ratelimit-reset', 'unknown') 
                logger.info(f"Rate limit remaining: {rate_limit_remaining}, reset time: {rate_limit_reset}")
                # Re-raise so tenacity can retry it
                raise
            else:
                logger.error(f"HTTP error searching for '{query}' in {repo.full_name}: {e}")
                raise
        except Exception as e:
            logger.error(f"Error searching for '{query}' in {repo.full_name}: {e}")
            raise



async def create_issue_graphql(self, repo, issue):
    """Create an issue and assign Copilot using the GraphQL API, with improved error handling."""
    repo_id, copilot_id = await self.get_repo_and_copilot_ids(repo)
    headers = {"Authorization": f"Bearer {self.auth_token}", "Accept": "application/vnd.github+json"}
    mutation = '''
    mutation($input: CreateIssueInput!) {
        createIssue(input: $input) {
        issue {
            id
            number
            title
            url
        }
        }
    }
    '''
    input_obj = {
        "repositoryId": repo_id,
        "title": issue.title,
        "body": issue.body,
        "assigneeIds": [copilot_id],
    }
    async with httpx.AsyncClient(timeout=self.timeout) as client:
        resp = await client.post(GITHUB_GRAPHQL_URL, headers=headers, json={"query": mutation, "variables": {"input": input_obj}})
        resp.raise_for_status()
        data = resp.json()
    issue_data = data.get("data", {}).get("createIssue", {}).get("issue")
    return {
        "number": issue_data["number"],
        "html_url": issue_data["url"]
    }