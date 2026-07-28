---
name: kb-publish
description: >
  Publish the wiki vault to a GitHub or GitLab Wiki. Wraps
  .agents/skills/kb-publish/scripts/deploy-gitlab-wiki.py and .agents/skills/kb-publish/scripts/push.sh, which convert the vault's
  nested wiki structure into Gollum-compatible pages and push them.
  Triggers on: "publish the wiki", "deploy the wiki", "push to github wiki",
  "push to gitlab", "publicar el wiki", "desplegar wiki", "sube el wiki", "/kb-publish".
allowed-tools: Read Bash Grep Glob
---

# kb-publish: Deploy the Vault to GitHub or GitLab Wiki

The vault under `wiki/` is the source of truth. The wiki host (GitHub or
GitLab) is a **generated target**: never edit pages there, they are overwritten
on every deploy.

Both platforms run Gollum under the hood, so wikilinks, `_sidebar.md`, and the
git-based deploy mechanics are identical. The only difference is the landing
page filename: GitHub uses `Home.md` (capital H), GitLab uses `home.md`. The
script handles this based on the `target` field in `kb-config.yaml`.

---

## Always dry-run first

```bash
python3 .agents/skills/kb-publish/scripts/deploy-gitlab-wiki.py --dry-run
```

This prepares every page and prints a summary without cloning or pushing.
Read the summary before going further. There is no undo on a push.

## The three modes

| Command | Clones | Pushes | Use when |
|---|---|---|---|
| `python3 .agents/skills/kb-publish/scripts/deploy-gitlab-wiki.py --dry-run` | no | no | always, first |
| `python3 .agents/skills/kb-publish/scripts/deploy-gitlab-wiki.py --no-push` | yes | no | inspecting generated output on disk |
| `python3 .agents/skills/kb-publish/scripts/deploy-gitlab-wiki.py` | yes | yes | the real deploy |

`--no-push` leaves the clone directory in place so you can diff the generated
Gollum pages against what you expected.

---

## Configuration

The wiki repo, branch and VPN precondition come from the `gitlab_wiki` section
of `kb-config.yaml` at the vault root. The scripts refuse to run and name the
problem when `enabled` is false or `repo` is missing.

## VPN is a hard precondition

When `vpn_required` is true, `push.sh` resolves `vpn_host` and **aborts unless
it points at an address under `vpn_private_prefix`**. A public IP means the VPN
is down. Set `vpn_required: false` for a wiki outside a corporate network.

```bash
.agents/skills/kb-publish/scripts/push.sh            # push repo + deploy wiki
.agents/skills/kb-publish/scripts/push.sh --force    # extra args are forwarded to git push
```

If it exits with "VPN NO activa", stop. Do not work around the check —
it exists because pushing to a resolvable-but-wrong host is worse than failing.

Tell the user to connect the VPN and retry. Never suggest editing the check out.

---

## Order of operations

1. Run the lint gate. Publishing a vault that fails its own gate spreads the
   problem to a place other people read:
   ```bash
   python3 .agents/skills/wiki-lint/scripts/run-lint.py
   ```
   Exit code `1` means at least one check failed — see the `kb-setup` skill for
   what each check means.
2. Dry-run the deploy.
3. Show the user the summary and ask before pushing.
4. Push.

---

## Known constraints

- Wikilinks are **case-sensitive** to the lint checkers: `[[keycloak]]`
  resolves, `[[Keycloak]]` is reported dead even though the wiki accepts both.
  Fix the link, not the checker.
- Gollum labels containing `[` or `]` (e.g. Jira titles like `[SEC] ...`) must
  be escaped by the deploy script; if links render as raw `[[...]]` text in
  GitLab, that escaping regressed.
- Numbered page paths need natural sorting, not lexical — otherwise
  `PROJ-10` sorts before `PROJ-2`.
