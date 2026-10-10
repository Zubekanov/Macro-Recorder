#!/usr/bin/env bash
# Print the applications-menu entry with the uv and repository paths filled in.
#
#   desktop-entry.sh TEMPLATE UV REPO > macro-recorder.desktop
#
# The template's Exec line holds "@UV@" and "@REPO@" inside double quotes and
# its Path line holds @REPO@ bare.  Each value is escaped for where it lands
# (Desktop Entry Specification, "The Exec key"): inside a quoted Exec argument
# a backslash becomes \\\\, and ", $ and ` become \\", \\$ and \\`; % becomes
# %% anywhere in Exec; in Path a backslash becomes \\.  No sed is involved, so
# & and | in a path are plain characters.

set -euo pipefail

if [ "$#" -ne 3 ]; then
    echo "usage: $0 TEMPLATE UV REPO" >&2
    exit 2
fi
template=$1
uv=$2
repo=$3

# Bash 5.2 treats & in a ${var//pattern/replacement} replacement as the match
# unless this is off.
shopt -u patsub_replacement 2>/dev/null || true

exec_arg() {
    local v=$1
    local bs='\' q='"' dollar='$' tick='`'
    v=${v//"$bs"/"$bs$bs$bs$bs"}
    v=${v//"$q"/"$bs$bs$q"}
    v=${v//"$dollar"/"$bs$bs$dollar"}
    v=${v//"$tick"/"$bs$bs$tick"}
    v=${v//%/%%}
    printf '%s' "$v"
}

string_value() {
    local v=$1
    local bs='\'
    printf '%s' "${v//"$bs"/"$bs$bs"}"
}

uv_exec=$(exec_arg "$uv")
repo_exec=$(exec_arg "$repo")
repo_path=$(string_value "$repo")

while IFS= read -r line || [ -n "$line" ]; do
    case $line in
        Exec=*)
            line=${line//@UV@/"$uv_exec"}
            line=${line//@REPO@/"$repo_exec"}
            ;;
        *)
            line=${line//@UV@/"$(string_value "$uv")"}
            line=${line//@REPO@/"$repo_path"}
            ;;
    esac
    printf '%s\n' "$line"
done < "$template"
