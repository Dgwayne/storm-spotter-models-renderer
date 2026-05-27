# Cloudflare R2 + Domain Setup Guide

Step-by-step setup for `models.dgwaynes.com` → Cloudflare R2 →
storm-spotter-models-renderer pipeline.

**Total time:** ~30 minutes (plus ~24 hours for nameserver propagation,
but you can start rendering before that finishes).

**Cost:** $0/month forever, as long as you stay under 10 GB R2 storage.

---

## Prerequisites

- A GitHub account (already done — you have `Dgwayne/storm-spotter-models-renderer`).
- A domain you control. This guide uses `dgwaynes.com` on Squarespace.
- ~30 minutes of focused time. Don't rush — the steps are reversible if you
  make a mistake, but easier if you do them in order.

---

## Part 1 — Create a Cloudflare account

1. Go to https://dash.cloudflare.com/sign-up.
2. Sign up with your email. Use a password manager.
3. Verify your email when the confirmation arrives.

✅ You should now see the Cloudflare dashboard at https://dash.cloudflare.com.

---

## Part 2 — Add `dgwaynes.com` to Cloudflare (nameserver migration)

> **Why we do this:** Cloudflare R2's custom-domain feature requires the
> whole DNS zone for the domain to be served by Cloudflare's nameservers.
> We're going to point `dgwaynes.com`'s nameservers from Squarespace to
> Cloudflare, but keep any existing DNS records intact so your domain
> keeps working.

### 2a. Inventory your current Squarespace DNS

Before changing anything, screenshot your current Squarespace DNS records
so you can restore them if needed.

1. Log into Squarespace.
2. Settings → Domains → click `dgwaynes.com`.
3. DNS Settings → screenshot the table of "Custom Records".
4. Note down which records exist — typically:
   - A records pointing to Squarespace's website IPs (`198.185.159.144` etc.)
   - CNAME records like `www` → `ext-sq.squarespace.com`
   - MX records if you have email

You will re-create these on Cloudflare in the next step.

### 2b. Add the site to Cloudflare

1. In the Cloudflare dashboard, click **+ Add site** (top right).
2. Type `dgwaynes.com` → **Add site**.
3. Choose the **Free plan** → **Continue**.
4. Cloudflare will scan your existing DNS records. **Important:** it usually
   catches most of them, but compare against your Squarespace screenshot from
   step 2a. Add any that Cloudflare missed by clicking **+ Add record**.
5. Once the record list matches what you had at Squarespace, click **Continue**.

### 2c. Update Squarespace's nameservers

Cloudflare will display two nameservers like:

```
tina.ns.cloudflare.com
walt.ns.cloudflare.com
```

Yours will be different — **use the ones Cloudflare shows you, not these examples.**

1. Copy both nameservers.
2. Back in Squarespace → Settings → Domains → `dgwaynes.com` → **Use custom nameservers**.
3. Paste the two Cloudflare nameservers, replacing the existing Squarespace ones.
4. Save.

### 2d. Wait for propagation

1. Back in Cloudflare → click **Done, check nameservers**.
2. Propagation usually takes 10 minutes to 4 hours, sometimes up to 24.
3. You'll get an email "🎉 dgwaynes.com is now on Cloudflare" when it's live.

> **You can continue with Parts 3–5 while waiting.** Only Part 6 (connecting
> the custom domain to R2) needs the migration to finish.

---

## Part 3 — Create the R2 bucket

1. Cloudflare dashboard → left sidebar → **R2 Object Storage**.
2. First time? You'll be asked to **Purchase R2** — pick the **Free plan**.
   You'll be prompted to add a payment method as a safety net for overages,
   **but you will not be charged as long as you stay under 10 GB**. You can
   set a billing alert at $0.01 if you want extra paranoia.
3. Click **Create bucket**.
4. Bucket name: `storm-spotter-models`
5. Location: **Automatic** (default).
6. Default storage class: **Standard** (default).
7. Click **Create bucket**.

✅ You should now see an empty bucket named `storm-spotter-models`.

---

## Part 4 — Create an R2 API token

This is the key the GitHub Actions workflows will use to upload PNG frames.

1. R2 → click **Manage R2 API Tokens** (top-right).
2. Click **Create API Token**.
3. **Token name:** `storm-spotter-renderer`
4. **Permissions:** **Object Read & Write** (NOT "Admin").
5. **Specify bucket(s):** pick **Apply to specific buckets only** → check
   `storm-spotter-models`.
6. **TTL:** **Forever** (no expiry). You can rotate later if needed.
7. Click **Create API Token**.

8. **CRITICAL:** the next screen shows three values. Copy ALL of them into a
   password manager or temporary text file — Cloudflare will NEVER show them again:
   - **Access Key ID** (looks like `abc123...`)
   - **Secret Access Key** (looks like `xyz789...`)
   - **Endpoint** for S3 clients (looks like `https://<account>.r2.cloudflarestorage.com`)

9. Click **Finish**.

---

## Part 5 — Add the four secrets to GitHub

1. Open https://github.com/Dgwayne/storm-spotter-models-renderer/settings/secrets/actions
   (this exists once the repo is pushed — see Part 7 below if not yet).
2. Click **New repository secret** four times, one for each:

   | Name | Value |
   |------|-------|
   | `R2_ACCESS_KEY_ID` | Access Key ID from step 4.8 |
   | `R2_SECRET_ACCESS_KEY` | Secret Access Key from step 4.8 |
   | `R2_ENDPOINT` | Endpoint from step 4.8 |
   | `R2_BUCKET` | `storm-spotter-models` |

✅ All four should now appear in the secrets list (values masked, of course).

---

## Part 6 — Connect `models.dgwaynes.com` to the R2 bucket

> Only proceed once the "site is on Cloudflare" email from Part 2d has arrived.
> Otherwise Cloudflare will refuse the custom domain step.

1. R2 → click your bucket `storm-spotter-models`.
2. **Settings** tab → scroll to **Public access** → **Custom Domains**.
3. Click **Connect Domain**.
4. Enter `models.dgwaynes.com` → **Continue**.
5. Cloudflare will automatically add a CNAME record at
   `models.dgwaynes.com` → `pub-<id>.r2.dev` in your DNS zone.
6. Click **Connect Domain** to confirm.

7. SSL/TLS provisioning takes ~5 minutes. When the status changes to **Active**,
   test it by uploading a tiny test file:

   ```bash
   echo "hello world" > /tmp/test.txt
   rclone copy /tmp/test.txt r2:storm-spotter-models/test.txt
   curl https://models.dgwaynes.com/test.txt
   # Should print: hello world
   rclone delete r2:storm-spotter-models/test.txt
   ```

✅ `models.dgwaynes.com` now serves anything in the R2 bucket over HTTPS,
publicly readable. Egress = $0 forever.

---

## Part 7 — Trigger the first render

1. https://github.com/Dgwayne/storm-spotter-models-renderer/actions
2. Left sidebar → **Render HRRR** workflow → **Run workflow** → main branch → **Run workflow**.
3. Watch the run. First time takes ~20 minutes (152 PNGs uploaded).
4. Once green, open in browser:

   ```
   https://models.dgwaynes.com/v1/HRRR/manifest.json
   ```

   You should see JSON with one run, 8 products, 19+ frames available.

5. Pick any frame from the manifest, e.g.:

   ```
   https://models.dgwaynes.com/v1/HRRR/refc/2026052618/F006.png
   ```

   Should render a CONUS-shaped composite reflectivity image.

✅ Backend is live. Cron will keep it fresh every 15 minutes.

---

## Part 8 — Update the Flutter app constant

Open the app repo:

```
lib/core/config/app_constants.dart
```

Set the constant `weatherModelsBaseUrl` to:

```dart
static const String weatherModelsBaseUrl = 'https://models.dgwaynes.com/v1';
```

That's the only app-side touchpoint to the backend.

---

## Troubleshooting

**"Render HRRR" workflow runs but uploads 0 frames**
- Check `secrets`: all four must be set.
- Workflow log will show whether `.idx` HEAD requests are succeeding.
- HRRR run from "now - 2 hours" must actually exist; if NOAA is delayed,
  the next cron tick (15 min later) usually catches it.

**`models.dgwaynes.com` returns SSL error**
- Provisioning can take 15 minutes. Wait and retry.
- Cloudflare → SSL/TLS → make sure **Full** mode is enabled (it is by default for R2).

**Squarespace site is broken after nameserver migration**
- Compare current Cloudflare DNS records with the screenshot from step 2a.
- Add back any missing A/CNAME/MX records via Cloudflare → DNS.
- Squarespace help: https://support.squarespace.com/hc/en-us/articles/206192847

**R2 dashboard shows storage growing past 5 GB**
- Edit `config/products.yml`, change `retain_runs: 5` → `retain_runs: 3` for HRRR.
- Next cron tick will prune.

---

## Cost monitoring checklist (do this weekly for the first month)

- [ ] Cloudflare → R2 → Bucket → **Storage**: should be < 500 MB
- [ ] Cloudflare → R2 → **Class A operations**: should be < 50k/day
- [ ] Cloudflare → Billing: should show $0.00
- [ ] GitHub → Settings → Billing → Actions: should show 0 minutes used
  (public repo = unlimited)

If anything looks off, open an issue and we'll figure it out.
