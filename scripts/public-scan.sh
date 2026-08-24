#!/usr/bin/env bash
# 扫描公开发布候选字节，只输出 finding 类别和相对文件名。

set -u

scan_error() {
    printf 'scan-error: %s\n' "$1" >&2
    exit 2
}

rg_match() {
    rg "$@" >/dev/null 2>&1
    status=$?
    if ((status > 1)); then
        scan_error content-scan-failed
    fi
    return "$status"
}

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P) || scan_error script-path-failed
repo_root=${1:-"$script_dir/.."}

for dependency in git rg; do
    command -v "$dependency" >/dev/null 2>&1 || scan_error "missing-$dependency"
done

top_level=$(git -C "$repo_root" rev-parse --show-toplevel 2>/dev/null) || scan_error invalid-repository
repo_root=$top_level

candidate_file=$(mktemp) || scan_error temporary-file-failed
content_file=$(mktemp) || {
    rm -f -- "$candidate_file"
    scan_error temporary-file-failed
}
trap 'rm -f -- "$candidate_file" "$content_file"' EXIT

git -C "$repo_root" ls-files -z --cached >"$candidate_file" 2>/dev/null || scan_error candidate-list-failed
mapfile -d '' tracked <"$candidate_file"
git -C "$repo_root" ls-files -z --others --exclude-standard >"$candidate_file" 2>/dev/null || scan_error candidate-list-failed
mapfile -d '' untracked <"$candidate_file"

declare -A tracked_paths
candidates=()
for relative_path in "${tracked[@]}"; do
    tracked_paths["$relative_path"]=1
    candidates+=("$relative_path")
done
for relative_path in "${untracked[@]}"; do
    candidates+=("$relative_path")
done

home_path=${HOME%/}
home_name=${home_path##*/}
findings=0

report() {
    printf '%s: %s\n' "$1" "$2"
    findings=$((findings + 1))
}

for relative_path in "${candidates[@]}"; do
    file_path=$repo_root/$relative_path
    if [[ -n ${tracked_paths["$relative_path"]+tracked} ]]; then
        git -C "$repo_root" show ":$relative_path" >"$content_file" 2>/dev/null || scan_error index-read-failed
    elif [[ -L "$file_path" ]]; then
        readlink -- "$file_path" >"$content_file" 2>/dev/null || scan_error worktree-read-failed
    elif [[ -f "$file_path" ]]; then
        cp -- "$file_path" "$content_file" 2>/dev/null || scan_error worktree-read-failed
    else
        scan_error worktree-read-failed
    fi

    personal_path=false
    for value in "$home_path" "/Users/$home_name" "C:\\Users\\$home_name" "C:/Users/$home_name"; do
        if [[ -n "$value" ]] && rg_match --quiet --fixed-strings -- "$value" "$content_file"; then
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
            if rg_match --quiet --ignore-case --pcre2 -- \
                '(?:api[_-]?key|access[_-]?token|private[_-]?token|password|passwd|secret|credential|authorization)\s*[=:]\s*[\x22\x27]?[A-Za-z0-9_./+@:-]{8,}' \
                "$content_file"; then
                credential_assignment=true
            fi
            ;;
    esac
    if [[ $credential_name == true || $credential_assignment == true ]] ||
        rg_match --quiet --pcre2 -- '(?:-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[A-Z0-9]{16})' "$content_file"; then
        report credential "$relative_path"
        continue
    fi

    if rg_match --quiet --ignore-case --pcre2 -- \
        '[a-z][a-z0-9+.-]*://[^/@:\s\x22\x27<>?#]+(?::[^/@\s\x22\x27<>?#]*)?@(?!(?:[A-Za-z0-9-]+\.)*invalid\.?(?=[^A-Za-z0-9.-]|$))(?:[A-Za-z0-9.-]+|\[[0-9A-F:.]+\])' \
        "$content_file"; then
        report userinfo-url "$relative_path"
        continue
    fi

    case "$lowercase" in
        *.log|*.jsonl|latest.json|*.lock|*.pid|*.sqlite|*.sqlite3|*.db)
            report local-artifact "$relative_path"
            continue
            ;;
    esac

    if [[ -z ${tracked_paths["$relative_path"]+tracked} ]]; then
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
