from pathlib import Path
from typing import Literal

from jinja2 import Environment, FileSystemLoader
from pydantic import BaseModel, HttpUrl


class RepoConfig(BaseModel):
    """Configuration for a repository in repos.yaml"""
    name: str
    owner: str


class RepositoriesConfig(BaseModel):
    """Configuration for repositories in repos.yaml"""
    personal: list[str] | None = None
    organizations: dict[str, list[str]] | None = None


class Repository(BaseModel):
    name: str
    owner: str
    archived: bool
    is_personal: bool = False  # Track if this is a personal repo
    
    @property
    def full_name(self) -> str:
        """Return the full name of the repository in the format owner/name."""
        return f"{self.owner}/{self.name}"


class PullRequest(BaseModel):
    number: int
    author: str
    url: HttpUrl
    title: str


class CheckRun(BaseModel):
    name: str
    status: Literal['queued','in_progress','completed']
    conclusion: Literal['success','failure','cancelled','timed_out','action_required']
    url: HttpUrl | None = None
    output: dict | None = None
    
    def get_output_text(self) -> str | None:
        """Returns the text from the output, if available."""
        if self.output and self.output.get('text'):
            return self.output.get('text')
        return None
    
    def get_output_summary(self) -> str | None:
        """Returns the summary from the output, if available."""
        if self.output and self.output.get('summary'):
            return self.output.get('summary')
        return None


class AnalyzeFailureInput(BaseModel):
    pr_url: HttpUrl
    check_runs: list[CheckRun]


class AnalyzeFailureOutput(BaseModel):
    type: Literal['dependency_conflict','build_error','test_failure','install_error','other']
    summary: str
    related_logs: list[str]


class CodeCheckConfig(BaseModel):
    """Configuration for checking specific code patterns in files"""
    file_path: str | None = None  # Path to a specific file in the repository
    directory_path: str | None = None  # Path to a directory to check all files within
    file_pattern: str | None = None  # Regex pattern to match filenames (when using directory_path)
    search_repo: bool = False  # If True, search entire repository using GitHub's search API
    check_missing: bool = False  # If True, create issue when file_path does NOT exist
    pattern: str | None = None   # The query or pattern to search for (used for file selection/search API)
    content_pattern: str | None = None  # Optional pattern to validate within matched files
    issue_if_missing: bool = False  # If True, create an issue if the pattern is NOT found (inverse logic)
    issue_title: str  # Title for the issue to create
    issue_description: str  # Description for the issue
    labels: list[str] = []  # Labels to apply to the issue
    assignees: list[str] = []  # Users to assign the issue to

    def model_post_init(self, __context):
        """Validate configuration options"""
        options_count = sum([
            bool(self.file_path),
            bool(self.directory_path),
            bool(self.search_repo)
        ])
        
        if options_count == 0:
            raise ValueError("Must specify one of: file_path, directory_path, or search_repo=True")
        if options_count > 1:
            raise ValueError("Cannot specify more than one of: file_path, directory_path, or search_repo=True")
        
        # check_missing can only be used with file_path
        if self.check_missing and not self.file_path:
            raise ValueError("check_missing=True can only be used with file_path")
        
        # pattern is required unless check_missing is True
        if not self.check_missing and not self.pattern:
            raise ValueError("pattern is required unless check_missing=True")

        # issue_if_missing cannot be combined with check_missing (they serve different purposes)
        if self.issue_if_missing and self.check_missing:
            raise ValueError("issue_if_missing cannot be used with check_missing")

        # issue_if_missing currently only supported for directory_path or file_path (not search_repo)
        if self.issue_if_missing and self.search_repo:
            raise ValueError("issue_if_missing is not supported with search_repo=True (use directory_path or file_path)")


class FileContent(BaseModel):
    """Represents the content of a file from a repository"""
    path: str
    content: str
    sha: str


class DirectoryItem(BaseModel):
    """Represents an item in a directory listing"""
    name: str
    path: str
    type: Literal['file', 'dir', 'symlink', 'submodule']
    sha: str
    size: int | None = None
    download_url: str | None = None


class CodeMatchResult(BaseModel):
    """Result of checking a file for a specific code pattern"""
    file_path: str
    pattern: str
    matched: bool
    line_numbers: list[int] = []  # Line numbers where matches were found
    matched_lines: list[str] = []  # The actual lines that matched


class IssuePayload(BaseModel):
    title: str
    body: str
    labels: list[str]
    assignees: list[str]
    
    @classmethod
    def from_template(cls, title: str, template_path: str, template_vars: dict, labels: list[str], assignees: list[str]) -> "IssuePayload":
        """Create an issue payload using a Jinja template for the body.
        
        Args:
            title: The issue title
            template_path: Path to the Jinja template file
            template_vars: Dictionary of variables to pass to the template
            labels: List of labels to apply to the issue
            assignees: List of users to assign to the issue
            
        Returns:
            An IssuePayload instance with the rendered template as the body
        """

        
        templates_dir = Path(__file__).parent / "templates"
        env = Environment(loader=FileSystemLoader(templates_dir))
        template = env.get_template(template_path)
        body = template.render(**template_vars)
        
        return cls(
            title=title,
            body=body,
            labels=labels,
            assignees=assignees
        )
