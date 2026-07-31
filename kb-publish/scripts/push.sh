#!/usr/bin/env bash
# push.sh — pushes to the main repo and deploys the wiki to GitLab
# Usage: ./.agents/skills/kb-publish/scripts/push.sh [git push args]

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# This script lives inside a skill, so its depth below the repo root depends on
# where the skill is installed. Walk up looking for the markers instead of
# counting levels, which breaks the moment the skill moves.
# Both wiki/ AND .git are required: there is a skill directory named "wiki"
# under .agents/skills/, so the wiki/ marker alone resolves to the skills
# directory instead of the repo root.
REPO_DIR="$SCRIPT_DIR"
while [ "$REPO_DIR" != "/" ] && ! { [ -d "$REPO_DIR/wiki" ] && [ -e "$REPO_DIR/.git" ]; }; do
    REPO_DIR="$(dirname "$REPO_DIR")"
done
if ! { [ -d "$REPO_DIR/wiki" ] && [ -e "$REPO_DIR/.git" ]; }; then
    echo "❌ Repo root not found: no ancestor has both wiki/ and .git"
    exit 1
fi
cd "$REPO_DIR"

# Project configuration — nothing project-specific lives in this script
CFG="$REPO_DIR/kb-config.json"
if [ ! -f "$CFG" ]; then
    echo "❌ Config not found: $CFG"
    echo "   Copy kb-config.example.json from the kb-skills repo and fill it in."
    exit 1
fi
read_cfg() { python3 -c "
import json,sys
cfg=json.load(open('$CFG')).get('wiki_publish',{})
v=cfg.get('$1', '$2')
print('' if v is None else ('true' if v is True else ('false' if v is False else v)))"; }

VPN_REQUIRED=$(read_cfg vpn_required true)
VPN_HOST=$(read_cfg vpn_host "")
VPN_PREFIX=$(read_cfg vpn_private_prefix "10.")

# Verify VPN — the host must resolve to a private IP
if [ "$VPN_REQUIRED" = "true" ]; then
    if [ -z "$VPN_HOST" ]; then
        echo "❌ wiki_publish.vpn_required is true but vpn_host is missing in kb-config.json"
        exit 1
    fi
    GIT_IP=$(host "$VPN_HOST" 2>/dev/null | awk '/has address/ {print $NF}' | head -1)
    if [ -z "$GIT_IP" ]; then
        echo "❌ Cannot resolve $VPN_HOST — is DNS working?"
        exit 1
    fi
    case "$GIT_IP" in
        "$VPN_PREFIX"*)
            echo "🔒 VPN active ($VPN_HOST → $GIT_IP)" ;;
        *)
            echo "⚠️  VPN NOT active — $VPN_HOST resolves to $GIT_IP (outside $VPN_PREFIX*)"
            echo "   Connect the VPN before pushing."
            exit 1 ;;
    esac
fi

# Push to the main repo
echo "📦 Pushing to the main repo..."
git push "$@"

# If there are changes in wiki/, deploy
CHANGED=$(git diff --name-only HEAD~1 HEAD 2>/dev/null | grep "^wiki/" | head -1)
if [ -n "$CHANGED" ]; then
    echo "📋 Changes in wiki/ detected — deploying to GitLab Wiki..."
    python3 "$SCRIPT_DIR/deploy-wiki.py"
else
    echo "ℹ No changes in wiki/ — skip deploy"
fi