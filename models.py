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
