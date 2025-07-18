import logging
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
        headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json"}
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
        async with httpx.AsyncClient() as client:
            resp = await client.post(GITHUB_GRAPHQL_URL, headers=headers, json={"query": query, "variables": variables})
            resp.raise_for_status()
            data = resp.json()
        repo_id = data["data"]["repository"]["id"]
        copilot_node = next((n for n in data["data"]["repository"]["suggestedActors"]["nodes"] if n["login"] == "copilot-swe-agent"), None)
        if not copilot_node or not copilot_node.get("id"):
            raise RuntimeError("Copilot is not assignable in this repository.")
        return repo_id, copilot_node["id"]

    async def create_issue_graphql(self, repo, issue):
        """Create an issue and assign Copilot using the GraphQL API."""
        repo_id, copilot_id = await self.get_repo_and_copilot_ids(repo)
        headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json"}
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
        async with httpx.AsyncClient() as client:
            resp = await client.post(GITHUB_GRAPHQL_URL, headers=headers, json={"query": mutation, "variables": {"input": input_obj}})
            resp.raise_for_status()
            data = resp.json()
        return {
            "number": data["data"]["createIssue"]["issue"]["number"],
            "html_url": data["data"]["createIssue"]["issue"]["url"]
        }
    def __init__(self, token: str):
        self.token = token
        self.headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
        }

    async def list_owned_repos(self, org: str | None = None) -> list[Repository]:
        """List all repos owned by the authenticated user, or where user is a collaborator/maintainer. Optionally filter by org using /orgs/{org}/repos."""
        repos = []
        page = 1
        if org:
            # List all repos in the org, but filter by permissions
            url = f"{GITHUB_API_URL}/orgs/{org}/repos?per_page=100&type=public"
            while True:
                paged_url = f"{url}&page={page}"
                async with httpx.AsyncClient() as client:
                    resp = await client.get(paged_url, headers=self.headers)
                    resp.raise_for_status()
                    data = resp.json()
                    if not data:
                        break
                    for repo in data:
                        # Only include if user has admin, maintain, or write permission
                        permissions = repo.get("permissions", {})
                        if permissions.get("admin") or permissions.get("maintain") or permissions.get("push"):
                            logger.info(f"[ORG] Found repo: {repo['name']} owned by {repo['owner']['login']}, archived: {repo['archived']}")
                            repos.append(Repository(
                                name=repo["name"],
                                owner=repo["owner"]["login"],
                                archived=repo["archived"],
                            ))
                page += 1
        else:
            # Use affiliation=owner,collaborator,organization_member to get all repos user can access
            url = f"{GITHUB_API_URL}/user/repos?affiliation=owner,collaborator,organization_member&per_page=100&type=public"
            while True:
                paged_url = f"{url}&page={page}"
                async with httpx.AsyncClient() as client:
                    resp = await client.get(paged_url, headers=self.headers)
                    resp.raise_for_status()
                    data = resp.json()
                    if not data:
                        break
                    for repo in data:
                        logger.info(f"Found repo: {repo['name']} owned by {repo['owner']['login']}, archived: {repo['archived']}")
                        # Only include if user has admin, maintain, or write permission
                        permissions = repo.get("permissions", {})
                        if permissions.get("admin") or permissions.get("maintain") or permissions.get("push"):
                            repos.append(Repository(
                                name=repo["name"],
                                owner=repo["owner"]["login"],
                                archived=repo["archived"],
                            ))
                page += 1
        return repos

    async def list_dependabot_prs(self, repo: Repository) -> list[PullRequest]:
        prs = []
        page = 1
        while True:
            url = f"{GITHUB_API_URL}/repos/{repo.owner}/{repo.name}/pulls?state=open&per_page=100&page={page}"
            async with httpx.AsyncClient() as client:
                resp = await client.get(url, headers=self.headers)
                resp.raise_for_status()
                data = resp.json()
                if not data:
                    break
                for pr in data:
                    if pr["user"]["login"].startswith("dependabot"):
                        prs.append(PullRequest(number=pr["number"], author=pr["user"]["login"], url=pr["html_url"], title=pr["title"]))
                page += 1
        return prs

    async def get_pr_check_runs(self, repo: Repository, pr_number: int) -> list[CheckRun]:
        # Get the head SHA for the PR
        url_pr = f"{GITHUB_API_URL}/repos/{repo.owner}/{repo.name}/pulls/{pr_number}"
        async with httpx.AsyncClient() as client:
            resp = await client.get(url_pr, headers=self.headers)
            resp.raise_for_status()
            pr_data = resp.json()
            sha = pr_data["head"]["sha"]
        # Get check runs for the SHA
        url = f"{GITHUB_API_URL}/repos/{repo.owner}/{repo.name}/commits/{sha}/check-runs"
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, headers=self.headers)
            resp.raise_for_status()
            data = resp.json()
            runs = []
            for run in data.get("check_runs", []):
                runs.append(CheckRun(
                    name=run["name"],
                    status=run["status"],
                    conclusion=run["conclusion"] or "action_required",
                    url=run["html_url"] if "html_url" in run else None,
                ))
        return runs

    async def find_existing_issues(self, repo: Repository, pr_number: int) -> list[Any]:
        url = f"{GITHUB_API_URL}/repos/{repo.owner}/{repo.name}/issues?state=open&per_page=100"
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, headers=self.headers)
            resp.raise_for_status()
            data = resp.json()
            found = []
            for issue in data:
                # Match titles like 'Dependabot PR #123 to upgrade ... failed CI' or 'Dependabot #123 to upgrade ... failed CI'
                # TODO: Dont use regex, maybe look for a label
                import re
                pr_pattern = re.compile(rf"Dependabot( PR)? #{pr_number}( | to )")
                if pr_pattern.search(issue["title"]):
                    found.append(issue)
            return found

    async def create_issue(self, repo: Repository, payload: IssuePayload) -> Any:
        url = f"{GITHUB_API_URL}/repos/{repo.owner}/{repo.name}/issues"
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, headers=self.headers, json=payload.dict())
            resp.raise_for_status()
            return resp.json()

    async def assign_issue(self, repo: Repository, issue_number: int, assignees: list[str]) -> Any:
        url = f"{GITHUB_API_URL}/repos/{repo.owner}/{repo.name}/issues/{issue_number}/assignees"
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, headers=self.headers, json={"assignees": assignees})
            resp.raise_for_status()
            return resp.json()
