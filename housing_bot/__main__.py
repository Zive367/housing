"""Command line: `python -m housing_bot <command>`."""

from __future__ import annotations

import argparse
import imaplib
import logging
import smtplib
import sys
import time

from .config import SECRETS_DIR, load_config


def cmd_login(url: str) -> None:
    """Open a visible browser, let you log in to a site once, and save the session for the bot."""
    from playwright.sync_api import sync_playwright

    from .fetch import HEADERS, session_file
    from .ingest import domain_of

    target = session_file(domain_of(url))
    target.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False)
        context = browser.new_context(locale="nl-NL", user_agent=HEADERS["User-Agent"])
        context.new_page().goto(url)
        input(f"Log in to {domain_of(url)} in the browser window, then press Enter here... ")
        context.storage_state(path=str(target))
        browser.close()
    print(f"Saved session to {target}. Copy it to the server's {SECRETS_DIR / 'sessions'} folder.")


def cmd_check(ctx) -> None:
    """Verify every external connection and setting, with a clear OK/FAIL per line."""
    cfg = ctx.cfg
    results: list[tuple[str, bool, str]] = []

    def check(name: str, fn) -> None:
        try:
            detail = fn() or ""
            results.append((name, True, str(detail)))
        except Exception as e:  # report anything, keep checking the rest
            results.append((name, False, f"{type(e).__name__}: {e}"[:200]))

    def applicant() -> str:
        a = cfg.applicant
        missing = [k for k, v in {"APPLICANT_FIRST_NAME": a.first_name, "APPLICANT_LAST_NAME": a.last_name,
                                  "APPLICANT_PHONE": cfg.secrets.applicant_phone}.items() if not v]
        if missing:
            raise ValueError(f"missing {', '.join(missing)}")
        incomes = "incomes set" if a.gross_monthly_income else "no income set (solo/couple choice is cruder)"
        return f"{a.first_name}, partner: {cfg.partner.first_name or 'none'}, {incomes}"

    def claude() -> str:
        if ctx.llm is None:
            raise ValueError("ANTHROPIC_API_KEY missing")
        return ctx.llm.client.models.retrieve(cfg.llm.model).display_name

    def imap() -> str:
        s = cfg.secrets
        with imaplib.IMAP4_SSL(s.imap_host, timeout=20) as box:
            box.login(s.bot_email, s.bot_email_app_password)
        return s.bot_email

    def smtp() -> str:
        s = cfg.secrets
        with smtplib.SMTP_SSL(s.smtp_host, s.smtp_port, timeout=20) as server:
            server.login(s.bot_email, s.bot_email_app_password)
        return "login ok"

    def sheet() -> str:
        if ctx.sheet is None:
            raise ValueError("GOOGLE_SERVICE_ACCOUNT_FILE / GOOGLE_SHEET_ID missing")
        return ctx.sheet.book.title

    def work() -> str:
        found = ctx.work_location()
        if not found:
            raise ValueError(f"could not find '{cfg.work.address}' in the address register")
        return f"{cfg.work.address} -> {found[0]:.5f}, {found[1]:.5f}"

    def browser() -> str:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            pw.chromium.launch(headless=True).close()
        return "chromium ok"

    def notify() -> str:
        s = cfg.secrets
        needed = {"whatsapp_cloud": [s.whatsapp_token, s.whatsapp_phone_number_id, s.whatsapp_to],
                  "callmebot": [s.callmebot_phone, s.callmebot_apikey], "email": [s.notify_email]}
        if not all(needed.get(s.notify_channel, [None])):
            raise ValueError(f"settings for NOTIFY_CHANNEL={s.notify_channel} incomplete")
        return f"{s.notify_channel} (backup email: {s.notify_email or 'none'})"

    for name, fn in [("Applicant profile", applicant), ("Claude API", claude), ("Mailbox IMAP", imap),
                     ("Mailbox SMTP", smtp), ("Google Sheet", sheet), ("Work address", work),
                     ("Browser", browser), ("Notifications", notify)]:
        check(name, fn)
    for name, ok, detail in results:
        print(f"{'OK  ' if ok else 'FAIL'}  {name:<18} {detail}")
    print(f"\nMode: {'DRY RUN, nothing is sent' if cfg.apply.dry_run else 'LIVE, applications are sent'}")
    if not all(ok for _, ok, _ in results):
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(prog="housing_bot", description="Rental search bot for the Den Haag region")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="watch the mailbox 24/7 and process everything")
    sub.add_parser("check", help="test all connections and settings")
    p = sub.add_parser("process", help="process one listing URL now (respects dry_run)")
    p.add_argument("url")
    p.add_argument("--force", action="store_true", help="re-process a URL that was seen before")
    sub.add_parser("test-notify", help="send a test notification")
    p = sub.add_parser("login", help="log in to a site once in a visible browser and save the session")
    p.add_argument("url")
    sub.add_parser("digest", help="send the daily summary now")
    sub.add_parser("learn", help="run the weekly review now")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if args.command == "login":
        cmd_login(args.url)
        return

    from . import learn, runner
    from .pipeline import build_context, process_candidate

    ctx = build_context(load_config())
    if args.command == "run":
        runner.run(ctx)
    elif args.command == "check":
        cmd_check(ctx)
    elif args.command == "process":
        listing = process_candidate(ctx, args.url, "manual", force=args.force)
        if listing is None:
            print("Already processed. Use --force to run it again.")
            return
        print(listing.model_dump_json(indent=2, exclude={"details": {"red_flag_quotes"}}))
    elif args.command == "test-notify":
        ok = ctx.notifier.send("Test", "If you can read this, notifications work.", "https://example.com")
        print("sent" if ok else "FAILED: check the notification settings in .env")
    elif args.command == "digest":
        text = runner.digest_text(ctx, time.time() - 86400)
        ctx.notifier.send("Daily summary", text)
        print(text)
    elif args.command == "learn":
        print(learn.weekly(ctx))


if __name__ == "__main__":
    main()
