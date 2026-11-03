# Profile Analytics Setup

The profile assets refresh daily at **02:17 UTC** through `.github/workflows/refresh-profile-analytics.yml`. The workflow can also be started manually from **Actions → Refresh profile analytics → Run workflow**.

## What works without a personal token

The workflow always uses its built-in `GITHUB_TOKEN` to update only the generated files in `assets/`. It can publish public contribution-calendar data, but it is a GitHub App installation token—not proof of access to the profile owner's private contribution data.

## Exact private-activity setup checklist

Use this only when the combined public-and-eligible-private view is required.

1. Sign in as **salahsaeed19** and open **Settings → Developer settings → Personal access tokens → Tokens (classic)**.
2. Create a token with a short expiration and select **only** the `read:user` scope. Do not select `repo`, `workflow`, or any organization scope.
3. Open the profile repository’s **Settings → Secrets and variables → Actions** page.
4. Create a repository secret named `GH_CONTRIBUTIONS_TOKEN`; paste the token as its value.
5. In **Settings → Actions → General**, ensure **Workflow permissions** allow read/write repository contents. The workflow requests only `contents: write`, which is needed solely to update SVG assets.
6. Open **Actions → Refresh profile analytics → Run workflow** and run it on `main`.
7. Inspect the generated **Live Development Activity** card. It should say **Public + eligible private activity** only after all verification checks pass.

The script requires all three conditions before it uses that combined label:

- the supplied token is the personal-token secret rather than the workflow token;
- GitHub reports `viewer.login = salahsaeed19`;
- GitHub’s `X-OAuth-Scopes` response header includes `read:user`.

If any condition is absent—or GitHub does not return an OAuth-scope header—the published label is **Private inclusion unverified**. The script never calculates or displays an unverified private-only number.

## Manual workflow test

After the manual run completes:

1. Open the workflow log and confirm the generator reports an exact 365-day update, with no GraphQL or calendar-validation failure.
2. Confirm that only files under `assets/` were committed by `github-actions[bot]`, and only if their content changed.
3. Open the profile README and verify that the activity card and heatmap render, the trend shows every calendar month touched by the 365-day window, and the displayed date range matches the card.
4. If using `GH_CONTRIBUTIONS_TOKEN`, verify the combined scope label. If it remains public-only, replace the secret with an owner-created classic token carrying `read:user`.
5. Revoke and replace the token immediately if it is ever exposed outside GitHub Secrets.

## Data and failure behavior

- The range is the exact 365 calendar days from 364 days before today through today. Query bounds are midnight-to-midnight in `Asia/Hebron`, with an exclusive next-day end.
- “Today” and “Last 30 days” are contribution counts, not commit counts.
- The generator requires every date in the requested calendar window and verifies that the day sum equals GitHub’s `totalContributions`; it does not drop partial months or missing days.
- The heatmap leaves dates outside the exact window visually blank, even though GitHub returns whole calendar weeks.
- An API, schema, calendar, or validation failure exits non-zero before writing assets, preserving the last known-good visuals.

## Local verification

```powershell
py -3 scripts/generate_contributions.py --placeholder
py -3 -m unittest discover -s tests -v
```

For a live local refresh, set `GH_CONTRIBUTIONS_TOKEN` only for the active shell. Never save a token in a tracked file.
