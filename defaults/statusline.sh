#!/bin/bash
read -r input

# Find persist.json walking up from CWD
persist_file=""
dir="$(pwd)"
while [ "$dir" != "$HOME" ] && [ "$dir" != "/" ]; do
    if [ -f "$dir/.claude/persist.json" ]; then
        persist_file="$dir/.claude/persist.json"
        break
    fi
    dir="$(dirname "$dir")"
done

persist=""
if [ -n "$persist_file" ]; then
    read -r iteration total deadline < <(jq -r \
        '[(.iteration // 0), (.total // 0), (.deadline // 0)] | map(tostring) | join(" ")' \
        "$persist_file" 2>/dev/null)
    if [ "$total" -gt 0 ] 2>/dev/null; then
        persist="persist $iteration/$total"
    elif [ "${deadline%.*}" -gt 0 ] 2>/dev/null; then
        now=$(date +%s)
        remaining=$(( ${deadline%.*} - now ))
        if [ "$remaining" -le 0 ]; then
            persist="persist #$iteration expired"
        else
            hours=$((remaining / 3600))
            minutes=$(( (remaining % 3600) / 60 ))
            if [ "$hours" -gt 0 ]; then
                persist=$(printf 'persist #%s ~%dh%02dm' "$iteration" "$hours" "$minutes")
            else
                persist="persist #$iteration ~${minutes}m"
            fi
        fi
    else
        persist="persist #$iteration"
    fi
fi

line="Coding Cave #${CODING_CAVE_SESSION_INDEX:-?} ${CODING_CAVE_PROJECT_PATH:-?}/"
if [ -n "$persist" ]; then
    line="$line [$persist]"
fi
echo "$line"
