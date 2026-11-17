# Setup checklist

## 1. Create the repo

```bash
cd starlit-stories
git init -b main
git add .
git commit -m "3D kids video pipeline"
gh repo create starlit-stories --private --source=. --push
```

No `gh` CLI? Create an empty repo on github.com, then:

```bash
git remote add origin https://github.com/<you>/starlit-stories.git
git push -u origin main
```

## 2. Add secrets

Repo → Settings → Secrets and variables → Actions → **New repository secret**:

| Name | Where to get it |
|---|---|
| `YT_CLIENT_ID` | Google Cloud → APIs & Services → Credentials → OAuth 2.0 Client ID |
| `YT_CLIENT_SECRET` | same page |
| `YT_REFRESH_TOKEN` | from your OAuth flow — see below |

`GEMINI_API_KEY` is **not needed** any more. The pipeline has no image API and the script
writer runs locally, so there is nothing to pay for or rate-limit.

### Getting the refresh token

Use the OAuth 2.0 Playground, or run this once locally:

```bash
pip install google-auth-oauthlib
python - <<'PY'
from google_auth_oauthlib.flow import InstalledAppFlow
flow = InstalledAppFlow.from_client_config(
    {"installed": {"client_id": "YOUR_ID", "client_secret": "YOUR_SECRET",
                   "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                   "token_uri": "https://oauth2.googleapis.com/token",
                   "redirect_uris": ["http://localhost"]}},
    scopes=["https://www.googleapis.com/auth/youtube.upload"])
creds = flow.run_local_server(port=0)
print("REFRESH TOKEN:", creds.refresh_token)
PY
```

## 3. ⚠️ Publish the OAuth consent screen

Google Cloud → APIs & Services → **OAuth consent screen** → Publish app.

While it sits in **Testing**, every refresh token **expires after 7 days**. Your runs will
work for a week, then fail at upload after a full render. `kids.py` checks credentials at
step 0 so this surfaces in 5 seconds.

## 4. Test before trusting the schedule

Actions → **Make kids video (3D)** → Run workflow:
- `format: short`, `kind: counting`, `dry_run: true`

Renders and checks without uploading; download the artifact to inspect. Then run once with
`dry_run: false` and confirm the video appears in YouTube Studio as **private**.

## 5. Schedule

Already set in `.github/workflows/make-video.yml`:
Tue 10:00 IST, Thu 10:00 IST (short) · Sun 10:00 IST (long).
