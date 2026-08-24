#!/usr/bin/env bash
set -euo pipefail

usage() {
    echo "Usage: install-skill.sh --host codex|claude|gemini [--bin]" >&2
}

fail_usage() {
    echo "ERROR: $1" >&2
    usage
    exit 2
}

host=""
install_bin=0

# Parse every option before creating directories or links.
while [ "$#" -gt 0 ]; do
    case "$1" in
        --host)
            [ -z "$host" ] || fail_usage "--host may be specified only once"
            [ "$#" -ge 2 ] || fail_usage "--host requires a value"
            host="$2"
            shift 2
            ;;
        --bin)
            [ "$install_bin" -eq 0 ] || fail_usage "--bin may be specified only once"
            install_bin=1
            shift
            ;;
        *)
            fail_usage "unknown argument: $1"
            ;;
    esac
done

case "$host" in
    codex)
        host_directory=".agents"
        ;;
    claude)
        host_directory=".claude"
        ;;
    gemini)
        host_directory=".gemini"
        ;;
    "")
        fail_usage "--host is required"
        ;;
    *)
        fail_usage "unsupported host: $host"
        ;;
esac

[ -n "${HOME:-}" ] || fail_usage "HOME is required"

script_directory="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
repository_root="$(CDPATH= cd -- "$script_directory/.." && pwd -P)"
cli_source="$repository_root/scripts/data-infra-sync"
skill_target="$HOME/$host_directory/skills/data-infra-sync-skill"
binary_target="$HOME/.local/bin/data-infra-sync"

[ -x "$cli_source" ] || fail_usage "CLI entry is not executable: $cli_source"

is_same_link() {
    [ -L "$1" ] && [ "$(readlink -f -- "$1")" = "$2" ]
}

validate_target() {
    target="$1"
    source="$2"
    if is_same_link "$target" "$source"; then
        return 0
    fi
    if [ -e "$target" ] || [ -L "$target" ]; then
        echo "ERROR: refusing to replace existing target: $target" >&2
        exit 1
    fi
}

validate_parent_path() {
    local parent_path next_parent
    parent_path="$(dirname -- "$1")"
    while true; do
        if { [ -e "$parent_path" ] || [ -L "$parent_path" ]; } && [ ! -d "$parent_path" ]; then
            echo "ERROR: install parent is not a directory: $parent_path" >&2
            exit 1
        fi
        next_parent="$(dirname -- "$parent_path")"
        [ "$next_parent" != "$parent_path" ] || break
        parent_path="$next_parent"
    done
}

install_link() {
    source="$1"
    target="$2"
    if is_same_link "$target" "$source"; then
        return 0
    fi
    mkdir -p -- "$(dirname -- "$target")"
    ln -s -- "$source" "$target"
}

# Validate every requested destination before the first filesystem write.
validate_target "$skill_target" "$repository_root"
validate_parent_path "$skill_target"
if [ "$install_bin" -eq 1 ]; then
    validate_target "$binary_target" "$cli_source"
    validate_parent_path "$binary_target"
fi

install_link "$repository_root" "$skill_target"
if [ "$install_bin" -eq 1 ]; then
    install_link "$cli_source" "$binary_target"
fi

echo "Installed data-infra-sync-skill for $host."
