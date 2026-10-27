# Proving a gate fails

AGENTS.md hard rule 3: *a gate that has never failed has never been tested.*
Before trusting any new or changed lint check, inject the fault it exists to
catch and observe a non-zero exit. Investigation only — keep throwaway vaults
out of the repo and delete them after recording the evidence.

This rule is not ceremony. A fusion test once passed 54 tests while the
implementation it guarded was broken, because the injected fault was
unreachable from the test's entry point. The gate looked green and proved
nothing.

## Recipe, one fault per fresh vault

```bash
T=$(mktemp -d /tmp/<check>-test.XXXXXX) && cd "$T" && git init -q .
mkdir -p wiki/sources
# seed one page carrying the fault (off-vocabulary tag, dead link, dangling relation)
printf -- '---\ntype: source\ntitle: "Off"\ntags: [source, securty-typo]\n---\n# Off\n' \
  > wiki/sources/off.md
printf 'tags:\n  allowed:\n    - "source"\n' > kb-config.yaml
python3 <repo>/wiki-lint/scripts/<check>.py; echo "exit=$?"     # expect 1
# remove the fault (or the config) and rerun                        # expect 0
python3 <repo>/wiki-lint/scripts/run-lint.py --no-report          # aggregator sees it
cd / && rm -rf "$T"
```

## Gotchas, each of which cost a debugging round

- **`git init` is mandatory** in the temp vault. Root discovery requires `wiki/`
  **and** `.git`, walking cwd first then `__file__`. Without `.git` you get
  "vault root not found", which reads like a script bug and is not one.
- **Quote YAML scalars.** A bare `true` or a number parses as a non-string and
  the gate rejects the config as invalid — correct behaviour, confusing symptom.
- **One fault per vault.** Fixtures contaminate each other; a second fault can
  make the first untestable.
- **The aggregator runs sibling scripts with `cwd=vault_root`.** A missing
  sibling is reported as `error` and counts as failure — never a silent skip.
- **Piped output:** gate scripts must survive `| head` (catch `BrokenPipeError`,
  exit 0). A traceback under a pipe looks like a crash and hides the real exit
  code.
- **Scan scope differs per check.** `lint-dead-links.py` and
  `lint-tag-taxonomy.py` include `wiki/.reciclaje/`; `lint-relations.py` and
  `lint-tag-taxonomy.py` exempt `hot/index/log.md` and `wiki/meta/`. Faults
  seeded outside a check's scope prove nothing.
- **Run against a real vault before committing.** Read-only, or with
  `cp kb-config.yaml /tmp/bak` first and restore after. A check that passes on a
  synthetic fixture and explodes on 300 real pages is not done.

## Evidence worth reporting

Actual stdout and exit code per case: fault → 1, clean → 0, unconfigured →
SKIP 0, invalid config → 1, aggregator → 1 then 0. Never report a PASS you did
not observe, and never report a fail you produced by breaking the fixture
instead of the gate.
