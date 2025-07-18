from typing import Literal

from pydantic import BaseModel, HttpUrl


class Repository(BaseModel):
    name: str
    owner: str
    archived: bool


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
