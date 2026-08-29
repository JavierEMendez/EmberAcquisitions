# Ember Tract Underwriting — Deployment Guide

## What this is
A fully self-contained web application for land acquisition underwriting.
- No Excel required on any computer
- All calculations run in Python on the server
- PostgreSQL database stores all projects
- Username/password authentication
- Shareable URL for your whole team

---

## Deploy to Railway (Recommended — ~10 minutes)

### Step 1: Create a GitHub repo
1. Go to https://github.com/new
2. Create a new **private** repository (e.g. `ember-underwriting`)
3. Upload all files from this folder to that repo

### Step 2: Sign up for Railway
1. Go to https://railway.app
2. Sign up with your GitHub account (free)

### Step 3: Create the app
1. Click **New Project**
2. Choose **Deploy from GitHub repo**
3. Select your `ember-underwriting` repo
4. Railway will auto-detect Python and start building

### Step 4: Add PostgreSQL
1. In your Railway project, click **+ New**
2. Choose **Database → PostgreSQL**
3. Railway will automatically add `DATABASE_URL` to your environment

### Step 5: Set environment variables
In Railway → your app service → Variables, add:
```
SECRET_KEY = (generate a random string, e.g. use: python3 -c "import secrets; print(secrets.token_hex(32))")
```

### Step 6: Deploy
Railway will automatically deploy. Your app will be live at:
`https://your-app-name.up.railway.app`

---

## First Login
- **Username:** `admin`
- **Password:** `ember2024`

**Change this immediately** via the Admin panel (⚙ button in sidebar).

---

## Adding Team Members
1. Log in as admin
2. Click the **⚙ Admin** button in the sidebar
3. Add usernames and passwords for each team member
4. They can log in immediately at your Railway URL

---

## Local Development (optional)
```bash
# Install dependencies
pip install -r requirements.txt

# Set up local PostgreSQL (or use Railway's DATABASE_URL)
export DATABASE_URL="postgresql://user:password@localhost/ember"
export SECRET_KEY="dev-secret-key"

# Run
python app.py
# Open http://localhost:5001
```

---

## Costs
- Railway free tier: $5/month credit — more than enough for a small team
- PostgreSQL: included in Railway free tier
- If you need more: Railway Hobby plan is $20/month

---

## Files
```
ember_web/
├── app.py              # Flask backend + auth + API routes
├── calc.py             # Python calculation engine (no Excel needed)
├── requirements.txt    # Python dependencies
├── Procfile            # Process startup for Railway/Render
├── railway.toml        # Railway configuration
├── templates/
│   ├── login.html      # Login page
│   └── app.html        # Main application
└── static/
    └── img/
        └── ember_logo.png
```

## Acquisitions GIS tab

Two things this tab needs that no other dashboard does.

**A persistent volume.** The statewide parcel cache is a ~1.3 GB SQLite file
with an R-Tree index — far too large for the image and wrong for Postgres,
since it is a rebuildable geometry cache rather than application state. Mount a
Railway volume and point `ACQ_DATA_DIR` at it:

| Var | Value | Why |
|---|---|---|
| `ACQ_DATA_DIR` | `/app/acq-data` (matching the volume mount path) | Where `parcels_cache.db` lives. Without it the cache is rebuilt into the container and lost on every deploy, and every parcel search falls back to a slow live StratMap query. |

**Worker threads.** `gunicorn.conf.py` now sets `threads = 4`
(`GUNICORN_THREADS`). The analysis endpoint blocks for 30–60s on external GIS
services — FEMA, USFWS wetlands, USGS elevation. With 2 sync workers and no
threads, two concurrent analyses would occupy both workers and the entire
portal would stop answering. Do not drop this back to workers-only.

Optional, each degrading one card rather than breaking the page:

| Var | Effect if unset |
|---|---|
| `CBAS_TOKEN` | competition / builder card reads "not configured" |
| `CENSUS_API_KEY` | ACS demographics return empty |
| `FRED_API_KEY` | macro context is skipped |
