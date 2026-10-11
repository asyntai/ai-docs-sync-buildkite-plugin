# AI Docs Sync Buildkite Plugin

Keep the [Asyntai](https://asyntai.com) AI chatbot on your website current with
the documentation in your repository. Add one step to a pipeline. Every page you
change is in the chatbot's knowledge base by the end of the build, so your
customers get the answer that matches the version you just shipped.

## Example

```yaml
steps:
  - label: ":books: Sync docs to Asyntai"
    branches: main
    plugins:
      - asyntai/ai-docs-sync#v1.0.0:
          path: docs
          title-prefix: "Docs / "
```

Read several file types, and skip some files:

```yaml
steps:
  - label: ":books: Sync docs to Asyntai"
    plugins:
      - asyntai/ai-docs-sync#v1.0.0:
          path: site/content
          patterns: ["*.md", "*.mdx", "*.rst"]
          exclude: ["drafts/*", "CHANGELOG.md"]
```

Give the plugin a step of its own. It runs as the step command.

## Set up

1. Sign in to Asyntai, open **Settings**, then **API**, and copy your API key.
2. Give the agent the key in one of two ways:
   - Set `ASYNTAI_API_KEY` in the agent `environment` hook, or
   - Store it as a Buildkite secret and name it with `api-key-secret`:

```yaml
      - asyntai/ai-docs-sync#v1.0.0:
          path: docs
          api-key-secret: ASYNTAI_API_KEY
```

The API needs an Asyntai plan of Starter or higher. The agent needs `bash` and
Python 3.

## Configuration

### `path` (required, string)

The file or folder to read, relative to the checkout.

### `patterns` (optional, string or list)

File patterns to include. Default: `*.md,*.mdx,*.txt`.

### `exclude` (optional, string or list)

Patterns to skip, for example `drafts/*` or `CHANGELOG.md`.

### `title-prefix` (optional, string)

Text put in front of every entry title, for example `Docs / `.

### `website-id` (optional, string)

The Asyntai website to write to. Leave it out for your first website.

### `api-key-env` (optional, string)

The environment variable that holds your API key. Default: `ASYNTAI_API_KEY`.

### `api-key-secret` (optional, string)

The name of a Buildkite secret that holds your API key. When set, the plugin
reads the key with `buildkite-agent secret get`.

### `prune` (optional, boolean)

Remove the entries that carry your `title-prefix` and whose file is gone from
the repository. Default: `false`.

### `dry-run` (optional, boolean)

List the files and send nothing. Default: `false`.

### `annotate` (optional, boolean)

Add a summary of the run to the build page. Default: `true`.

### `base-url` (optional, string)

The Asyntai address. Change it only for a private install.

## How the entries are named

Each file becomes one knowledge base entry. The title is the path of the file
inside `path`, with `title-prefix` in front. A file keeps the same title on
every build, so the plugin replaces the old entry with the new one. The
pipeline can run on every commit and still hold one entry per file.

A file whose text already matches the stored entry is left alone, so a build on
a day when the documentation did not change costs nothing against your daily
upload limit. Every run ends with a line like this, which also goes on the
build page:

```
Done. 2 added, 1 updated, 14 unchanged, 0 deleted, 0 skipped.
```

## Several repositories, one chatbot

Give each repository its own `title-prefix`, for example `product-docs/` and
`handbook/`, and turn `prune` on. Each pipeline then works only on the entries
that carry its own prefix.

## Developing

```bash
python tests/run_tests.py
```

The tests run `hooks/command` the way the agent runs it, with the plugin
options as `BUILDKITE_PLUGIN_AI_DOCS_SYNC_*` variables, against a test API.

Lint the plugin with the Buildkite linter:

```bash
docker run -it --rm -v "$PWD:/plugin:ro" buildkite/plugin-linter --id asyntai/ai-docs-sync
```

## More

Full guide: https://asyntai.com/documentation/integrations/buildkite/

## License

MIT. See `LICENSE`.
