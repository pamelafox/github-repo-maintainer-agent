import os
import sys

# Add parent directory to path to allow importing from the main package
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from models import IssuePayload


def test_issue_payload_from_template_with_logs():
    """Test creating an IssuePayload from a template with logs."""
    # Setup test variables
    title = "Test Issue"
    template_path = "issue_with_logs.jinja2"
    template_vars = {
        "pr_url": "https://github.com/owner/repo/pull/123",
        "summary": "Test summary",
        "related_logs": ["Log entry 1", "Log entry 2"]
    }
    labels = ["test-label"]
    assignees = ["test-user"]
    
    # Create issue payload from template
    issue = IssuePayload.from_template(
        title=title,
        template_path=template_path,
        template_vars=template_vars,
        labels=labels,
        assignees=assignees
    )
    
    # Verify the issue was created correctly
    assert issue.title == title
    assert issue.labels == labels
    assert issue.assignees == assignees
    
    # Verify the body contains expected content from the template
    assert template_vars["pr_url"] in issue.body
    assert template_vars["summary"] in issue.body
    assert "Relevant logs:" in issue.body
    assert "Python Virtual Environment Setup" in issue.body
    assert "python -m venv .venv" in issue.body
    for log in template_vars["related_logs"]:
        assert log in issue.body


def test_issue_payload_from_template_no_logs():
    """Test creating an IssuePayload from a template without logs."""
    # Setup test variables
    title = "Test Issue No Logs"
    template_path = "issue_no_logs.jinja2"
    template_vars = {
        "pr_url": "https://github.com/owner/repo/pull/456"
    }
    labels = ["no-logs-label"]
    assignees = ["no-logs-user"]
    
    # Create issue payload from template
    issue = IssuePayload.from_template(
        title=title,
        template_path=template_path,
        template_vars=template_vars,
        labels=labels,
        assignees=assignees
    )
    
    # Verify the issue was created correctly
    assert issue.title == title
    assert issue.labels == labels
    assert issue.assignees == assignees
    
    # Verify the body contains expected content from the template
    assert template_vars["pr_url"] in issue.body
    assert "no logs are available" in issue.body.lower()
    assert "Python Virtual Environment Setup" in issue.body
    assert "python -m venv .venv" in issue.body
