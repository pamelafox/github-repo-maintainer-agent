
import argparse
import asyncio
import logging
import os

from dotenv import load_dotenv
from rich.logging import RichHandler

from github_client import GitHubClient
from llm_client import LLMClient
from models import AnalyzeFailureInput, IssuePayload

# Setup logging with RichHandler
logging.basicConfig(
    level=logging.WARNING,
    format="%(message)s",
    datefmt="[%X]",
    handlers=[RichHandler(show_level=True)]
)
logger = logging.getLogger("repo_maintainer_agent")
logger.setLevel(logging.INFO)

# Set third-party loggers to WARNING level to reduce noise
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

class RepoMaintainerAgent:
    def __init__(self, dry_run: bool = False, exclude_archived: bool = True, filter_pattern: str | None = None, repos_yaml: str | None = None):
        self.dry_run = dry_run
        self.exclude_archived = exclude_archived
        self.filter_pattern = filter_pattern
        self.repos_yaml = repos_yaml
        self.github = GitHubClient(os.environ["GITHUB_TOKEN"])
        self.llm = LLMClient()

    async def process_all(self, org: str | None = None):
        # Determine how to load repositories
        if self.repos_yaml:
            logger.info(f"Loading repositories from YAML file: {self.repos_yaml}")
            repos = await self.github.list_repos_from_yaml(self.repos_yaml)
        else:
            logger.info(f"Discovering repositories from GitHub API{' (org: ' + org + ')' if org else ''}")
            repos = await self.github.list_owned_repos(org=org)
        
        logger.info(f"Discovered {len(repos)} repositories.")
        
        if self.exclude_archived:
            repos = [r for r in repos if not r.archived]
        if self.filter_pattern:
            import re
            repos = [r for r in repos if re.search(self.filter_pattern, r.name)]
        logger.info(f"Processing {len(repos)} repositories after filtering.")
        total_prs = 0
        total_issues = 0
        for repo in repos:
            prs = await self.github.list_dependabot_prs(repo)
            if not prs:
                continue
            for pr in prs:
                check_runs = await self.github.get_pr_check_runs(repo, pr.number)
                failing = [c for c in check_runs if c.conclusion in ["failure", "cancelled", "timed_out"]]
                if not failing:
                    continue
                total_prs += 1
                # Only analyze if at least one failing check_run has non-empty logs
                # Check for logs safely using the helper method
                has_logs = any(c.get_output_text() is not None for c in failing)
                existing = await self.github.find_existing_issues(repo, pr.number)
                if existing:
                    logger.info(f"Issue already exists for PR #{pr.number} in {repo.name}")
                    continue
                # Extract target package from PR title (e.g., 'Bump requests from 2.25.1 to 2.26.0')
                import re
                pr_title = pr.title
                # Try to extract the first word after 'Bump ' or 'Update '
                m = re.match(r"(?:Bump|Update) ([^ ]+)", pr_title)
                target_pkg = m.group(1) if m else pr_title
                if has_logs:
                    logger.info(f"Analyzing failure for PR #{pr.number} in {repo.name} targeting {target_pkg}")
                    analysis = await self.llm.analyze_failure(AnalyzeFailureInput(pr_url=pr.url, check_runs=failing))
                    issue = IssuePayload(
                        title=f"Dependabot PR #{pr.number} to upgrade {target_pkg} failed CI",
                        body=f"PR: {pr.url}\n\nSummary: {analysis.summary}\n\nInstructions: Please check the failed workflow and ensure the packages can be installed fully per repo README instructions. Always check your work by making sure the packages can be installed successfully! \n\nRelevant logs:\n" + "\n".join(analysis.related_logs),
                        labels=["dependabot-agent", analysis.type],
                        assignees=["copilot-swe-agent"],  # for dry-run/info only
                    )
                    logger.info(f"Analysis for PR #{pr.number} in {repo.name}: {analysis.summary}")
                else:
                    logger.info(f"No logs for PR #{pr.number} in {repo.name}, creating generic failed CI issue.")
                    issue = IssuePayload(
                        title=f"Dependabot #{pr.number} to upgrade {target_pkg} failed CI",
                        body=f"PR: {pr.url}\n\nSummary: This Dependabot PR has at least one failed, cancelled, or timed out check run, but no logs are available (they may have expired). Please upgrade the package that was the target of the PR and make sure that the full package requirements can be installed according to the repo's README.",
                        labels=["dependabot-agent", "failed_ci"],
                        assignees=["copilot-swe-agent"],  # for dry-run/info only
                    )
                if self.dry_run:
                    logger.info(f"[DRY RUN] Would create issue in {repo.name} for PR #{pr.number}: {issue.title}")
                else:
                    try:
                        created = await self.github.create_issue_graphql(repo, issue)
                        logger.info(f"Created issue {created['html_url']} for PR #{pr.number} in {repo.name}")
                        total_issues += 1
                    except Exception as e:
                        import traceback
                        logger.error(f"Failed to create issue in {repo.name} for PR #{pr.number}: {e}")
                        try:
                            import httpx
                            if isinstance(e, httpx.HTTPStatusError) and e.response is not None:
                                logger.error(f"Response content: {e.response.text}")
                        except Exception:
                            logger.error("Could not read response content or httpx not available.")
                        logger.error(traceback.format_exc())
        logger.info(f"Scan complete. PRs checked: {total_prs}, Issues created: {total_issues}")

if __name__ == "__main__":
    load_dotenv(override=True)

    parser = argparse.ArgumentParser(description="GitHub Repo Maintainer Agent")
    parser.add_argument("--dry-run", action="store_true", help="Log actions without making changes")
    parser.add_argument("--exclude-archived", action="store_true", default=True, help="Exclude archived repos")
    parser.add_argument("--filter-pattern", type=str, help="Regex to filter repo names")
    parser.add_argument("--org", type=str, help="Only include repos in this organization (e.g. Azure-Samples)")
    parser.add_argument("--repos-yaml", type=str, help="Path to a YAML file that lists repositories to process")
    args = parser.parse_args()
    
    agent = RepoMaintainerAgent(
        dry_run=args.dry_run, 
        exclude_archived=args.exclude_archived, 
        filter_pattern=args.filter_pattern,
        repos_yaml=args.repos_yaml
    )
    
    asyncio.run(agent.process_all(org=args.org))
