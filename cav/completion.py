"""Custom shell completion classes for `cav`.

Click's stock completion templates always insert a trailing space after
a `plain` completion. For `cav cp cave:src/<TAB>` we want to descend
into directories tab-by-tab, which requires suppressing the trailing
space when a value ends with `/`. These subclasses ship templates that
honor that.
"""

from click.shell_completion import (
    BashComplete,
    ZshComplete,
    add_completion_class,
)


_BASH_SOURCE = """\
%(complete_func)s() {
    local IFS=$'\\n'
    local response
    local has_dir_suffix=0

    response=$(env COMP_WORDS="${COMP_WORDS[*]}" COMP_CWORD=$COMP_CWORD %(complete_var)s=bash_complete $1)

    for completion in $response; do
        IFS=',' read type value <<< "$completion"

        if [[ $type == 'dir' ]]; then
            COMPREPLY=()
            compopt -o dirnames
        elif [[ $type == 'file' ]]; then
            COMPREPLY=()
            compopt -o default
        elif [[ $type == 'plain' ]]; then
            COMPREPLY+=($value)
            if [[ "$value" == */ ]]; then
                has_dir_suffix=1
            fi
        fi
    done

    if [[ $has_dir_suffix == 1 ]]; then
        compopt -o nospace
        local i
        for i in "${!COMPREPLY[@]}"; do
            if [[ "${COMPREPLY[$i]}" != */ ]]; then
                COMPREPLY[$i]="${COMPREPLY[$i]} "
            fi
        done
    fi

    return 0
}

%(complete_func)s_setup() {
    complete -o nosort -F %(complete_func)s %(prog_name)s
}

%(complete_func)s_setup;
"""


_ZSH_SOURCE = """\
#compdef %(prog_name)s

%(complete_func)s() {
    local -a completions
    local -a completions_nospace
    local -a completions_with_descriptions
    local -a response
    (( ! $+commands[%(prog_name)s] )) && return 1

    response=("${(@f)$(env COMP_WORDS="${words[*]}" COMP_CWORD=$((CURRENT-1)) %(complete_var)s=zsh_complete %(prog_name)s)}")

    for type key descr in ${response}; do
        if [[ "$type" == "plain" ]]; then
            if [[ "$key" == */ ]]; then
                completions_nospace+=("$key")
            elif [[ "$descr" == "_" ]]; then
                completions+=("$key")
            else
                completions_with_descriptions+=("$key":"$descr")
            fi
        elif [[ "$type" == "dir" ]]; then
            _path_files -/
        elif [[ "$type" == "file" ]]; then
            _path_files -f
        fi
    done

    if [ -n "$completions_with_descriptions" ]; then
        _describe -V unsorted completions_with_descriptions -U
    fi

    if [ -n "$completions" ]; then
        compadd -U -V unsorted -a completions
    fi

    if [ -n "$completions_nospace" ]; then
        compadd -U -S '' -V unsorted -a completions_nospace
    fi
}

if [[ $zsh_eval_context[-1] == loadautofunc ]]; then
    %(complete_func)s "$@"
else
    compdef %(complete_func)s %(prog_name)s
fi
"""


class CavBashComplete(BashComplete):
    name = "bash"
    source_template = _BASH_SOURCE


class CavZshComplete(ZshComplete):
    name = "zsh"
    source_template = _ZSH_SOURCE


def register() -> None:
    """Register the custom completion classes (idempotent)."""
    add_completion_class(CavBashComplete, "bash")
    add_completion_class(CavZshComplete, "zsh")
