# Instructions for agents

## Running a code check

Run a code check with:

```bash
.venv/bin/python agent.py code-check --config <code_check_file.yaml> --repos-yaml repos.yaml --dry-run
```

## Adding a new code check

When adding a new code check, follow the example from code_checks.yaml.example
and add the new code check to the ./checks/ folder.

### Code check pattern matching mechanics

Understanding how the agent matches patterns helps you author effective checks.

#### Matching modes

- The agent loads each target file and splits it into lines.
- It attempts to compile your `pattern` as a regular expression with the `re.IGNORECASE` flag.
- If the regex compiles successfully, each line is tested independently (no multi-line context) — a match anywhere in the line counts.
- If the pattern is NOT a valid regex (compilation error), it falls back to a case-insensitive literal substring search per line.

#### Important limitations

- No multi-line regex: `.` will NOT cross newline boundaries because matching is performed line-by-line, not with a single multi-line string. Constructs like `.*` will only consume characters within one line.
- Lookarounds, groups, alternation, etc. are supported (as long as they compile) but still line-scoped.
- Anchors (`^`, `$`) refer to the beginning / end of an individual line, not the entire file.
- There is currently no flag control (e.g., DOTALL / MULTILINE) exposed via configuration.

#### Repository-wide search (`search_repo: true`)

- When `search_repo: true` is set, the GitHub Code Search API is used with the raw text of `pattern` (NOT treated as a regex by GitHub’s side — it is a plain string query unless GitHub natively interprets operators).
- The agent then fetches each matching file and does a simple substring check for the same text to confirm matches and extract line numbers.
- Because of this, for repository-wide searches you should normally use a simple literal fragment (e.g. `from imp import` or `actions/checkout@v1`).

#### Choosing the right approach

| Goal | Recommended fields |
|------|--------------------|
| Check one file exactly | `file_path: "requirements.txt"` + regex or literal `pattern` |
| Check every file in a folder | `directory_path: "src"` (optional `file_pattern`) |
| Limit to certain extensions | Add `file_pattern: "\\.ya?ml$"` or similar |
| Fast literal match anywhere in repo | `search_repo: true` (pattern as literal) |
| Need regex features (captures, alternation) | Use `file_path` or `directory_path` (NOT `search_repo`) |

#### Escaping tips

- In YAML, backslashes must often be escaped. For example to match `flask==1.*` with a regex you might write: `flask==1\\..*`
- To match a literal dot, escape it twice in YAML regex context: `myfile\\.json`
- To match backslashes themselves, double them again: `C:\\Path` would become `C:\\\\Path` in YAML.

#### Strategies for pseudo multi-line detection

Because multi-line detection isn’t supported directly, use one of these workarounds:

1. Match only the leading keyword and rely on a human to verify (e.g., detect `pipeline:` to flag deprecated Azure `pipeline:` blocks).
2. Write two separate checks if you need to ensure two distinct lines exist (may create two issues though).
3. Prefer a structural linter or a future enhancement if exact multi-line validation becomes necessary.

#### Examples

Minimal single-file regex check:

```yaml
code_checks:
  - file_path: "requirements.txt"
    pattern: "urllib3==1\\..*"
    issue_title: "Outdated urllib3 1.x detected"
    issue_description: |
      urllib3 2.x includes improvements...
```

Directory + filename filter:

```yaml
code_checks:
  - directory_path: ".github/workflows"
    file_pattern: "\\.ya?ml$"
    pattern: "actions/checkout@v[12]"
    issue_title: "Outdated checkout action"
    issue_description: |
      Please upgrade to actions/checkout@v4.
```

Repo-wide literal search:

```yaml
code_checks:
  - search_repo: true
    pattern: "from imp import"
    issue_title: "Deprecated 'imp' module usage"
    issue_description: |
      Replace with importlib.
```

#### Validation reminder

Always validate new check files before running the agent:

```bash
python - <<'PY'
import yaml
from models import CodeCheckConfig
with open('my_new_check.yaml') as f:
    data = yaml.safe_load(f)
checks = [CodeCheckConfig.model_validate(c) for c in data.get('code_checks', [])]
print(f"Loaded {len(checks)} checks. First: {checks[0]}")
PY
```

Make sure you run from the virtual environment in `.venv` so that `yaml` and `pydantic` are available.