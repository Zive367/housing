# Setup (about 1.5 hours)

Do the steps in order. Keep everything private and separate from work: private accounts, a private email
address and a private card.

## 1. Bot mailbox (10 min)

1. Create a new Gmail account, e.g. `firstname.lastname.housing@gmail.com`. Agents will see this address.
2. Turn on 2-Step Verification, then create an **App password** (Google Account → Security → App passwords).
   That 16-character password goes in `.env` as `BOT_EMAIL_APP_PASSWORD`.
3. In Gmail → Settings → Filters, create a filter for `from:(stekkies OR funda OR pararius OR huurwoningen OR kamernet)`
   with **Never send it to Spam**. The bot also checks the spam folder, but filters keep things fast.

## 2. Listing alerts → the bot mailbox (20 min)

**Signaal only sends push notifications to its app.** It has no email alerts and no API, so the bot can't read it.
Keep it on your phone if you like it, but the bot needs alerts by email:

- **Stekkies** (recommended, ~€17/month, 1,000+ sites, alerts within ~30 s): set email alerts to the bot
  mailbox. Filters: Den Haag, Rijswijk, Voorburg/Leidschendam, Delft (+ Wassenaar, Nootdorp if you like),
  max €1,500, from 25 m², apartment/studio/house, no rooms.
- **Free extras** (also to the bot mailbox, same filters, "instant/direct" frequency where offered):
  Funda (saved search), Pararius (zoekopdracht), Huurwoningen.nl, Kamernet.
- More sources = faster and more listings. Duplicates are merged automatically.

If you use a different alert service, add its sending domain to `alerts.sender_domains` in `config.yaml`.

## 3. Claude API key (5 min)

console.anthropic.com → create an account (private card) → API keys → create key → `ANTHROPIC_API_KEY`.
Set a **monthly spend limit** in the console as well (e.g. $60). The bot also stops itself at
`llm.daily_budget_usd`.

## 4. Google Sheet (15 min)

1. In your private Google account (the new Gmail is fine), create an empty Google Sheet. Its ID is the long
   part of the URL between `/d/` and `/edit` → `GOOGLE_SHEET_ID`.
2. Go to console.cloud.google.com → create a project → **APIs & Services → Library** → enable
   **Google Sheets API** and **Google Drive API**.
3. **IAM & Admin → Service accounts** → create one (no roles needed) → **Keys → Add key → JSON**. Save the
   file as `secrets/google-service-account.json`.
4. Share the sheet with the service account's email address (ends in `iam.gserviceaccount.com`) as **Editor**.

The bot creates the tabs, headers and colours itself.

## 5. WhatsApp notifications (30 min, or 2 min with option B)

**Option A: official WhatsApp Cloud API (recommended, a few cents per message).**

1. developers.facebook.com → create an app of type **Business** → add the **WhatsApp** product.
2. In **WhatsApp → API Setup** you get a free test sender number and its **Phone number ID**
   (`WHATSAPP_PHONE_NUMBER_ID`). Add your own WhatsApp number as a recipient and verify it (`WHATSAPP_TO`,
   format `316XXXXXXXX`).
3. Create a **permanent access token**: Meta Business settings → System users → add one (Admin) → assign the
   app → Generate token with `whatsapp_business_messaging` permission → `WHATSAPP_TOKEN`.
   (The temporary token on the API Setup page expires after 24 hours.)
4. **WhatsApp Manager → Message templates → Create template**: category **Utility**, name `housing_alert`,
   language **English**, body:

   ```
   Housing bot: {{1}}

   {{2}}

   Link: {{3}}

   Automatic update from your rental search.
   ```

   Sample values: `Viewing booked` · `Prinsegracht 12, Den Haag on Tue 7 Oct 18:30, agent Petra` ·
   `https://www.pararius.nl/`. Approval usually takes minutes to a day.

**Option B: CallMeBot (free, unofficial, 2 minutes).** Follow callmebot.com's WhatsApp instructions to get
an API key, then set `NOTIFY_CHANNEL=callmebot`, `CALLMEBOT_PHONE`, `CALLMEBOT_APIKEY`. Downside: a third party
sees your alerts (addresses, viewing times), and it's a hobby service that can stop working.

Either way, set `NOTIFY_EMAIL` to your personal email. Important alerts (viewing booked, act now) are always
emailed there too, so a WhatsApp hiccup can't hide them.

## 6. Server (20 min)

1. Hetzner Cloud (or similar) → create a **CX22** server with **Ubuntu 24.04**, location Falkenstein or
   Nuremberg, add your SSH key. About €5/month.
2. On the server:

   ```bash
   curl -fsSL https://get.docker.com | sh
   git clone https://github.com/Zive367/housing.git && cd housing
   cp .env.example .env && nano .env                     # fill everything in
   cp config.example.yaml config.yaml && nano config.yaml  # set work.address to your workplace
   mkdir -p secrets/sessions data
   # copy secrets/google-service-account.json here (scp from your laptop)
   docker compose build
   docker compose run --rm housing-bot python -m housing_bot check
   ```

   `check` must say OK on every line. Fix whatever says FAIL first.

## 7. Site logins (optional, 10 min, on your laptop)

Some sites only accept reactions from a logged-in account (Pararius, Kamernet, ...). Create those accounts with
the **bot mailbox** address, then save each login once:

```bash
pip install -e . && python -m playwright install chromium
python -m housing_bot login https://www.pararius.nl/login
python -m housing_bot login https://kamernet.nl/en/login
scp secrets/sessions/*.json server:housing/secrets/sessions/
```

Without a saved login, those sites end up as "apply yourself" WhatsApp messages with the text ready to paste.

## 8. Start in dry-run mode

```bash
docker compose up -d
docker compose logs -f
```

`apply.dry_run: true` is the default: everything runs and messages are written into the sheet (status
`READY`), but nothing is sent. Test it on one real listing too:

```bash
docker compose run --rm housing-bot python -m housing_bot process "https://www.funda.nl/detail/huur/..."
```

That shows whether the site's pages and forms work from your server. After a day, read the messages in the
sheet's **Message sent** column. When you're happy, set `apply.dry_run: false` in `config.yaml` and run
`docker compose restart`.

## Day to day

- **WhatsApp** tells you about booked viewings (🚩), viewing invites (book fast), document requests, questions,
  and listings you need to apply to yourself. You get a summary every morning at 08:00 and a review on Sundays.
- **The sheet** is the full picture. Filter on the 🚩 and 📞 columns.
- **Calls:** the `📞` column marks listings where calling helps: phone-only listings, and applications with no
  reply after 4 days.
- **Never** pay before you've seen the home and signed a contract, whatever the scam score says.

## Updating

```bash
git pull && docker compose build && docker compose up -d
```
