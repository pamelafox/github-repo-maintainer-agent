import io
import logging
import re
import zipfile
from typing import Any

import httpx

from models import CheckRun, IssuePayload, PullRequest, Repository

logger = logging.getLogger("repo_maintainer_agent")
logger.setLevel(logging.INFO)

GITHUB_GRAPHQL_URL = "https://api.github.com/graphql"

GITHUB_API_URL = "https://api.github.com"

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
    def __init__(self, auth_token):
        self.auth_token = auth_token
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

    async def list_owned_repos(self, org: str | None = None) -> list[Repository]:
        """List all repos owned by the authenticated user, or where user is a collaborator/maintainer. Optionally filter by org using /orgs/{org}/repos."""
        repos = []
        
        if org:
            # List all repos in the org, sorted by updated date in descending order, filter by permissions
            url = f"{GITHUB_API_URL}/orgs/{org}/repos?per_page=100&type=public&sort=updated&direction=desc"
        else:
            # Use affiliation=owner,collaborator,organization_member to get all repos user can access
            url = f"{GITHUB_API_URL}/user/repos?affiliation=owner,collaborator,organization_member&per_page=100&type=public"
        
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
                    if check_run.conclusion in ["failure", "cancelled", "timed_out"] and not found_logs_for_failed_check:
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
                                    # Save to a local file for debugging
                                    with open(f"check_run_{check_run.name}_logs.txt", "w", encoding="utf-8") as f:
                                        f.write(job_logs)
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
