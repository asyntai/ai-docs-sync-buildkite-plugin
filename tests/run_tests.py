#!/usr/bin/env python3
"""Test the plugin the way the Buildkite agent runs it.

The agent turns the plugin configuration into BUILDKITE_PLUGIN_AI_DOCS_SYNC_*
environment variables and runs hooks/command with bash in the checkout. This
runner does the same, against a stand-in API (tests/fake_api.py), with a fake
buildkite-agent on PATH that records annotations and answers secret lookups.

Usage:
    python tests/run_tests.py
"""

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
HOOK = os.path.join(ROOT, "hooks", "command")
API_KEY = "test-key-2f8a1c"
P = "BUILDKITE_PLUGIN_AI_DOCS_SYNC_"

# On Windows the bare name "bash" can reach the WSL bash, which does not get
# the environment the test sets. Use Git Bash there.
BASH = shutil.which("bash") or "bash"
if os.name == "nt":
    _git_bash = "C:/Program Files/Git/usr/bin/bash.exe"
    if os.path.exists(_git_bash):
        BASH = _git_bash

PASSED = []
FAILED = []


def check(name, condition, detail=""):
    if condition:
        PASSED.append(name)
        print("  PASS  %s" % name)
    else:
        FAILED.append((name, detail))
        print("  FAIL  %s" % name)
        if detail:
            print("        %s" % detail)


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def make_docs(base):
    docs = os.path.join(base, "docs")
    os.makedirs(os.path.join(docs, "guides"))
    os.makedirs(os.path.join(docs, "drafts"))
    files = {
        "index.md": "# Welcome\n\nAsyntai answers questions on your website.",
        "pricing.md": "# Pricing\n\nThe Starter plan covers one website.",
        "guides/setup.md": "# Setup\n\nPaste the script tag before the body ends.",
        "drafts/wip.md": "# Draft\n\nThis page is not ready for readers yet.",
        "tiny.md": "short",
        "logo.png": "not text at all, and not a pattern match either",
        "notes.txt": "Plain text notes that are long enough to send.",
        "api.rst": "API reference written in reStructuredText format.",
    }
    for name, text in files.items():
        with open(os.path.join(docs, name), "w", encoding="utf-8") as handle:
            handle.write(text)
    return docs


def write_exe(path, text):
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    os.chmod(path, 0o755)


def make_shims(workdir):
    """python3 that is a real Python, and a fake buildkite-agent."""
    shim_dir = os.path.join(workdir, "shim")
    os.makedirs(shim_dir)
    write_exe(os.path.join(shim_dir, "python3"),
              '#!/bin/sh\nexec "%s" "$@"\n' % sys.executable.replace(os.sep, "/"))
    agent_log = os.path.join(workdir, "agent-calls.log").replace(os.sep, "/")
    write_exe(os.path.join(shim_dir, "buildkite-agent"), """#!/usr/bin/env bash
# Fake buildkite-agent: records each call, answers "secret get".
LOG="%s"
if [[ "$1" == "annotate" ]]; then
  body="$(cat)"
  printf 'annotate|%%s|%%s\\n' "$*" "$body" >> "$LOG"
  exit 0
fi
if [[ "$1" == "secret" && "$2" == "get" ]]; then
  printf 'secret|%%s\\n' "$3" >> "$LOG"
  if [[ "$3" == "ASYNTAI_KEY_SECRET" ]]; then printf '%%s' "%s"; exit 0; fi
  echo "secret not found" >&2
  exit 1
fi
printf 'other|%%s\\n' "$*" >> "$LOG"
""" % (agent_log, API_KEY))
    return shim_dir, agent_log


def run_hook(env_extra, cwd, base_env):
    env = dict(os.environ)
    # Drop any plugin variable that leaked in from an earlier case.
    for key in list(env):
        if key.startswith(P):
            del env[key]
    env.update(base_env)
    env.update(env_extra)
    return subprocess.run([BASH, HOOK.replace(os.sep, "/")], cwd=cwd, env=env,
                          capture_output=True, text=True)


def server_dump(dump_path):
    flag = dump_path + ".flush"
    open(flag, "w").close()
    for _ in range(100):
        if not os.path.exists(flag):
            break
        time.sleep(0.05)
    with open(dump_path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def titles(dump):
    return sorted(entry["title"] for entry in dump["entries"])


def read_log(path):
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as handle:
        return [line.rstrip("\n") for line in handle if line.strip()]


def check_plugin_yml():
    print("\nplugin.yml")
    with open(os.path.join(ROOT, "plugin.yml"), "r", encoding="utf-8") as h:
        spec = yaml.safe_load(h)
    for field in ("name", "description", "author", "requirements",
                  "configuration"):
        check("plugin.yml has %s" % field, field in spec)
    conf = spec.get("configuration", {})
    props = conf.get("properties", {})
    check("path is the one required option", conf.get("required") == ["path"])
    check("unknown options are refused (additionalProperties: false)",
          conf.get("additionalProperties") is False)

    with open(HOOK, "r", encoding="utf-8") as h:
        hook = h.read()
    # Every option the hook reads must be declared, and the other way round.
    used = set()
    import re
    for m in re.finditer(r'(?:list_)?option ([A-Z_]+)', hook):
        used.add(m.group(1).lower().replace("_", "-"))
    declared = set(props)
    check("every option the hook reads is declared in plugin.yml",
          used <= declared, "undeclared: %s" % sorted(used - declared))
    check("every declared option is read by the hook",
          declared <= used, "unused: %s" % sorted(declared - used))
    known = re.search(r'KNOWN="([^"]+)"', hook)
    known = set(k.lower().replace("_", "-") for k in known.group(1).split()) \
        if known else set()
    check("the hook's list of known options matches plugin.yml",
          known == declared, "hook %s vs yml %s" % (sorted(known), sorted(declared)))

    try:
        import jsonschema
    except ImportError:
        check("jsonschema is installed for the schema checks", False,
              "pip install jsonschema")
        return
    jsonschema.Draft7Validator.check_schema(conf)
    v = jsonschema.Draft7Validator(conf)
    good = [
        {"path": "docs"},
        {"path": "docs", "patterns": ["*.md", "*.rst"], "exclude": "drafts/*",
         "title-prefix": "Docs / ", "prune": True, "dry-run": False,
         "api-key-secret": "ASYNTAI", "annotate": False},
    ]
    bad = [{}, {"path": "docs", "colour": "red"}, {"path": "docs", "prune": "yes"}]
    check("the schema accepts the README examples",
          all(v.is_valid(c) for c in good))
    check("the schema refuses a missing path, an unknown option, a wrong type",
          not any(v.is_valid(c) for c in bad))


def check_files():
    print("\nRepository files")
    for name in ("README.md", "LICENSE", "plugin.yml", "hooks/command",
                 "lib/sync.py"):
        check("%s exists" % name, os.path.exists(os.path.join(ROOT, name)))
    with open(HOOK, "rb") as h:
        raw = h.read()
    check("hooks/command has Unix line endings", b"\r\n" not in raw)
    check("hooks/command starts with a bash shebang",
          raw.startswith(b"#!/usr/bin/env bash"))
    syntax = subprocess.run([BASH, "-n", HOOK.replace(os.sep, "/")],
                            capture_output=True, text=True)
    check("bash accepts hooks/command", syntax.returncode == 0,
          syntax.stderr.strip())


def main():
    check_plugin_yml()
    check_files()

    workdir = tempfile.mkdtemp(prefix="asyntai-bk-test-")
    shim_dir, agent_log = make_shims(workdir)
    docs = make_docs(workdir)
    port = free_port()
    dump_path = os.path.join(workdir, "server.json")
    server = subprocess.Popen(
        [sys.executable, os.path.join(HERE, "fake_api.py"), str(port), dump_path],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(100):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.05)
    else:
        print("The stand-in API did not start.", file=sys.stderr)
        server.kill()
        return 2

    base = {
        "PATH": shim_dir + os.pathsep + os.environ.get("PATH", ""),
        "ASYNTAI_API_KEY": API_KEY,
        P + "PATH": "docs",
        P + "BASE_URL": "http://127.0.0.1:%d" % port,
    }
    expected = ["drafts/wip.md", "guides/setup.md", "index.md", "notes.txt",
                "pricing.md"]

    def run(**extra):
        return run_hook(extra, workdir, base)

    try:
        print("\nFirst run, an empty knowledge base")
        proc = run()
        check("the hook succeeds", proc.returncode == 0,
              proc.stdout[-600:] + proc.stderr[-600:])
        check("the log opens a Buildkite section",
              proc.stdout.startswith("--- :books:"), proc.stdout[:80])
        dump = server_dump(dump_path)
        check("every matching file became an entry", titles(dump) == expected,
              str(titles(dump)))
        check("the file under ten characters was skipped",
              "tiny.md" not in titles(dump))
        check("a file of another type was skipped", "logo.png" not in titles(dump))
        check("the output counts them as added", "5 added" in proc.stdout,
              proc.stdout[-300:])
        notes = [l for l in read_log(agent_log) if l.startswith("annotate|")]
        check("the build got one success annotation",
              len(notes) == 1 and "--style success" in notes[0], str(notes))
        check("the annotation carries the summary line",
              notes and "5 added" in notes[0], str(notes))
        check("the annotation uses its own context",
              notes and "--context asyntai-ai-docs-sync" in notes[0])

        print("\nSecond run, nothing changed")
        before = server_dump(dump_path)
        posts = sum(1 for c in before["calls"] if c["method"] == "POST")
        ids = {e["title"]: e["id"] for e in before["entries"]}
        proc = run()
        dump = server_dump(dump_path)
        check("the hook succeeds again", proc.returncode == 0, proc.stderr[-400:])
        check("nothing was duplicated", len(dump["entries"]) == len(expected))
        check("every entry kept its id",
              ids == {e["title"]: e["id"] for e in dump["entries"]})
        check("no text was sent again",
              sum(1 for c in dump["calls"] if c["method"] == "POST") == posts)
        check("the output counts them as unchanged",
              "5 unchanged" in proc.stdout, proc.stdout[-300:])

        print("\nChanged text")
        with open(os.path.join(docs, "pricing.md"), "w", encoding="utf-8") as h:
            h.write("# Pricing\n\nThe Standard plan covers three websites.")
        proc = run()
        dump = server_dump(dump_path)
        pricing = [e for e in dump["entries"] if e["title"] == "pricing.md"]
        check("the entry holds the new text",
              len(pricing) == 1 and "Standard plan" in pricing[0]["content"])
        check("only the changed file was sent",
              "0 added, 1 updated, 4 unchanged" in proc.stdout,
              proc.stdout[-300:])

        print("\nPatterns and exclude as lists")
        proc = run(**{P + "PATTERNS_0": "*.md", P + "PATTERNS_1": "*.rst",
                      P + "EXCLUDE_0": "drafts/*", P + "EXCLUDE_1": "index.md",
                      P + "TITLE_PREFIX": "List / "})
        check("the hook succeeds", proc.returncode == 0, proc.stderr[-400:])
        dump = server_dump(dump_path)
        listed = [t for t in titles(dump) if t.startswith("List / ")]
        check("both list patterns were used and both excludes applied",
              listed == ["List / api.rst", "List / guides/setup.md",
                         "List / pricing.md"], str(listed))

        print("\nPatterns and exclude as strings")
        proc = run(**{P + "PATTERNS": "*.txt", P + "EXCLUDE": "",
                      P + "TITLE_PREFIX": "Str / "})
        dump = server_dump(dump_path)
        check("a comma string works too",
              [t for t in titles(dump) if t.startswith("Str / ")] ==
              ["Str / notes.txt"], str(titles(dump)))

        print("\nPrune")
        prune = {P + "TITLE_PREFIX": "Docs / ", P + "PRUNE": "true"}
        run(**prune)
        os.remove(os.path.join(docs, "notes.txt"))
        proc = run(**prune)
        dump = server_dump(dump_path)
        check("the hook succeeds", proc.returncode == 0, proc.stderr[-400:])
        check("the entry of the deleted file is gone",
              "Docs / notes.txt" not in titles(dump))
        check("entries without the prefix were not touched",
              "notes.txt" in titles(dump))
        check("the output counts the deletion", "1 deleted" in proc.stdout)

        print("\nDry run")
        calls = len(server_dump(dump_path)["calls"])
        proc = run(**{P + "DRY_RUN": "true", "ASYNTAI_API_KEY": ""})
        check("a dry run needs no key and succeeds", proc.returncode == 0,
              proc.stderr[-400:])
        check("the output says nothing was sent", "Nothing was sent" in proc.stdout)
        check("the API was not called",
              len(server_dump(dump_path)["calls"]) == calls)

        print("\nThe key from a Buildkite secret")
        proc = run(**{"ASYNTAI_API_KEY": "", P + "API_KEY_SECRET": "ASYNTAI_KEY_SECRET"})
        check("the hook reads the key with buildkite-agent secret get",
              proc.returncode == 0, proc.stdout[-300:] + proc.stderr[-300:])
        check("the agent was asked for that secret",
              "secret|ASYNTAI_KEY_SECRET" in read_log(agent_log))
        proc = run(**{"ASYNTAI_API_KEY": "", P + "API_KEY_SECRET": "MISSING"})
        check("a missing secret stops the step", proc.returncode != 0)
        check("the message names the secret", "MISSING" in proc.stderr,
              proc.stderr[-300:])
        check("the key never appears in the log",
              API_KEY not in proc.stdout + proc.stderr)

        print("\nThe key from a named variable")
        proc = run(**{"ASYNTAI_API_KEY": "", P + "API_KEY_ENV": "MY_DOCS_KEY",
                      "MY_DOCS_KEY": API_KEY})
        check("the hook reads the variable named by api-key-env",
              proc.returncode == 0, proc.stderr[-300:])

        print("\nAnnotate switched off")
        before = len(read_log(agent_log))
        run(**{P + "ANNOTATE": "false"})
        check("no annotation was added",
              len([l for l in read_log(agent_log)[before:]
                   if l.startswith("annotate|")]) == 0)

        print("\nFailures")
        proc = run(**{"ASYNTAI_API_KEY": ""})
        check("an empty key stops the step", proc.returncode != 0)
        check("the message names the variable", "ASYNTAI_API_KEY" in proc.stderr,
              proc.stderr[-300:])

        before = len(read_log(agent_log))
        proc = run(**{"ASYNTAI_API_KEY": "wrong-key"})
        check("a wrong key stops the step", proc.returncode != 0)
        check("the log repeats what the API said",
              "Invalid API key" in proc.stdout, proc.stdout[-300:])
        check("the error line comes after the sync header, not before it",
              "Asyntai knowledge sync" in proc.stdout and
              proc.stdout.index("Asyntai knowledge sync") <
              proc.stdout.index("Invalid API key"), proc.stdout[:300])
        notes = [l for l in read_log(agent_log)[before:] if l.startswith("annotate|")]
        check("a failed sync gets an error annotation",
              len(notes) == 1 and "--style error" in notes[0], str(notes))

        proc = run(**{P + "PATH": "no-such-folder"})
        check("a missing folder stops the step", proc.returncode != 0)
        check("the log names the folder", "no-such-folder" in proc.stdout)

        proc = run(**{P + "PATH": ""})
        check("an empty path stops the step", proc.returncode != 0)
        check("the message says a path is needed", "needs a path" in proc.stderr)

        proc = run(**{P + "BASE_URL": "http://127.0.0.1:1"})
        check("an unreachable server stops the step", proc.returncode != 0)
        check("the log says the server could not be reached",
              "Cannot reach" in proc.stdout)

        print("\nAn unknown option")
        calls = len(server_dump(dump_path)["calls"])
        proc = run(**{P + "TITLE__PREFIX": "Docs / ", P + "COLOUR": "red"})
        check("an unknown option stops the step", proc.returncode != 0)
        check("the message names each unknown option",
              "title--prefix" in proc.stderr and "colour" in proc.stderr,
              proc.stderr[-300:])
        check("nothing was sent", len(server_dump(dump_path)["calls"]) == calls)

        print("\nbuildkite-agent found through BUILDKITE_BIN_PATH")
        bin_dir = os.path.join(workdir, "agentbin")
        os.makedirs(bin_dir)
        shutil.move(os.path.join(shim_dir, "buildkite-agent"),
                    os.path.join(bin_dir, "buildkite-agent"))
        try:
            before = len(read_log(agent_log))
            proc = run(**{"BUILDKITE_BIN_PATH": bin_dir.replace(os.sep, "/")})
            notes = [l for l in read_log(agent_log)[before:]
                     if l.startswith("annotate|")]
            check("the hook succeeds", proc.returncode == 0, proc.stderr[-300:])
            check("the annotation still reached the agent", len(notes) == 1,
                  str(notes))
            proc = run()
            check("with no agent at all the sync still succeeds",
                  proc.returncode == 0, proc.stderr[-300:])
        finally:
            shutil.move(os.path.join(bin_dir, "buildkite-agent"),
                        os.path.join(shim_dir, "buildkite-agent"))

        print("\nA single file as the path")
        proc = run(**{P + "PATH": "docs/index.md", P + "TITLE_PREFIX": "One / "})
        check("the hook succeeds", proc.returncode == 0, proc.stderr[-300:])
        check("the title is the file name",
              "One / index.md" in titles(server_dump(dump_path)))

        print("\nRun from another folder")
        other = tempfile.mkdtemp(prefix="asyntai-bk-cwd-")
        try:
            shutil.copytree(docs, os.path.join(other, "docs"))
            proc = run_hook({}, other, base)
            check("the hook finds lib/sync.py from any checkout",
                  proc.returncode == 0, proc.stderr[-300:])
        finally:
            shutil.rmtree(other, ignore_errors=True)

    finally:
        open(dump_path + ".stop", "w").close()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
        shutil.rmtree(workdir, ignore_errors=True)

    print("\n%d passed, %d failed." % (len(PASSED), len(FAILED)))
    for name, detail in FAILED:
        print("  FAILED: %s" % name)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
