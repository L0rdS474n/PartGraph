# Contributing to PartGraph

Thanks for your interest in PartGraph. This document covers the development
setup, the testing policy, and the rules for pull requests.

## Development setup

PartGraph targets **Python 3.12** and a local **Dgraph** instance running in
Docker.

```bash
# Create the environment (conda recommended; a venv works too)
conda env create -f environment.yml
conda activate partgraph

# Install in editable mode with the development extras (pytest, ruff)
pip install -e ".[dev]"
```

Start the database when you need it:

```bash
partgraph db up
partgraph db apply-schema
# ... work ...
partgraph db down            # data is preserved (no -v)
```

## Test-first policy

PartGraph is developed **test-first**. The test suite under `tests/` is the
contract:

1. Behaviour is specified by Given/When/Then tests **before** implementation.
2. Implementation satisfies only what the tests specify.
3. No untested behaviour is added to core functionality.

Run the linter and tests before opening a PR:

```bash
ruff check .
pytest -m "not integration"     # unit tests — no infrastructure required
pytest -m integration           # integration tests — requires `partgraph db up`
```

### Unit vs integration tests

- **Unit tests** are pure and run anywhere (this is what CI runs).
- **Integration tests** are marked `@pytest.mark.integration` and require a
  **local Docker setup** with a running Dgraph instance (`partgraph db up`).
  They are executed locally only and are never run in CI.

## Pull-request rules

- **One objective per PR.** Keep the change set small and reviewable.
- **CI must pass.** Linting (`ruff`) and the unit-test job are required checks.
- Write or update tests for any behaviour change; document non-trivial risk in
  the PR description.

## Branch protection

The `main` branch is protected: changes land via pull request and the CI check
must pass. While the project is maintained solo, **0 approvals** are required
(the maintainer merges their own PRs). When more contributors join, the
required-approvals count will be raised to 1 (CODEOWNERS is already in place).
