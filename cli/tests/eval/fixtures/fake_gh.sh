#!/bin/bash
LOG="__LOG__"
FIXTURES="__FIXTURES__"

echo "$*" >> "$LOG"

_arg_after() { local k="$1"; shift; echo "$*" | awk -v k="$k" '{for(i=1;i<NF;i++) if($i==k) {print $(i+1); exit}}'; }

case "$1 $2" in
  "pr list")
    SEARCH=$(_arg_after --search "$@")
    REPO=$(_arg_after -R "$@" | tr '/' '_')
    FILE="$FIXTURES/pr_list_${REPO}_${SEARCH}.json"
    [ -f "$FILE" ] && cat "$FILE" || echo "[]"
    ;;
  "pr view")
    REPO=$(_arg_after -R "$@" | tr '/' '_')
    FILE="$FIXTURES/pr_${REPO}_$3.json"
    [ -f "$FILE" ] && cat "$FILE" || echo "{}"
    ;;
  "pr diff")
    REPO=$(_arg_after -R "$@" | tr '/' '_')
    FILE="$FIXTURES/pr_diff_${REPO}_$3.diff"
    [ -f "$FILE" ] && cat "$FILE" || echo ""
    ;;
  "issue view")
    REPO=$(_arg_after -R "$@" | tr '/' '_')
    FILE="$FIXTURES/issue_${REPO}_$3.json"
    [ -f "$FILE" ] && cat "$FILE" || echo "{}"
    ;;
  "pr comment"|"pr edit"|"pr create"|"pr ready")
    exit 0
    ;;
  "issue comment"|"issue edit")
    exit 0
    ;;
  "api graphql")
    exit 0
    ;;
  "api"*)
    exit 0
    ;;
  *)
    echo "[]"
    ;;
esac
