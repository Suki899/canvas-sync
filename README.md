# Canvas Sync

Mirror your Canvas course files into any normal folder. Enter your Canvas URL and access token once; Canvas Sync downloads the files and checks for changes every five minutes.

Requires Python 3.10+ on Linux or macOS.

## Start

From this repository:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/canvas-sync ~/Canvas
```

Choose any destination, including a path with spaces:

```sh
.venv/bin/canvas-sync "/path/to/My Canvas Files"
```

The first run asks for:

1. Your Canvas website URL, such as `https://canvas.example.edu` or `https://yourschool.instructure.com`.
2. An access token from **Canvas → Account → Settings → New Access Token**. Input is hidden. Use a Canvas API token, not your school password. Your institution must permit personal access tokens; if the **New Access Token** button is missing, it does not, and Canvas Sync cannot be used there.

Leave the command running to keep syncing. Press Ctrl+C to stop. Files remain available offline.

## Options

```sh
canvas-sync ~/Canvas --once          # one pass, then exit
canvas-sync ~/Canvas --interval 60   # check every minute
canvas-sync ~/Canvas --login         # replace an expired token
```

These examples assume your virtual environment is activated (`source .venv/bin/activate`); otherwise use `.venv/bin/canvas-sync`.

For unattended setup, supply `CANVAS_URL` and `CANVAS_TOKEN` as environment variables. Avoid putting tokens in shell history. Environment tokens are not saved unless `--login` is used. Tokens are stored separately from downloaded files under `$XDG_CONFIG_HOME/canvas-sync/accounts/` (default `~/.config/canvas-sync/accounts/`) in files readable only by your user (`0600`). They are stored as plaintext, not in an OS keychain. One saved token is used per Canvas site.

## Mirror behavior

- Downloads accessible files from the Files area of every course returned by Canvas, preserving its folder hierarchy. It does not filter by semester.
- Course folders use `Course code [Canvas ID]` so identically named courses stay separate. Conflicting or filesystem-incompatible names are made unique and safe.
- Adds new files, updates changed files, follows remote renames, and removes remote-deleted files only when the local copy is unmodified and the course scan succeeded.
- Preserves local edits and pre-existing files, reporting them as protected. Move an edited copy outside the mirror, or remove it, to let the next sync restore the Canvas version.
- Keeps files from courses that disappear or become inaccessible. Locked files are skipped. Losing access is not treated as proof of deletion.
- Uses temporary downloads, size checks, atomic replacement, content hashes, network timeouts, retries for transient HTTP errors, and a per-folder process lock.
- Keeps tracking data in `.canvas-sync/` inside each destination. Keep that directory with the files when moving the mirror. Each destination belongs to one Canvas account.
- Never uploads local edits. Pages, announcements, quizzes, submissions, personal/group storage, and external links are outside its scope. Empty Canvas folders are not materialized until they contain files.

`--once` returns `0` on success, `1` for errors or protected-file conflicts, and `2` for invalid arguments. Continuous mode retries failed sync passes at the configured interval. An expired token stops the process with an instruction to use `--login`.

Avoid placing the destination inside this source repository or changing its files through another synchronization tool at the same time.

## Development

```sh
python3 -m unittest discover -s tests -v
```

Tests use simulated Canvas responses and temporary folders; they do not need credentials. CI runs them on Linux and macOS with Python 3.10 and 3.14.

API references: [Canvas Files API](https://developerdocs.instructure.com/services/canvas/resources/files), [Canvas pagination](https://developerdocs.instructure.com/services/canvas/basics/file.pagination).

## License

MIT. See [LICENSE](LICENSE).
