#!/usr/bin/env bash
# 扫描公开发布候选文件，只输出 finding 类别和相对文件名。

set -u

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P) || exit 2
repo_root=${1:-"$script_dir/.."}

for dependency in git rg; do
    if ! command -v "$dependency" >/dev/null 2>&1; then
        printf 'scan-error: missing-%s\n' "$dependency" >&2
        exit 2
    fi
done

if ! top_level=$(git -C "$repo_root" rev-parse --show-toplevel 2>/dev/null); then
    printf 'scan-error: invalid-repository\n' >&2
    exit 2
fi
repo_root=$top_level

candidate_file=$(mktemp) || {
    printf 'scan-error: temporary-file-failed\n' >&2
    exit 2
}
trap 'rm -f -- "$candidate_file"' EXIT
if ! git -C "$repo_root" ls-files -z --cached --others --exclude-standard >"$candidate_file" 2>/dev/null; then
    printf 'scan-error: candidate-list-failed\n' >&2
    exit 2
fi
mapfile -d '' candidates <"$candidate_file"

home_path=${HOME%/}
home_name=${home_path##*/}
findings=0

report() {
    printf '%s: %s\n' "$1" "$2"
    findings=$((findings + 1))
}

for relative_path in "${candidates[@]}"; do
    file_path=$repo_root/$relative_path
    if [[ ! -f "$file_path" ]]; then
        continue
    fi

    personal_path=false
    for value in "$home_path" "/Users/$home_name" "C:\\Users\\$home_name" "C:/Users/$home_name"; do
        if [[ -n "$value" ]] && rg --quiet --fixed-strings -- "$value" "$file_path"; then
            personal_path=true
            break
        fi
    done
    if [[ $personal_path == true ]]; then
        report personal-path "$relative_path"
        continue
    fi

    basename=${relative_path##*/}
    lowercase=${basename,,}
    credential_name=false
    case "$lowercase" in
        .env|.env.*|.git-credentials|.netrc|.npmrc|.pypirc|credentials|credentials.*|id_rsa|id_rsa.*|id_ed25519|id_ed25519.*|*.pem|*.p12|*.pfx)
            credential_name=true
            ;;
    esac
    credential_assignment=false
    case "$lowercase" in
        *.env|*.ini|*.conf|*.cfg|*.yaml|*.yml|*.json|*.toml|*.properties|*.xml|*.config)
            if rg --quiet --ignore-case --pcre2 -- \
                '(?:api[_-]?key|access[_-]?token|private[_-]?token|password|passwd|secret|credential|authorization)\s*[=:]\s*[\x22\x27]?[A-Za-z0-9_./+@:-]{8,}' \
                "$file_path"; then
                credential_assignment=true
            fi
            ;;
    esac
    if [[ $credential_name == true || $credential_assignment == true ]] ||
        rg --quiet --pcre2 -- '(?:-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[A-Z0-9]{16})' "$file_path"; then
        report credential "$relative_path"
        continue
    fi

    if rg --quiet --pcre2 -- \
        'https?://[^/@\s]+:[^/@\s]+@(?!(?:[A-Za-z0-9-]+\.)*invalid(?=[:/\s]|$))[^/\s]+' \
        "$file_path"; then
        report userinfo-url "$relative_path"
        continue
    fi

    case "$lowercase" in
        *.log|*.jsonl|latest.json|*.lock|*.pid|*.sqlite|*.sqlite3|*.db)
            report local-artifact "$relative_path"
            continue
            ;;
    esac

    if ! git -C "$repo_root" ls-files --error-unmatch -- "$relative_path" >/dev/null 2>&1; then
        case "$lowercase" in
            *.py|*.sh|*.md|*.rst|*.txt|*.json|*.yaml|*.yml|*.toml|*.ini|*.cfg|*.conf|*.c|*.h|*.cc|*.cpp|*.go|*.rs|*.js|*.jsx|*.ts|*.tsx)
                report untracked-source "$relative_path"
                ;;
        esac
    fi
done

if ((findings > 0)); then
    exit 1
fi
exit 0
