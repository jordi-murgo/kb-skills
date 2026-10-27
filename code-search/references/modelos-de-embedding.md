# Changing or comparing an embedding model

Read before swapping the model behind `wiki-search` or `code-search`. Both read
their model and endpoint from `kb-config.yaml` — `wiki_search` and `code_search`
respectively, falling back to the shared `embeddings` section — so no code change
is needed. What is needed is a measurement, because the failure mode is silent.

## 1. Pin the size tag, not `latest`

```bash
curl -s http://<host>:11434/api/embed \
  -d '{"model":"qwen3-embedding:0.6b","input":["x"]}' | python3 -c \
  'import json,sys; print(len(json.load(sys.stdin)["embeddings"][0]))'
```

`qwen3-embedding:latest` is the 8B model at 4096 dims, not the 0.6B at 1024. A
different dimension is a schema change, not an experiment: the index has to be
rebuilt and every stored vector is invalid. Check the dimension **before**
building.

## 2. Never trust the paper's benchmark alone

Model cards report MTEB on public corpora, not on your vault. Use the paper to
pick two or three candidates; use the A/B to pick the model.

The verified example matters because it nearly went the other way. Qwen3-
Embedding (arXiv 2506.05176, Table 9) reports MTEB(Code) nDCG@10 **76.74** for
`qwen3-embedding:0.6b` against **49.65** for `BGE-M3 (Dense)` at equal parameter
count — which predicts a win, and locally the model was *worse* until the task
instruction was added (§3). A benchmark that ignores your invocation is a
benchmark for a different invocation.

## 3. Instruction-aware models need the instruction on the query only

`Instruct: {one-sentence task description}\nQuery:{query}` — and documents are
embedded **unchanged**. Both scripts expose it as `query_prefix` (default `""`).

Measured on a 20-query code gold set (53 files, 118 chunks):

| arm | vector-mode MRR |
|---|---|
| bge-m3 | 0.497 |
| qwen3-embedding:0.6b, no instruction | **0.478** (worse than what it replaced) |
| qwen3-embedding:0.6b, with instruction | **0.771** |

Missing the prefix raises nothing: the model returns well-formed vectors, the
index validates, `status` reports healthy, and only relevance drops. This is the
single most expensive silent failure in the retrieval stack, which is why it is
configurable and tested rather than hardcoded.

Measured separately on the vulcano wiki gold set (35 queries, 327 pages,
`--mode vector`), the prefix is a smaller and **not significant** effect:

| arm | MRR | hit@5 | nDCG@5 |
|---|---|---|---|
| no prefix | 0.8619 | 0.943 | 0.850 |
| with prefix | 0.9143 | 0.971 | 0.894 |

+0.052 MRR, 4 wins / 1 loss / 30 ties — p=0.375 on 5 non-tied pairs, below the
0.121 MDE at n=35. Direction consistent, magnitude unproven on wiki. Set the
prefix anyway (it is free and never hurt), but do not quote it as a wiki win.

## 4. Isolate the A/B without touching the live index

`sandbox` both arms by copying the corpus. The root must contain `wiki/` **and**
be a git worktree, because candidate collection applies Git ignore rules:

```bash
T=/tmp/ab; rm -rf "$T"; mkdir -p "$T/base" "$T/new"
cp -R <vault>/projects "$T/base/projects"; cp -R <vault>/projects "$T/new/projects"
for d in base new; do
  mkdir -p "$T/$d/wiki"; git init -q "$T/$d"
  printf 'embeddings:\n  model: bge-m3\n  endpoint: <ollama-url>\n' > "$T/$d/kb-config.yaml"
done
# then add the `code_search:` / `wiki_search:` override to the arm under test
python3 <kb-skills>/code-search/scripts/code-search.py --root "$T/base" build
python3 <kb-skills>/code-search/scripts/code-search.py --root "$T/new"  build
```

Gotchas that cost real time:

- `--root` goes **before** the subcommand.
- A copied **submodule `.git` pointer** (e.g. `projects/x/.git` holding
  `gitdir: ../../.git/modules/...`) makes `git check-ignore` exit 128 and the
  build dies with "Cannot apply Git ignore rules". Delete the pointers before
  `git init`: `find "$T" -name .git -maxdepth 3 -delete`.
- Both arms must produce the **same dimensions**, or you are measuring a schema
  change.

## 5. Report the per-mode table, then the noise floor

Run `--mode bm25`, `--mode vector` and `--mode hybrid` separately; equal-weight
RRF can be worse than the better channel, and a single hybrid number hides it.

Then state what your n can detect. At n=20-40 queries, paired deltas below ~0.08
MRR are indistinguishable from noise; at n=35 the MDE is ≈0.121 RR. A +0.21 gain
at 10 wins / 3 losses / 7 ties is p≈0.09 — suggestive, not settled. Say which.

## 6. Verify

`python3 -m pytest . -q`, then a smoke query against the real vault: the obvious
query must return the obvious page at rank 1. A healthy-looking MRR with a wrong
top-1 means the index and the query path disagree — usually the model in the
config differs from the model that built the index, which `build` reports and
`status` repeats from the index metadata.
