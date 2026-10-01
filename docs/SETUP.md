# Setup (about 1.5 hours)

Do the steps in order. Keep everything private and separate from work: private accounts, a private email
address and a private card.

## 1. Bot mailbox (10 min)

1. Create a new Gmail account, e.g. `firstname.lastname.housing@gmail.com`. Agents will see this address.
2. Turn on 2-Step Verification, then create an **App password** (Google Account → Security → App passwords).
   That 16-character password goes in `.env` as `BOT_EMAIL_APP_PASSWORD`.
3. In Gmail → Settings → Filters, create a filter for `from:(funda OR pararius OR huurwoningen OR kamernet)`
   with **Never send it to Spam**. The bot also checks the spam folder, but filters keep things fast.

## 2. Free listing alerts → the bot mailbox (20 min)

Create a free account on each site with the **bot mailbox** address and save a search with email alerts.
Filters everywhere: Den Haag, Rijswijk, Voorburg/Leidschendam, Delft (+ Wassenaar, Nootdorp if you like),
max €1,500, from 25 m², apartment/studio/house, no rooms. Choose the fastest frequency each site offers
("direct" / "instant", not daily).

- **Funda**: saved search → notifications by email.
- **Pararius**: zoekopdracht opslaan → email alert.
- **Huurwoningen.nl** and **Kamernet**: search alert by email. (Reacting on these sites may need their paid
  plan; then those listings come to you as "apply yourself" instead.)

More sources = more listings. Duplicates across sites are merged automatically. If you add another free
alert source later, put its sending domain in `alerts.sender_domains` in `config.yaml`.

Signaal can't feed the bot (app push only), but it's fine to keep using it yourself next to the bot.

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

## 5. Notifications by email (5 min)

Set `NOTIFY_EMAIL` in `.env` to your personal email (not the bot mailbox, not a work address). Alerts arrive
from the bot mailbox with subjects like:

- `[Housing bot] 🚩 VIEWING BOOKED`, `🚩 Book the viewing NOW`, `🚩 Agent asks for documents`,
  `🚩 Reply needed`, `🚩 Apply yourself, fast`: act on these quickly.
- `[Housing bot] Daily summary` (08:00) and `Weekly review` (Sunday evening).

On your phone, turn on notifications for that inbox. In Gmail you can add a filter on `subject:("[Housing bot] 🚩")`
→ **Star it** + **Always mark as important**, so viewing alerts stand out. Test it with
`python -m housing_bot test-notify`.

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

Without a saved login, those sites end up as "apply yourself" emails with the text ready to paste.

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

- **Email alerts** (🚩) tell you about booked viewings, viewing invites (book fast), document requests, questions,
  and listings you need to apply to yourself. You get a summary every morning at 08:00 and a review on Sundays.
- **The sheet** is the full picture. Filter on the 🚩 and 📞 columns.
- **Calls:** the `📞` column marks listings where calling helps: phone-only listings, and applications with no
  reply after 4 days.
- **Never** pay before you've seen the home and signed a contract, whatever the scam score says.

## Updating

```bash
git pull && docker compose build && docker compose up -d
```
