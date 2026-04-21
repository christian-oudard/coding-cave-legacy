#!/bin/bash
# Coding Cave statusline script.
# Outputs the agent label, plus persist session status if active.
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
    read -r iteration total deadline started lock < <(jq -r \
        '[(.iteration // 0), (.total // 0), (.deadline // 0), (.started // 0), (.lock // false)] | map(tostring) | join(" ")' \
        "$persist_file" 2>/dev/null)
    fmt_dur() {
        local s=$1 h m
        h=$((s / 3600)); m=$(( (s % 3600) / 60 ))
        if [ "$h" -gt 0 ]; then printf '%dh%02dm' "$h" "$m"
        else printf '%dm' "$m"; fi
    }
    prefix="persist"
    [ "$lock" = "true" ] && prefix="persist --lock"
    if [ "$total" -gt 0 ] 2>/dev/null; then
        persist="$prefix $iteration/$total"
    elif [ "${deadline%.*}" -gt 0 ] 2>/dev/null; then
        now=$(date +%s)
        elapsed=$(( now - ${started%.*} ))
        persist=$(printf '%s %s/%s' "$prefix" "$(fmt_dur $elapsed)" "$(fmt_dur $((${deadline%.*} - ${started%.*})))")
    else
        persist="$prefix forever"
    fi
fi

line="Coding Cave - ${CODING_CAVE_AGENT_NAME:-?}"
if [ -n "$persist" ]; then
    line="$line [$persist]"
fi
echo "$line"
