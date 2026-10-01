# Setup (about 1.5 hours)

Do the steps in order. Keep everything private and separate from work: private accounts, a private email
address and a private card.

## 1. Mailbox (5 min)

You can use your own Gmail. The bot never downloads your normal mail: it only asks Gmail for messages from
the housing sites and messages sent to your **housing address**, `yourname+housing@gmail.com`. Gmail delivers
that address to your normal inbox, so there's nothing to create.

1. Turn on 2-Step Verification and create an **App password** (Google Account → Security → App passwords).
   The setup wizard asks for it; never paste it anywhere else.
2. Applications go out from your Gmail with replies directed to the housing address, so agents' answers
   reach the bot.

## 2. Free listing alerts → your Gmail (20 min)

Use your account on each site (Google login is fine) and save a search with email alerts to your Gmail.
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

1. In your Google account, create an empty Google Sheet. Its ID is the long
   part of the URL between `/d/` and `/edit` → `GOOGLE_SHEET_ID`.
2. Go to console.cloud.google.com → create a project → **APIs & Services → Library** → enable
   **Google Sheets API** and **Google Drive API**.
3. **IAM & Admin → Service accounts** → create one (no roles needed) → **Keys → Add key → JSON**. Save the
   file as `secrets/google-service-account.json`.
4. Share the sheet with the service account's email address (ends in `iam.gserviceaccount.com`) as **Editor**.

The bot creates the tabs, headers and colours itself.

## 5. Notifications by email (5 min)

The wizard asks where alerts go (your Gmail is fine, never a work address). Alerts arrive
from your Gmail with subjects like:

- `[Housing bot] 🚩 VIEWING BOOKED`, `🚩 Book the viewing NOW`, `🚩 Agent asks for documents`,
  `🚩 Reply needed`, `🚩 Apply yourself, fast`: act on these quickly.
- `[Housing bot] Daily summary` (08:00) and `Weekly review` (Sunday evening).

On your phone, turn on notifications for that inbox. In Gmail you can add a filter on `subject:("[Housing bot] 🚩")`
→ **Star it** + **Always mark as important**, so viewing alerts stand out. Test it with
`python -m housing_bot test-notify`.

## 6. Server (15 min)

1. Hetzner Cloud (or similar) → create a **CX22** server with **Ubuntu 24.04**, location Falkenstein or
   Nuremberg, add your SSH key. About €5/month.
2. Copy your Google key to the server, then log in and run the installer:

   ```bash
   scp google-service-account.json root@SERVER:/root/
   ssh root@SERVER
   git clone https://github.com/Zive367/housing.git && cd housing
   mkdir -p secrets && mv /root/google-service-account.json secrets/
   ./scripts/install.sh
   ```

   The installer installs Docker, builds the bot, asks you the setup questions (bot Gmail, keys, your and
   your partner's details, work address, budget), writes `.env` and `config.yaml`, runs `check`, and starts the
   bot. If a check says FAIL, fix it (`nano .env` or `nano config.yaml`) and run `./scripts/install.sh` again.

## 7. Site logins (optional, 10 min, on your laptop)

Some sites only accept reactions from a logged-in account (Pararius, Kamernet, ...). The bot reuses a saved
login. Google blocks "Sign in with Google" inside automated browsers, so first give each site account a normal
password: on the site's login page use **Forgot password** with your Gmail address. Then, on your laptop:

```bash
pip install -e . && python -m playwright install chromium
python -m housing_bot login https://www.pararius.nl/login
python -m housing_bot login https://kamernet.nl/en/login
scp secrets/sessions/*.json root@SERVER:housing/secrets/sessions/
```

Without a saved login, those sites end up as "apply yourself" emails with the text ready to paste.

## 8. Dry run, then go live

The installer started the bot. Watch it with `docker compose logs -f`.

`apply.dry_run: true` is the default: everything runs and messages are written into the sheet (status
`READY`), but nothing is sent. Test it on one real listing too:

```bash
docker compose run --rm housing-bot python -m housing_bot process "https://www.funda.nl/detail/huur/..."
```

That shows whether the site's pages and forms work from your server. After a day, read the messages in the
sheet's **Message sent** column. When you're happy, set `apply.dry_run: false` in `config.yaml` and run
`docker compose restart`.

## 9. Phone dashboard (10 min)

The bot serves a page with everything that needs you first (book a viewing, apply yourself, documents,
calls), with buttons: **Apply** copies the message and opens the listing, plus **Mark applied**, **Viewing
booked**, **Called**, **Rejected**, **Skip**. Every tap updates the Google Sheet too.

It is only reachable through **Tailscale** (free), a private network between your server and your phone, so
it is never on the open internet.

1. Set a password: `nano .env` and fill in `DASHBOARD_PASSWORD=` (12+ characters), then
   `docker compose up -d --build`.
2. On the server:

   ```bash
   curl -fsSL https://tailscale.com/install.sh | sh
   tailscale up                 # open the link it prints and log in (Google login is fine)
   tailscale serve --bg 8080    # prints your private address, like https://ubuntu-2gb-nbg1-1.tailXXXX.ts.net
   ```
3. On your phone: install the **Tailscale** app, log in with the same account, switch it on.
4. Open the `https://...ts.net` address in your phone's browser, log in with the dashboard password, then
   **Add to Home Screen** (Safari: share button; Chrome: ⋮ menu). It now opens like an app.

### Settings from your phone (you and your partner)

Tap the ⚙ icon on the dashboard to change names, phone, jobs, incomes, budget, sizes, areas, languages and
practice mode. Saving takes effect immediately and is stored in `data/settings.json` on the server, which wins
over `config.yaml` and `.env`. Keys and passwords can only be changed on the server.

To give your partner access: in the Tailscale admin console (login.tailscale.com) open **Machines**, click the
server's **⋯ → Share**, and send her the invite. She installs the Tailscale app, accepts, and logs in to the
dashboard with the same password.

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
