# Mock: dashboard-v1

Static mocks of the teams dashboard pages, phone first. This is a design proposal, not the running app.

- `index.html`: the teams page (`/`)
- `settings.html`: the settings page (`/settings`)

Open either file straight from disk in a browser. Each is self-contained: inline CSS, no JavaScript, no network requests. The data in them is made up, drawn from `deploy/team-kit/dashboard.fixture.json`.

## Version name

`dashboard-v1` is the mock's version name. The UX designer's mock rounds name each mock version (`v1`, `v2`, ...) so the owner can approve one specific version. A changed mock goes in a new directory (`dashboard-v2`) and says what changed. This is version 1 of the dashboard redesign.

## Status: waiting for owner approval

Nothing here is implemented. The running pages in `deploy/team-kit/` (`dashboard_teams.*`, `dashboard_settings.*`) are unchanged, and there is no `/mocks` route. Implementation starts only after the owner approves `dashboard-v1` by name; until then developers do not build from it.

## What it fixes

| Problem on the current pages | In this mock |
| --- | --- |
| The team page is about 9000px tall at phone width because every mail is shown in full. | Mails are one-line summaries (time, sender → recipient, first line). Tapping one expands it. An expanded body scrolls inside a capped box. The other sections are collapsible panels. |
| Tables are squeezed on a phone, so ids and times wrap. | Below 720px each table row is a stacked card with labelled fields. From 720px up they are real tables. Ids and times never wrap; on a phone a long id scrolls sideways inside its own field. |
| No way to get from `/` to `/settings`. | A **Settings** button sits at the top right of `/`. `/settings` has a **← Teams** button back. |

Also: the team tabs are a horizontally scrolling strip, a jump bar links to each section, tap targets are at least 2.5rem, and on a phone the settings page keeps Preview, Confirm and Cancel in a bar fixed to the bottom of the screen.

## Behaviour the mock only suggests

- The mock has no JavaScript, so the team strip, filters, "Load older", the settings form and the preview do not do anything. Expanding mails and panels works because it uses native `<details>`.
- The real page keeps the current rules: all text from the API is put in as text, never as markup or links; the preview and confirmation flow on `/settings` is unchanged.
- The Settings link and expandable mails need small script changes. The layout and the table-to-card switch are CSS only.

## Review notes

The mocks were written without rendering them in a browser, because none was available where they were made. Check them at about 360px and at about 1000px wide before approving.
