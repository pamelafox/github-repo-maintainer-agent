
import argparse
import asyncio
import logging
import re
from pathlib import Path

import yaml
from dotenv import load_dotenv
from rich.logging import RichHandler

from github_client import GitHubClient
from llm_client import LLMClient
from models import AnalyzeFailureInput, CodeCheckConfig, IssuePayload

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
    def __init__(self, dry_run: bool = False, exclude_archived: bool = True, repo_name: str | None = None, repos_yaml: str | None = None):
        self.dry_run = dry_run
        self.exclude_archived = exclude_archived
        self.repo_name = repo_name
        self.repos_yaml = repos_yaml
        self.llm = LLMClient()
        # Cache for GitHub clients by organization
        self._github_clients = {}

    def get_github_client(self, org: str | None = None) -> GitHubClient:
        """Get a GitHub client for the specified organization, creating one if needed.
        
        Args:
            org: Organization name. If None, uses personal token.
            
        Returns:
            GitHubClient instance configured for the organization.
        """
        cache_key = org or "personal"
        if cache_key not in self._github_clients:
            self._github_clients[cache_key] = GitHubClient(org=org)
        return self._github_clients[cache_key]

    async def process_all(self, org: str | None = None):
        # Determine how to load repositories
        if self.repos_yaml:
            logger.info(f"Loading repositories from YAML file: {self.repos_yaml}")
            # Use personal client for loading repos from YAML (it can access all repos the token has access to)
            github = self.get_github_client(org=None)
            repos = await github.list_repos_from_yaml(self.repos_yaml)
        else:
            logger.info(f"Discovering repositories from GitHub API{' (org: ' + org + ')' if org else ''}")
            github = self.get_github_client(org=org)
            repos = await github.list_owned_repos(org=org)
        
        logger.info(f"Discovered {len(repos)} repositories.")
        
        if self.exclude_archived:
            repos = [r for r in repos if not r.archived]
        if self.repo_name:
            repos = [r for r in repos if r.name == self.repo_name]
        logger.info(f"Processing {len(repos)} repositories after filtering.")
        total_prs = 0
        total_issues = 0
        for repo in repos:
            # Get the appropriate GitHub client for this repository
            repo_org = None if repo.is_personal else repo.owner
            github = self.get_github_client(org=repo_org)
            
            prs = await github.list_dependabot_prs(repo)
            if not prs:
                continue
            for pr in prs:
                check_runs = await github.get_pr_check_runs(repo, pr.number)
                failing = [c for c in check_runs if c.conclusion in ["failure", "cancelled", "timed_out"]]
                if not failing:
                    continue
                total_prs += 1
                # Only analyze if at least one failing check_run has non-empty logs
                # Check for logs safely using the helper method
                has_logs = any(c.get_output_text() is not None for c in failing)
                existing = await github.find_existing_issues(repo, pr.number)
                if existing:
                    logger.info(f"Issue already exists for PR #{pr.number} in {repo.name}")
                    continue
                # Extract target package from PR title (e.g., 'Bump requests from 2.25.1 to 2.26.0')
                
                pr_title = pr.title
                # Try to extract the first word after 'Bump ' or 'Update '
                m = re.match(r"(?:Bump|Update) ([^ ]+)", pr_title)
                target_pkg = m.group(1) if m else pr_title
                if has_logs:
                    logger.info(f"Analyzing failure for PR #{pr.number} in {repo.name} targeting {target_pkg}")
                    analysis = await self.llm.analyze_failure(AnalyzeFailureInput(pr_url=pr.url, check_runs=failing))
                    issue = IssuePayload.from_template(
                        title=f"Dependabot PR #{pr.number} to upgrade {target_pkg} failed CI",
                        template_path="issue_with_logs.jinja2",
                        template_vars={
                            "pr_url": pr.url,
                            "summary": analysis.summary,
                            "related_logs": analysis.related_logs
                        },
                        labels=["dependabot-agent", analysis.type],
                        assignees=["copilot-swe-agent"],  # for dry-run/info only
                    )
                    logger.info(f"Analysis for PR #{pr.number} in {repo.name}: {analysis.summary}")
                else:
                    logger.info(f"No logs for PR #{pr.number} in {repo.name}, creating generic failed CI issue.")
                    issue = IssuePayload.from_template(
                        title=f"Dependabot #{pr.number} to upgrade {target_pkg} failed CI",
                        template_path="issue_no_logs.jinja2",
                        template_vars={
                            "pr_url": pr.url
                        },
                        labels=["dependabot-agent", "failed_ci"],
                        assignees=["copilot-swe-agent"],  # for dry-run/info only
                    )
                if self.dry_run:
                    logger.info(f"[DRY RUN] Would create issue in {repo.name} for PR #{pr.number}: {issue.title}")
                else:
                    try:
                        created = await github.create_issue_graphql(repo, issue)
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

    async def check_code_patterns(self, config_file: str, org: str | None = None):
        """Check for specific code patterns in repository files and create issues when found.
        
        Args:
            config_file: Path to YAML file containing code check configurations
            org: Optional organization to filter repositories
        """
        # Load code check configurations
        try:
            config_path = Path(config_file)
            if not config_path.exists():
                logger.error(f"Code check config file not found: {config_file}")
                return
            
            with open(config_path) as f:
                config_data = yaml.safe_load(f)
            
            # Parse configurations
            code_checks = []
            for check_data in config_data.get("code_checks", []):
                code_checks.append(CodeCheckConfig.model_validate(check_data))
            
            if not code_checks:
                logger.warning("No code checks defined in configuration file")
                return
                
            logger.info(f"Loaded {len(code_checks)} code check configurations")
            
        except Exception as e:
            logger.error(f"Error loading code check config: {e}")
            return

        # Load repositories
        if self.repos_yaml:
            logger.info(f"Loading repositories from YAML file: {self.repos_yaml}")
            # Use personal client for loading repos from YAML (it can access all repos the token has access to)
            github = self.get_github_client(org=None)
            repos = await github.list_repos_from_yaml(self.repos_yaml)
        else:
            logger.info(f"Discovering repositories from GitHub API{' (org: ' + org + ')' if org else ''}")
            github = self.get_github_client(org=org)
            repos = await github.list_owned_repos(org=org)
        
        logger.info(f"Discovered {len(repos)} repositories.")
        
        if self.exclude_archived:
            repos = [r for r in repos if not r.archived]
        if self.repo_name:
            repos = [r for r in repos if r.name == self.repo_name]
        
        logger.info(f"Processing {len(repos)} repositories after filtering.")
        
        total_checks = 0
        total_matches = 0
        total_issues = 0
        
        for repo in repos:
            logger.info(f"Checking repository: {repo.full_name}")
            
            # Get the appropriate GitHub client for this repository
            repo_org = None if repo.is_personal else repo.owner
            github = self.get_github_client(org=repo_org)
            
            for check in code_checks:
                total_checks += 1
                
                # Determine what we're checking
                if check.file_path:
                    check_target = check.file_path
                    logger.info(f"Checking {repo.name} for pattern '{check.pattern}' in file {check.file_path}")
                elif check.search_repo:
                    check_target = "entire repository"
                    logger.info(f"Checking {repo.name} for pattern '{check.pattern}' across entire repository using search API")
                else:
                    check_target = f"{check.directory_path}/*"
                    if check.file_pattern:
                        check_target += f" (files matching: {check.file_pattern})"
                    logger.info(f"Checking {repo.name} for pattern '{check.pattern}' in directory {check.directory_path}")
                
                try:
                    # Check if issue already exists for this file/pattern combination
                    existing_issue = await github.check_file_for_issue_exists(
                        repo, check_target, check.pattern
                    )
                    if existing_issue:
                        logger.info(f"Issue already exists for {check_target} pattern in {repo.name}")
                        continue
                    
                    # Get file content(s) to check
                    files_to_check = []
                    search_results = []
                    
                    if check.file_path:
                        # Single file check
                        file_content = await github.get_file_content(repo, check.file_path)
                        if file_content:
                            files_to_check.append(file_content)
                        else:
                            logger.info(f"File {check.file_path} not found in {repo.name}")
                            continue
                    elif check.search_repo:
                        # Use GitHub search API for repository-wide search
                        logger.info(f"Using search API to find '{check.pattern}' in {repo.name}")
                        search_results = await github.search_code_in_repo(repo, check.pattern)
                        if not search_results:
                            logger.info(f"No matches found for pattern '{check.pattern}' in {repo.name} via search API")
                            continue
                    else:
                        # Directory check (traditional method)
                        files_to_check = await github.get_files_in_directory(
                            repo, check.directory_path, check.file_pattern
                        )
                        if not files_to_check:
                            logger.info(f"No files found in directory {check.directory_path} in {repo.name}")
                            continue
                        logger.info(f"Found {len(files_to_check)} files to check in {check.directory_path}")
                    
                    # Check for pattern matches
                    all_matches = []
                    
                    if search_results:
                        # Use search results directly
                        all_matches = search_results
                    else:
                        # Check files individually for pattern matches
                        for file_content in files_to_check:
                            result = github.check_code_pattern(file_content, check.pattern)
                            if result.matched:
                                all_matches.append(result)
                    
                    if all_matches:
                        total_matches += 1
                        total_matched_files = len(all_matches)
                        total_matched_lines = sum(len(match.matched_lines) for match in all_matches)
                        
                        logger.info(f"Found {total_matched_lines} matches across {total_matched_files} files in {repo.name}")
                        
                        # Prepare template variables for multiple files
                        if len(all_matches) == 1:
                            # Single file match
                            match = all_matches[0]
                            template_vars = {
                                "description": check.issue_description,
                                "file_path": match.file_path,
                                "pattern": check.pattern,
                                "matched_lines": match.matched_lines,
                                "line_numbers": match.line_numbers,
                                "repo_url": f"https://github.com/{repo.full_name}",
                                "multiple_files": False
                            }
                        else:
                            # Multiple files match
                            template_vars = {
                                "description": check.issue_description,
                                "pattern": check.pattern,
                                "repo_url": f"https://github.com/{repo.full_name}",
                                "multiple_files": True,
                                "matches": all_matches,
                                "total_files": total_matched_files,
                                "total_lines": total_matched_lines
                            }
                        
                        # Create issue
                        issue = IssuePayload.from_template(
                            title=check.issue_title,
                            template_path="code_check_issue.jinja2",
                            template_vars=template_vars,
                            labels=["code-check"] + check.labels,
                            assignees=check.assignees,
                        )
                        
                        if self.dry_run:
                            logger.info(f"[DRY RUN] Would create issue in {repo.name}: {issue.title}")
                        else:
                            try:
                                created = await github.create_issue_graphql(repo, issue)
                                logger.info(f"Created issue {created['html_url']} for code pattern in {repo.name}")
                                total_issues += 1
                            except Exception as e:
                                import traceback
                                logger.error(f"Failed to create issue in {repo.name} for code pattern: {e}")
                                try:
                                    import httpx
                                    if isinstance(e, httpx.HTTPStatusError) and e.response is not None:
                                        logger.error(f"Response content: {e.response.text}")
                                except Exception:
                                    logger.error("Could not read response content or httpx not available.")
                                logger.error(traceback.format_exc())
                    else:
                        if check.file_path:
                            logger.info(f"No matches found for pattern '{check.pattern}' in {repo.name}/{check.file_path}")
                        elif check.search_repo:
                            logger.info(f"No matches found for pattern '{check.pattern}' in {repo.name} (searched entire repo)")
                        else:
                            logger.info(f"No matches found for pattern '{check.pattern}' in {repo.name}/{check.directory_path}")
                        
                except Exception as e:
                    logger.error(f"Error checking {repo.name}/{check_target}: {e}")
                    continue
        
        logger.info(f"Code pattern scan complete. Checks performed: {total_checks}, Matches found: {total_matches}, Issues created: {total_issues}")

if __name__ == "__main__":
    load_dotenv(override=True)

    # Create parent parser with common arguments
    parent_parser = argparse.ArgumentParser(add_help=False)
    parent_parser.add_argument("--dry-run", action="store_true", help="Log actions without making changes")
    parent_parser.add_argument("--exclude-archived", action="store_true", default=True, help="Exclude archived repos")
    parent_parser.add_argument("--repo", type=str, help="Target specific repository by name")
    parent_parser.add_argument("--org", type=str, help="Only include repos in this organization (e.g. Azure-Samples)")
    parent_parser.add_argument("--repos-yaml", type=str, help="Path to a YAML file that lists repositories to process")

    parser = argparse.ArgumentParser(description="GitHub Repo Maintainer Agent", parents=[parent_parser])
    
    # Add subcommands for different operations
    subparsers = parser.add_subparsers(dest="command", help="Command to run")
    
    # Dependabot command (default behavior)
    dependabot_parser = subparsers.add_parser("dependabot", help="Check Dependabot PRs and create issues for failures", parents=[parent_parser])
    
    # Code check command
    code_check_parser = subparsers.add_parser("code-check", help="Check code patterns in files and create issues", parents=[parent_parser])
    code_check_parser.add_argument("--config", type=str, required=True, help="Path to YAML file containing code check configurations")
    
    args = parser.parse_args()
    
    agent = RepoMaintainerAgent(
        dry_run=args.dry_run, 
        exclude_archived=args.exclude_archived, 
        repo_name=args.repo,
        repos_yaml=args.repos_yaml
    )
    
    # Run the appropriate command
    if args.command == "code-check":
        asyncio.run(agent.check_code_patterns(args.config, org=args.org))
    else:
        # Default behavior (dependabot) or when no command is specified
        asyncio.run(agent.process_all(org=args.org))
