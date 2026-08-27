#!/usr/bin/env bash
# Coding Cave default statusline: just the agent byline.
# Segments like persist status or model/context are composed by the
# user's dotfiles, which override statusLine.command with its own script.
exec cav-host agent-tag
