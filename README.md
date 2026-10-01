# Housing bot

A 24/7 assistant for finding a rental home around Den Haag (Den Haag, Rijswijk, Voorburg, Delft and
surroundings). It reads listing alerts the moment they arrive, checks each listing for scams, decides whether
it fits your criteria and whether to apply alone or as a couple, applies within seconds, watches the mailbox
for replies from agents, and keeps everything in a Google Sheet. Booked viewings and anything you need to act
on reach you on WhatsApp.

> **Personal data.** Everything personal (names, phone, incomes, work address, logins) lives in `.env`,
> `config.yaml` and `secrets/` on your server. All three are gitignored. This repository is public:
> never commit them.

## How it works

```
alert email (Stekkies / Funda / Pararius / ...)          reply from an agent
        │                                                      │
        ▼                                                      ▼
  bot mailbox (dedicated Gmail) ── polled every 20 s ──► reply triage (Claude)
        │                                                      │
        ▼                                                      ▼
  listing links ─► fetch page ─► extract facts (Claude)    match to listing
        │                                                      │
        ▼                                                      ▼
  address check (BAG register) · bike time to work ·      status update, viewing
  neighbourhood indicator (CBS) · duplicate check         date, booking link
        │                                                      │
        ▼                                                      ▼
  your criteria · solo or couple · Den Haag permit check   WhatsApp: 🚩 viewing booked,
        │                                                  book now, docs requested, reply
        ▼
  scam check (rules + Claude) ──► likely scam: never applied
        │
        ▼
  message (Claude, A/B-tested style) ─► email the agent, or fill the site's form,
                                        or WhatsApp you the link + message
        │
        ▼
  Google Sheet row (live) · daily summary · weekly self-review
```

### What it will never do

- Pay, book, reserve, or click anything that looks like it.
- Send or upload ID, payslips, income figures or other documents. If an agent asks, you get a WhatsApp and you
  send them yourself.
- Apply to listings with a scam score of 70 or more (`scam.block_score`).
- Create accounts on sites. You do that once and save the login (see [docs/SETUP.md](docs/SETUP.md)).

## The Google Sheet

| Tab | What's in it |
| --- | --- |
| **Listings** | Every home that passed your criteria: status, address, rent, m², bike minutes, area score, scam risk, solo/couple and why, agent phone/email, application method and speed, viewing date, message sent, link. Rows are coloured: red = viewing booked, orange = act now, yellow = call needed, blue = apply yourself. |
| **Skipped** | Homes that didn't fit, already rented, or likely scams, with the reason. |
| **Viewings** | One row per invited or booked viewing. |
| **Stats** | Weekly: viewing rate per message style, source, method, area, agency and speed. |

Statuses: `READY` (dry run) → `APPLIED` → `VIEWING_INVITED` / `VIEWING_BOOKED` / `DOCS_REQUESTED` /
`NEEDS_REPLY` / `REJECTED` / `NO_RESPONSE`, plus `MANUAL_APPLY` (you apply), `LIKELY_SCAM`, `FILTERED`, `FAILED`.

## How it improves

Each application uses one of several message styles (short English, short Dutch, warm English, ...). Every
positive reply or rejection is recorded, and the next message's style is picked by Thompson sampling, so the
style that gets more viewings gets used more. Once a week (Sunday 20:00) it writes the Stats tab, retires a
clearly losing style, and has Claude propose one new style to test.

## Commands

```bash
python -m housing_bot check          # test every connection and setting
python -m housing_bot run            # the 24/7 loop (Docker runs this)
python -m housing_bot process URL    # run one listing through the whole pipeline now
python -m housing_bot test-notify    # send a test WhatsApp
python -m housing_bot login URL      # log in to a site once, save the session (run on your laptop)
python -m housing_bot digest         # send the daily summary now
python -m housing_bot learn          # run the weekly review now
```

With Docker: `docker compose run --rm housing-bot python -m housing_bot check`, and so on.

## Setup

See **[docs/SETUP.md](docs/SETUP.md)**: about 1.5 hours, most of it creating accounts.

## Costs (monthly, estimates)

| Item | Cost |
| --- | --- |
| Server (e.g. Hetzner CX22) | ~€5 |
| Alert service (Stekkies, or keep Signaal for your phone only) | ~€15-20 |
| Claude API (default `claude-opus-5-5` for everything) | roughly €10-40, depending on listing volume. Capped by `llm.daily_budget_usd`. Setting `bulk_model: claude-haiku-4-5` cuts extraction cost about 4x. |
| WhatsApp Cloud API | a few cents per alert (Meta's per-message utility rate for NL) |

## Limits worth knowing

- **Bot protection.** Funda and Pararius show bot checks. When a page can't be read, the bot falls back to the
  alert email's text. When a form can't be submitted, you get a WhatsApp with the link and the ready-written
  message, so you can paste it in 20 seconds.
- **Commute times** are straight-line estimates (25% detour at 16 km/h), not routed.
- **Area score** is a socio-economic indicator from CBS (home values, low-income share, social assistance).
  It's a rough proxy, not a safety rating. It's off as a filter by default and used for ranking only.
- **Den Haag housing permit** rules (rent ≤ €1,228.07 needs a permit, with income caps) are in
  `config.yaml`. Check [denhaag.nl](https://www.denhaag.nl/nl/vergunningen-en-ontheffingen/woonvergunningen/huisvestingsvergunning-aanvragen/)
  when they change.
- The scam check lowers risk, it doesn't remove it. Never pay anything before a viewing and a signed contract.
