"""
Read-only tools the AI reviewer can call while investigating a proposal:
Stride source code at any git ref (downloaded from GitHub and cached on disk),
diffs between refs, live chain queries, and GitHub release notes.

Every tool returns a string. Output is capped, so one call can't flood the model's context.
"""

import difflib
import json
import re
import shutil
import tarfile
import time
import urllib.request
from pathlib import Path

KEEP_EXTENSIONS = {".go", ".proto", ".md", ".sh", ".json", ".yml", ".yaml", ".toml", ".mod"}
MAX_SOURCE_FILE_BYTES = 512 * 1024
MAX_CACHED_REFS = 4
REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-/]{0,99}$")
QUERY_PREFIXES = ("/cosmos/", "/ibc/", "/Stride-Labs/", "/cosmwasm/")
QUERY_PATH_RE = re.compile(r"^/[A-Za-z0-9_\-./?=&%:,]*$")

MAX_OUTPUT_CHARS = 16_000
READ_MAX_LINES = 250
SEARCH_MAX_MATCHES = 40
SEARCH_TIME_LIMIT = 15  # seconds


# Every release bumps the Go module path (github.com/Stride-Labs/stride/v33 -> /v34) in nearly
# every file. Diffs ignore that bump so the real changes stand out.
MODULE_PATH_RE = re.compile(r"(github\.com/Stride-Labs/stride)/v\d+")


def normalized_lines(path):
    if not path.is_file():
        return []
    return MODULE_PATH_RE.sub(r"\1/vN", path.read_text(errors="replace")).splitlines()


def cap(s, n=MAX_OUTPUT_CHARS):
    return s if len(s) <= n else s[:n] + f"\n...[truncated, {len(s) - n} more chars]"


TOOL_SPECS = [
    {
        "name": "search_code",
        "description": "Regex search (Python syntax, matched per line) over Stride source files at a git ref. "
                       "Returns up to 40 'path:line: text' matches. Generated *.pb.go files are skipped "
                       "unless `path` points into one.",
        "parameters": {
            "type": "object",
            "properties": {
                "ref": {"type": "string", "description": "git tag/branch/commit, e.g. v33.0.0"},
                "pattern": {"type": "string"},
                "path": {"type": "string", "description": "optional directory or file prefix, e.g. x/stakeibc"},
            },
            "required": ["ref", "pattern"],
        },
    },
    {
        "name": "read_file",
        "description": "Read lines of a source file at a git ref (up to 250 lines per call, numbered).",
        "parameters": {
            "type": "object",
            "properties": {
                "ref": {"type": "string"},
                "path": {"type": "string"},
                "start_line": {"type": "integer"},
                "end_line": {"type": "integer"},
            },
            "required": ["ref", "path"],
        },
    },
    {
        "name": "list_files",
        "description": "List the files and subdirectories of a directory at a git ref.",
        "parameters": {
            "type": "object",
            "properties": {"ref": {"type": "string"}, "path": {"type": "string"}},
            "required": ["ref"],
        },
    },
    {
        "name": "diff_refs",
        "description": "Compare two git refs. With a directory path (or none): list the added, removed and "
                       "modified files under it. With a file path: a unified diff of that file. The routine "
                       "Go module path bump (stride/v33 -> stride/v34) is ignored.",
        "parameters": {
            "type": "object",
            "properties": {
                "base_ref": {"type": "string"},
                "head_ref": {"type": "string"},
                "path": {"type": "string"},
            },
            "required": ["base_ref", "head_ref"],
        },
    },
    {
        "name": "query_chain",
        "description": "GET a path on the live Stride REST API (read-only). Allowed prefixes: "
                       "/cosmos/, /ibc/, /Stride-Labs/, /cosmwasm/. "
                       "Example: /ibc/core/client/v1/client_states/07-tendermint-176",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "github_release",
        "description": "Fetch the notes for a Stride-Labs/stride GitHub release tag. With an empty tag, "
                       "list the most recent tags.",
        "parameters": {
            "type": "object",
            "properties": {"tag": {"type": "string"}},
            "required": ["tag"],
        },
    },
]


class Toolbox:
    def __init__(self, http, lcd_get, src_dir, repo="Stride-Labs/stride", github_token="", log=print):
        self.http = http
        self.lcd_get = lcd_get
        self.src_dir = Path(src_dir)
        self.repo = repo
        self.github_token = github_token
        self.log = log

    # ------------------------------------------------------------------ dispatch

    def call(self, name, arguments):
        try:
            args = json.loads(arguments or "{}") if isinstance(arguments, str) else dict(arguments)
        except ValueError:
            return "error: tool arguments were not valid JSON"
        fn = getattr(self, f"tool_{name}", None)
        if fn is None:
            return f"error: unknown tool {name!r}"
        try:
            return cap(fn(**args))
        except TypeError as e:
            return f"error: bad arguments for {name}: {e}"
        except Exception as e:  # noqa: BLE001 - errors go back to the model, not up the stack
            return f"error: {e}"

    # ------------------------------------------------------------------ source cache

    def _github_headers(self):
        h = {"Accept": "application/vnd.github+json"}
        if self.github_token:
            h["Authorization"] = f"Bearer {self.github_token}"
        return h

    def source_root(self, ref):
        """Directory holding the source at `ref`, downloading and extracting it on first use."""
        ref = (ref or "").strip()
        if not REF_RE.match(ref) or ".." in ref:
            raise ValueError(f"invalid git ref {ref!r}")
        root = self.src_dir / ref.replace("/", "__")
        if (root / ".complete").exists():
            (root / ".complete").touch()  # mark as recently used
            return root

        url = f"https://codeload.github.com/{self.repo}/tar.gz/{ref}"
        self.log(f"downloading source {self.repo}@{ref}")
        started = time.monotonic()
        shutil.rmtree(root, ignore_errors=True)
        root.mkdir(parents=True)
        headers = {"User-Agent": "stride-gov-bot"}
        if self.github_token:
            headers["Authorization"] = f"Bearer {self.github_token}"
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=120) as resp:
                # Stream-extract so the ~100MB tarball never sits on disk.
                with tarfile.open(fileobj=resp, mode="r|gz") as tar:
                    for m in tar:
                        if not m.isfile() or m.size > MAX_SOURCE_FILE_BYTES:
                            continue
                        rel = Path(*Path(m.name).parts[1:])  # strip the "stride-<ref>/" prefix
                        if not rel.parts or rel.is_absolute() or ".." in rel.parts:
                            continue
                        if rel.suffix not in KEEP_EXTENSIONS:
                            continue
                        dest = root / rel
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        dest.write_bytes(tar.extractfile(m).read())
        except Exception:
            shutil.rmtree(root, ignore_errors=True)
            raise
        (root / ".complete").touch()
        self.log(f"source {ref} ready in {time.monotonic() - started:.0f}s")
        self._prune_cache()
        return root

    def _prune_cache(self):
        refs = sorted((d for d in self.src_dir.iterdir() if (d / ".complete").exists()),
                      key=lambda d: (d / ".complete").stat().st_mtime, reverse=True)
        for old in refs[MAX_CACHED_REFS:]:
            shutil.rmtree(old, ignore_errors=True)

    def _resolve(self, ref, path):
        root = self.source_root(ref).resolve()
        target = (root / (path or "").strip("/")).resolve()
        if target != root and root not in target.parents:
            raise ValueError("path escapes the source tree")
        return root, target

    # ------------------------------------------------------------------ tools

    def tool_list_files(self, ref, path=""):
        root, target = self._resolve(ref, path)
        if not target.is_dir():
            return f"not a directory: {path}"
        entries = sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name))
        lines = [f"{p.name}/" if p.is_dir() else f"{p.name}  ({p.stat().st_size} bytes)"
                 for p in entries if p.name != ".complete"]
        return "\n".join(lines[:300]) or "(empty)"

    def tool_read_file(self, ref, path, start_line=1, end_line=None):
        root, target = self._resolve(ref, path)
        if not target.is_file():
            return f"file not found at {ref}: {path}"
        lines = target.read_text(errors="replace").splitlines()
        start = max(1, int(start_line or 1))
        end = min(len(lines), int(end_line) if end_line else start + READ_MAX_LINES - 1,
                  start + READ_MAX_LINES - 1)
        body = "\n".join(f"{i:5d}  {lines[i - 1]}" for i in range(start, end + 1))
        more = f"\n[file has {len(lines)} lines; showing {start}-{end}]" if end < len(lines) or start > 1 else ""
        return body + more

    def tool_search_code(self, ref, pattern, path=""):
        root, target = self._resolve(ref, path)
        try:
            rx = re.compile(pattern)
        except re.error as e:
            return f"invalid regex: {e}"
        files = [target] if target.is_file() else sorted(target.rglob("*"))
        deadline = time.monotonic() + SEARCH_TIME_LIMIT
        matches = []
        for f in files:
            if not f.is_file() or f.name == ".complete":
                continue
            if f.name.endswith((".pb.go", ".pb.gw.go")) and not target.is_file():
                continue
            if time.monotonic() > deadline:
                matches.append("[search stopped: time limit]")
                break
            for n, line in enumerate(f.read_text(errors="replace").splitlines(), 1):
                if rx.search(line):
                    matches.append(f"{f.relative_to(root)}:{n}: {line.strip()[:200]}")
                    if len(matches) >= SEARCH_MAX_MATCHES:
                        return "\n".join(matches) + "\n[more matches not shown; narrow the path or pattern]"
        return "\n".join(matches) or "no matches"

    def tool_diff_refs(self, base_ref, head_ref, path=""):
        base_root, base = self._resolve(base_ref, path)
        head_root, head = self._resolve(head_ref, path)
        if base.is_file() or head.is_file():
            a, b = normalized_lines(base), normalized_lines(head)
            diff = "\n".join(difflib.unified_diff(a, b, f"{base_ref}/{path}", f"{head_ref}/{path}", lineterm=""))
            return diff or "files are identical"

        def listing(root, d):
            if not d.is_dir():
                return {}
            return {str(f.relative_to(root)): f for f in d.rglob("*") if f.is_file() and f.name != ".complete"}

        old, new = listing(base_root, base), listing(head_root, head)
        out = []
        for rel in sorted(set(old) | set(new)):
            if rel not in old:
                out.append(f"A {rel} (+{len(new[rel].read_text(errors='replace').splitlines())})")
            elif rel not in new:
                out.append(f"D {rel}")
            elif old[rel].read_bytes() != new[rel].read_bytes():
                a, b = normalized_lines(old[rel]), normalized_lines(new[rel])
                if a == b:
                    continue
                d = list(difflib.unified_diff(a, b, lineterm="", n=0))
                plus = sum(1 for x in d if x.startswith("+") and not x.startswith("+++"))
                minus = sum(1 for x in d if x.startswith("-") and not x.startswith("---"))
                out.append(f"M {rel} (+{plus} -{minus})")
        if not out:
            return "no differences"
        return f"{len(out)} files changed:\n" + "\n".join(out[:250])

    def tool_query_chain(self, path):
        path = (path or "").strip()
        if not path.startswith(QUERY_PREFIXES) or not QUERY_PATH_RE.match(path) or ".." in path:
            return f"refused: path must start with one of {', '.join(QUERY_PREFIXES)}"
        return json.dumps(self.lcd_get(path), indent=1)

    def tool_github_release(self, tag=""):
        tag = (tag or "").strip()
        api = f"https://api.github.com/repos/{self.repo}"
        if not tag:
            tags = json.loads(self.http("GET", f"{api}/tags?per_page=30", headers=self._github_headers()))
            return "recent tags: " + ", ".join(t["name"] for t in tags)
        if not REF_RE.match(tag):
            return "invalid tag"
        try:
            r = json.loads(self.http("GET", f"{api}/releases/tags/{tag}", headers=self._github_headers()))
        except Exception as e:  # noqa: BLE001
            if "404" in str(e):
                return f"no GitHub release for tag {tag!r} (the tag itself may or may not exist)"
            raise
        assets = ", ".join(a["name"] for a in r.get("assets", [])) or "none"
        return (f"release: {r.get('name')}\npublished: {r.get('published_at')}\nprerelease: {r.get('prerelease')}\n"
                f"assets: {assets}\n\n{r.get('body') or ''}")
