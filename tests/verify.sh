#!/bin/sh
# Referee entry point (make verify). Builds the artefact (an uncompressed source archive of HEAD, kept in dist/),
# unpacks it, runs the tests inside the unpacked copy, never the working tree, and writes verify-result.json: commit,
# dirty flag, artefact sha256, verdict. Pass needs a zero exit AND at least one test run (Python 3.9-3.11 exit 0 when
# discovery finds nothing). CI runs this on push; a local run is a pre-check only.
set -u
PY=${PYTHON:-python3}
commit=$(git rev-parse HEAD)
if [ -n "$(git status --porcelain)" ]; then dirty=true; else dirty=false; fi
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
mkdir -p dist && rm -f dist/conta-*.tar   # one artefact at a time locally
art="conta-$commit.tar"
git archive --format=tar --prefix=conta/ -o "dist/$art" HEAD
sha=$($PY -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' "dist/$art")
tar -xf "dist/$art" -C "$tmp"
if (cd "$tmp/conta" && "$PY" -I -m unittest discover -s tests -v) >"$tmp/log" 2>&1; then rc=0; else rc=1; fi
cat "$tmp/log"
ran=$(grep -Eo '^Ran [0-9]+' "$tmp/log" | grep -Eo '[0-9]+' || echo 0)
if [ "$rc" = 0 ] && [ "${ran:-0}" -gt 0 ]; then verdict=pass; else verdict=fail; fi
"$PY" - "$commit" "$dirty" "$art" "$sha" "$verdict" "$ran" <<'PYEOF'
import json, platform, sys
commit, dirty, art, sha, verdict, ran = sys.argv[1:]
json.dump({"commit": commit, "dirty": dirty == "true", "artefacts": [{"name": art, "sha256": sha}],
           "verdict": verdict, "tests_run": int(ran), "python": platform.python_version()},
          open("verify-result.json", "w"), indent=1)
print(f"verify-result.json: {verdict} ({ran} tests, python {platform.python_version()}, commit {commit[:7]}, dirty={dirty})")
PYEOF
[ "$verdict" = pass ]
