# Dropbox filing setup

One-time checklist for the Dropbox app that files each employee submission. Do this in the **chris.cox@coxresearch.com** Dropbox account. Do not put the app secret or refresh token in git or in chat.

The app uploads with `files/upload` (`mode` add, `autorename` true). It never overwrites, never deletes, and never creates a shared link. `files/upload` creates the closing-date folder and the employee folder when they are missing.

## 1. Create the scoped app

1. Sign in to Dropbox as **chris.cox@coxresearch.com**.
2. Open the [Dropbox App Console](https://www.dropbox.com/developers/apps) and create an app.
3. Choose **Scoped access**.
4. Choose **Full Dropbox** so the app can write under `/Chris's AI/`.
5. Name the app (for example `Cox Amex filing`).

## 2. Enable only these scopes

On the app **Permissions** tab, enable:

- `files.content.write` — upload the PDF, spreadsheet, and combined chart
- `files.content.read` — read file content for this app
- `files.metadata.read` — see folders and file names

Do not enable `sharing.write`, `files.metadata.write`, or `files.permanent_delete`. Those would allow shared links or deletes, and this app does not use them.

Submit the permission change.

## 3. Generate a refresh token

1. On the app **Settings** tab, copy the **App key** and **App secret**.
2. Under **OAuth 2**, set a redirect URI you control (for example `http://localhost:53682/`) and add it to the app.
3. Open this URL in a browser while signed in as chris.cox@coxresearch.com, replacing `APP_KEY`:

   `https://www.dropbox.com/oauth2/authorize?client_id=APP_KEY&response_type=code&token_access_type=offline`

4. Approve the app and copy the `code` from the redirect.
5. Exchange that code once:

   `curl -X POST https://api.dropboxapi.com/oauth2/token -d code=CODE -d grant_type=authorization_code -d client_id=APP_KEY -d client_secret=APP_SECRET -d redirect_uri=http://localhost:53682/`

6. Save the `refresh_token` from the response. The app exchanges it for a short-lived access token on each filing. It does not store the access token.

## 4. Set the three values on Render

On the **amex-submission** service, Environment tab, add these and nothing else for Dropbox:

- `DROPBOX_APP_KEY` — the app key
- `DROPBOX_APP_SECRET` — the app secret
- `DROPBOX_REFRESH_TOKEN` — the refresh token from step 3

Leave them out of the repo. If any of the three is missing, or an upload fails, submit still succeeds, the failure is logged, and the record email includes the line `Dropbox filing failed`.

## Where files go

- Employee PDF and spreadsheet: `/Chris's AI/American Express charges/<Month D>/<Employee Name>/`
  Example folder: `October 22`.
  Example file: `Ada - Amex October 2026 - submitted 2026-10-21 0930 CT.pdf`
- Combined chart for that statement: `/Chris's AI/American Express charges/Combined chart - Amex October 2026.pdf` and `.xlsx` (top of `American Express charges`, not inside the closing-date folder).
