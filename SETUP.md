# Clipper: channel setup guide

Put every key in `.env` (in the Clipper folder). Never paste keys or passwords into chat.

| # | Channel | Time | Status |
|---|---|---|---|
| 1 | Twitch (clip source) | 5 min | ✅ done |
| 2 | YouTube Shorts | 20 min + audit wait | |
| 3 | Facebook Page + Instagram account | 10 min | |
| 4 | Meta app (Instagram + Facebook Reels) | 15 min | |
| 5 | Cloudflare R2 (video hosting for Instagram) | 10 min | |
| 6 | Twitch uploads | none | manual |

---

## 1. Twitch (clip source): ✅ done
`TWITCH_CLIENT_ID` and `TWITCH_CLIENT_SECRET` are already in `.env`.

---

## 2. YouTube Shorts

**Before you start:** be logged into the Google account that owns the YouTube channel.
If you don't have a channel yet, open youtube.com → profile picture → **Create a channel**.

1. Open **https://console.cloud.google.com** and accept the terms if asked.
2. Top bar → project picker → **New Project** → name it `Clipper` → **Create** → select it.
3. Search bar: type **YouTube Data API v3** → open it → **Enable**.
4. Left menu → **Google Auth Platform** (or "OAuth consent screen") → **Get started**:
   - App name: `Clipper` · User support email: your email → Next
   - Audience: **External** → Next
   - Contact email: your email → Next → agree → **Create**
5. **Audience** page → **Test users** → **Add users** → add the Google account that owns the channel.
6. Still on **Audience**: click **Publish app** → confirm.
   (Without this, YouTube logs you out every 7 days. You'll see an "unverified app"
   warning when connecting. That's normal for a personal app: click *Advanced → Go to Clipper*.)
7. **Clients** → **Create client** → Application type **Desktop app** → name `Clipper` → **Create**.
8. Click **Download JSON**. Rename the file to `youtube_client_secret.json` and move it into the Clipper folder.
9. **Submit the audit now** (it takes days to weeks): https://support.google.com/youtube/contact/yt_api_form
   Until it's approved, API uploads are forced to **Private**. You can make them public by hand in YouTube Studio.

Limit: about **6 uploads per day** on the free API quota.

---

## 3. Facebook Page + Instagram professional account

**Facebook Page** (Reels have to go to a Page, not your personal profile):
1. Go to **https://www.facebook.com/pages/create**
2. Name it (e.g. your clip brand) → category **Gaming Video Creator** → **Create Page**.

**Instagram (your account): switch to professional:**
1. Instagram app → profile → ☰ → **Settings and privacy** → **Account type and tools**
   → **Switch to professional account** → choose **Creator** → category **Gaming Video Creator**.

**Link Instagram to the Page:**
1. Facebook → your Page → **Settings** → **Linked accounts** → **Instagram** → **Connect account**
   → log in to Instagram when Meta asks (on Meta's own screen) → allow.
2. Also turn on two-factor authentication on Instagram (Settings → Accounts Center → Password and security).

---

## 4. Meta developer app (posts to Instagram + Facebook)

1. Open **https://developers.facebook.com/apps** → log in with the Facebook account that manages the Page.
   First time? Click **Get started** and verify your account (free).
2. **Create App**:
   - App name: `Clipper` · contact email → Next
   - Use cases: pick **"Manage everything on your Page"** and, if listed,
     **"Manage messaging & content on Instagram"** → Next
   - Business portfolio: pick yours, or "I don't want to connect a business portfolio yet" → Next → **Create app**
3. In the app dashboard → **Use cases** → open each one → **Customize** → add these permissions:
   `pages_show_list`, `pages_read_engagement`, `pages_manage_posts`,
   `instagram_basic`, `instagram_content_publish`, `business_management`
4. Left menu → **Facebook Login for Business** → **Settings** → **Valid OAuth Redirect URIs**:
   ```
   http://localhost:5050/auth/meta/callback
   ```
   → **Save changes**.
5. Left menu → **App settings → Basic** → copy **App ID** and **App Secret** (click *Show*) into `.env`:
   ```
   META_APP_ID=...
   META_APP_SECRET=...
   ```
6. Keep the app in **Development mode**. No app review is needed to post to your own Page and Instagram.

---

## 5. Cloudflare R2 (needed for Instagram only)

Instagram downloads the video from a public link, so the clip has to be hosted somewhere first.
The free tier (10 GB) is plenty. Cloudflare asks for a card to turn on R2, but you won't be charged within the free tier.

1. Sign up at **https://dash.cloudflare.com/sign-up** → log in.
2. Left menu → **R2 Object Storage** → enable it (add the payment method when asked).
3. **Create bucket** → name `clipper` → **Create bucket**.
4. Open the bucket → **Settings** → **Public Development URL** → **Enable** → copy the
   `https://pub-xxxx.r2.dev` address.
5. Back on R2 overview → **Manage API tokens** (or "API" → "Manage R2 API Tokens") → **Create API token**:
   - Permissions: **Object Read & Write** · Apply to: bucket `clipper` → **Create**
   - Copy **Access Key ID** and **Secret Access Key** (shown once).
6. Your **Account ID** is on the R2 overview page (right side).
7. Add to `.env`:
   ```
   R2_ACCOUNT_ID=...
   R2_ACCESS_KEY_ID=...
   R2_SECRET_ACCESS_KEY=...
   R2_BUCKET=clipper
   R2_PUBLIC_URL=https://pub-xxxx.r2.dev
   ```

---

## 6. Twitch uploads (manual)

Twitch has no upload API. In the Clipper page → **Twitch** section → **Approve** → **Download for Twitch**, then:
**https://dashboard.twitch.tv** → **Content** → **Video Producer** → **Upload** → add the title → **Publish**.

---

## Final checklist

Your `.env` should have:
```
TWITCH_CLIENT_ID=...          ✅
TWITCH_CLIENT_SECRET=...      ✅
META_APP_ID=...
META_APP_SECRET=...
R2_ACCOUNT_ID=...
R2_ACCESS_KEY_ID=...
R2_SECRET_ACCESS_KEY=...
R2_BUCKET=clipper
R2_PUBLIC_URL=https://pub-xxxx.r2.dev
```
And the Clipper folder should have `youtube_client_secret.json`.

Then, in the Clipper page: **Connect YouTube** and **Connect Instagram & Facebook** → sign in on each
platform's own page → done. After that, **Approve** = posted.
