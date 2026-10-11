#!/usr/bin/env python3
"""Send documentation files to the Asyntai knowledge base.

Every file becomes one knowledge base entry. The title is derived from the
path, so the same file always maps to the same entry: on the next run the old
entry is removed first and the new text takes its place. That keeps a pipeline
that runs on every commit from filling the knowledge base with duplicates.

A file whose text already matches the stored entry is left alone. A pipeline
that runs on every commit therefore costs nothing against the daily upload
limit on the days when the documentation did not change.
"""

import argparse
import fnmatch
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_BASE_URL = "https://asyntai.com"
DEFAULT_PATTERNS = "*.md,*.mdx,*.txt"
MIN_CONTENT_CHARS = 10          # the API rejects anything shorter
LIST_LIMIT = 100                # the API returns at most 100 entries


class ApiError(Exception):
    pass


def request_json(method, url, api_key, body=None, timeout=60):
    data = None
    headers = {
        "Authorization": "Bearer " + api_key,
        "Accept": "application/json",
        "User-Agent": "asyntai-buildkite-plugin",
    }
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        message = raw
        try:
            message = json.loads(raw).get("error", raw)
        except ValueError:
            pass
        raise ApiError("HTTP %s from %s: %s" % (exc.code, url, message))
    except urllib.error.URLError as exc:
        raise ApiError("Cannot reach %s: %s" % (url, exc.reason))

    try:
        return json.loads(raw)
    except ValueError:
        raise ApiError("The API answered with text that is not JSON: %s" % raw[:200])


def collect_files(root, patterns, exclude):
    """Return the files under root that match one of the patterns."""
    found = []
    if os.path.isfile(root):
        return [root]
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            relative = os.path.relpath(full, root).replace(os.sep, "/")
            if any(fnmatch.fnmatch(relative, pat) or fnmatch.fnmatch(name, pat)
                   for pat in exclude):
                continue
            if any(fnmatch.fnmatch(name, pat) for pat in patterns):
                found.append(full)
    return sorted(found)


def make_title(path, root, prefix):
    if os.path.isfile(root):
        relative = os.path.basename(root)
    else:
        relative = os.path.relpath(path, root).replace(os.sep, "/")
    return (prefix + relative) if prefix else relative


def read_text(path):
    with open(path, "rb") as handle:
        raw = handle.read()
    return raw.decode("utf-8", "replace").strip()


def existing_entries(base_url, api_key, website_id):
    query = {"limit": str(LIST_LIMIT)}
    if website_id:
        query["website_id"] = website_id
    url = base_url + "/api/v1/knowledge/?" + urllib.parse.urlencode(query)
    payload = request_json("GET", url, api_key)
    entries = payload.get("entries") or []
    if len(entries) >= LIST_LIMIT:
        print("  Warning: the knowledge base holds %d or more entries. The API "
              "lists %d at a time, so an older entry with the same title may "
              "not be replaced." % (LIST_LIMIT, LIST_LIMIT))
    by_title = {}
    for entry in entries:
        by_title.setdefault(entry.get("title"), []).append(entry)
    return by_title


def stored_content(base_url, api_key, entry_id):
    """Return the text held by an entry, or None when it cannot be read."""
    payload = request_json(
        "GET", "%s/api/v1/knowledge/%s/" % (base_url, entry_id), api_key)
    if not payload.get("content_available"):
        return None
    content = payload.get("content")
    return content.strip() if isinstance(content, str) else None


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Send documentation files to the Asyntai knowledge base.")
    parser.add_argument("--path", required=True,
                        help="File or folder to read.")
    parser.add_argument("--patterns", default=DEFAULT_PATTERNS,
                        help="Comma separated file patterns.")
    parser.add_argument("--exclude", default="",
                        help="Comma separated patterns to skip.")
    parser.add_argument("--title-prefix", default="",
                        help="Text put in front of every title.")
    parser.add_argument("--website-id", default="",
                        help="Website to write to. Empty means the first one.")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--prune", action="store_true",
                        help="Remove entries carrying the title prefix whose "
                             "file is gone. Needs --title-prefix.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show the plan. Send nothing.")
    args = parser.parse_args(argv)

    api_key = os.environ.get("ASYNTAI_API_KEY", "").strip()
    if not api_key and not args.dry_run:
        print("Error: the environment variable ASYNTAI_API_KEY is empty.",
              file=sys.stderr)
        print("Set ASYNTAI_API_KEY in the agent environment hook, or pass "
              "api-key-secret to read it from Buildkite secrets.", file=sys.stderr)
        return 1

    root = args.path
    if not os.path.exists(root):
        print("Error: the path %s does not exist." % root, file=sys.stderr)
        return 1

    patterns = [p.strip() for p in args.patterns.split(",") if p.strip()]
    exclude = [p.strip() for p in args.exclude.split(",") if p.strip()]
    files = collect_files(root, patterns, exclude)

    if not files:
        print("Error: no file under %s matches %s." % (root, args.patterns),
              file=sys.stderr)
        return 1

    base_url = args.base_url.rstrip("/")
    print("Asyntai knowledge sync")
    print("  Source:  %s" % root)
    print("  Files:   %d" % len(files))
    print("  Target:  %s" % base_url)
    print("")

    if args.dry_run:
        for path in files:
            print("  would send %s as \"%s\"" %
                  (path, make_title(path, root, args.title_prefix)))
        print("")
        print("Dry run. Nothing was sent.")
        return 0

    try:
        by_title = existing_entries(base_url, api_key, args.website_id)
    except ApiError as exc:
        print("Error: %s" % exc, file=sys.stderr)
        return 1

    added = 0
    updated = 0
    unchanged = 0
    skipped = 0
    deleted = 0
    handled_titles = set()

    for path in files:
        title = make_title(path, root, args.title_prefix)
        content = read_text(path)
        handled_titles.add(title)

        if len(content) < MIN_CONTENT_CHARS:
            print("  skip      %s (under %d characters)" %
                  (title, MIN_CONTENT_CHARS))
            skipped += 1
            continue

        old_entries = by_title.get(title, [])

        try:
            # One stored entry holding the same text means there is nothing to
            # do. Sending it again would cost the account part of its daily
            # upload limit and would change nothing.
            if len(old_entries) == 1:
                stored = stored_content(base_url, api_key, old_entries[0]["id"])
                if stored is not None and stored == content:
                    unchanged += 1
                    print("  unchanged %s" % title)
                    continue

            for old in old_entries:
                request_json("DELETE",
                             "%s/api/v1/knowledge/%s/" % (base_url, old["id"]),
                             api_key)

            body = {"title": title, "content": content}
            if args.website_id:
                body["website_id"] = args.website_id
            result = request_json("POST", base_url + "/api/v1/knowledge/text/",
                                  api_key, body)
        except ApiError as exc:
            print("  failed    %s" % title)
            print("Error: %s" % exc, file=sys.stderr)
            return 1

        if old_entries:
            updated += 1
            word = "updated  "
        else:
            added += 1
            word = "added    "
        print("  %s %s (%d characters, %s chunks)" %
              (word, title, len(content), result.get("chunks_created", "?")))

    if args.prune and args.title_prefix:
        # Only entries carrying this prefix are touched, so one pipeline can
        # never remove what another pipeline owns.
        for title, entries in sorted(by_title.items()):
            if not title or not title.startswith(args.title_prefix):
                continue
            if title in handled_titles:
                continue
            for entry in entries:
                try:
                    request_json(
                        "DELETE",
                        "%s/api/v1/knowledge/%s/" % (base_url, entry["id"]),
                        api_key)
                except ApiError as exc:
                    print("Error: %s" % exc, file=sys.stderr)
                    return 1
                deleted += 1
                print("  deleted   %s (the file is gone)" % title)
    elif args.prune:
        print("  Warning: prune was ignored. Set a title prefix as well, so "
              "the step only removes entries it owns.")

    print("")
    print("Done. %d added, %d updated, %d unchanged, %d deleted, %d skipped."
          % (added, updated, unchanged, deleted, skipped))
    return 0


if __name__ == "__main__":
    sys.exit(main())
