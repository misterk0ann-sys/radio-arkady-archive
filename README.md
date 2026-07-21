# RADIO ARKADY Archive

Best-effort public archive of community posts containing:

- `#RADIO_ARKADY`
- `#ASK_ARKADY`
- `#NAI_POIOS_EINAI`
- `#ARKADIOS_ARKADYO`

## Files

- `collector.py` — collects public search results
- `archive.json` — stored post data
- `collection-report.json` — most recent collection report
- `.github/workflows/update-archive.yml` — manual and daily GitHub Action

## First collection

Open **Actions** → **Update RADIO ARKADY archive** → **Run workflow**.

Use `250` pages per hashtag for the first run. If the workflow succeeds and
the oldest available posts have not been reached, run it again with a larger
value, such as `500`.

## Important limitation

This uses a public third-party search source. It can preserve the public posts
that the source returns, but it cannot guarantee retrieval of deleted, private,
unavailable, or non-indexed posts.
