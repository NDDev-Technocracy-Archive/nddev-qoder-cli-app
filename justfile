set shell := ["bash", "-euo", "pipefail", "-c"]

# Lists the repository commands.
default:
    @just --list

# Checks public contracts, workflow safety and source/history secrets without running the manager.
check:
    just --fmt --check
    python3 cli-tools/validate_public_contracts.py
    python3 -m json.tool tools/ci-tools.json > /dev/null
    actionlint
    zizmor --offline --no-progress .github
    gitleaks dir --no-banner --redact .
    gitleaks git --no-banner --redact --log-opts=--all .

# Formats the repository command file.
fix:
    just --fmt

# Runs the complete source and retained-history validation.
verify: check
