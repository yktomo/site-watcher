import argparse
import json
import os
import re
import sys
import time
import smtplib
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path
from typing import Dict, List, Set

import requests
import yaml
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "state.json"


def log(message: str) -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now}] {message}")


def load_config(path: Path) -> Dict:
    if not path.exists():
        raise FileNotFoundError(f"config file not found: {path}")

    with path.open("r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    if not isinstance(cfg, dict):
        raise ValueError("config must be a YAML object")

    cfg.setdefault("interval_minutes", 10)
    cfg.setdefault("notify_on_first_run", False)
    cfg.setdefault("display_mode", "31days")
    cfg.setdefault("headless", True)
    cfg.setdefault("include_court_regex", "")

    urls = cfg.get("urls", {})
    if not isinstance(urls, dict) or not urls.get("top"):
        raise ValueError("config.urls.top is required")

    if cfg["display_mode"] not in ("7days", "31days"):
        raise ValueError("display_mode must be '7days' or '31days'")

    return cfg


def load_state() -> Dict[str, List[str]]:
    if not STATE_FILE.exists():
        return {}
    try:
        with STATE_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_state(state: Dict[str, List[str]]) -> None:
    with STATE_FILE.open("w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def send_webhook(webhook_url: str, message: str, timeout: int) -> None:
    response = requests.post(webhook_url, json={"text": message}, timeout=timeout)
    response.raise_for_status()


def send_line_broadcast(channel_access_token: str, message: str, timeout: int) -> None:
    """Broadcast a text message to all friends of the LINE Official Account."""
    response = requests.post(
        "https://api.line.me/v2/bot/message/broadcast",
        headers={
            "Authorization": f"Bearer {channel_access_token}",
            "Content-Type": "application/json",
        },
        json={"messages": [{"type": "text", "text": message[:5000]}]},
        timeout=timeout,
    )
    response.raise_for_status()


def send_email(smtp_server: str, smtp_port: int, email_from: str, email_password: str,
               email_to: str, subject: str, message: str) -> None:
    """Send email notification"""
    try:
        msg = MIMEMultipart()
        msg["From"] = email_from
        msg["To"] = email_to
        msg["Subject"] = subject
       
        msg.attach(MIMEText(message, "plain", "utf-8"))
       
        with smtplib.SMTP(smtp_server, smtp_port) as server:
            server.starttls()
            server.login(email_from, email_password)
            server.send_message(msg)
        log(f"Email sent to {email_to}")
    except Exception as e:
        log(f"Failed to send email: {e}")


def parse_available_slots(html: str, include_court_pattern: str) -> Set[str]:
    soup = BeautifulSoup(html, "html.parser")
    results: Set[str] = set()
    pattern = re.compile(include_court_pattern) if include_court_pattern else None

    for facility_header in soup.select("h3"):
        facility_name = " ".join(facility_header.get_text(" ", strip=True).split())

        ul = facility_header.find_next("ul")
        if ul is None:
            continue

        for li in ul.find_all("li", recursive=False):
            court_h4 = li.find("h4")
            table = li.find("table")
            if court_h4 is None or table is None:
                continue

            court_name = " ".join(court_h4.get_text(" ", strip=True).split())
            if pattern and not pattern.search(court_name):
                continue

            rows = table.find_all("tr")
            if not rows:
                continue

            header_cells = rows[0].find_all(["th", "td"])
            day_labels = [" ".join(c.get_text(" ", strip=True).split()) for c in header_cells[1:]]

            for row in rows[1:]:
                row_header = row.find(["th", "td"])
                if row_header is None:
                    continue
                time_label = " ".join(row_header.get_text(" ", strip=True).split())

                status_cells = row.find_all("td")
                for idx, cell in enumerate(status_cells):
                    img = cell.find("img")
                    status = img.get("alt", "").strip() if img else " ".join(cell.get_text(" ", strip=True).split())
                    if status != "空いています":
                        continue

                    date_label = day_labels[idx] if idx < len(day_labels) else f"col-{idx}"
                    slot = f"{facility_name} / {court_name} / {date_label} / {time_label}"
                    results.add(slot)

    return results


def fetch_ota_tennis_slots(config: Dict) -> Set[str]:
    top_url = config["urls"]["top"]
    headless = bool(config.get("headless", True))
    env_headless = os.getenv("PLAYWRIGHT_HEADLESS")
    if env_headless is not None:
        headless = env_headless.strip().lower() == "true"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context(locale="ja-JP", timezone_id="Asia/Tokyo")
        page = context.new_page()
        page.set_default_timeout(20000)

        try:
            page.goto(top_url)
            page.get_by_role("link", name="ログインせずに空き状況を検索").click()
            page.get_by_text("全施設から選択").click()

            # Select every "<area>地域庭球場" checkbox so newly added areas
            # are picked up automatically without code changes.
            area_labels = page.locator("label").all_inner_texts()
            areas = sorted({t.strip() for t in area_labels if t.strip().endswith("地域庭球場")})
            for area in areas:
                page.get_by_text(area, exact=True).click()

            page.get_by_role("button", name="選択した条件で次へ").click()

            # Select every individual court checkbox on the facility list so
            # newly added courts are picked up automatically. Which of these
            # actually trigger notifications is controlled by
            # config.include_court_regex, not by this selection.
            page.wait_for_selector('input[type=checkbox][id^="r_record"]')
            page.evaluate(
                "document.querySelectorAll('input[type=checkbox][id^=\"r_record\"]')"
                ".forEach(b => b.click())"
            )
            page.get_by_role("button", name="選択した施設で検索").click()
           
            if config.get("display_mode") == "31days":
                page.get_by_text("31日間").click()
                page.get_by_role("button", name="選択した条件で表示").click()

            page.wait_for_load_state("domcontentloaded")
            html = page.content()
            return parse_available_slots(html, config.get("include_court_regex", ""))
        except PlaywrightTimeoutError as ex:
            raise RuntimeError(f"操作タイムアウト: {ex}") from ex
        finally:
            context.close()
            browser.close()


def check_once(config: Dict, webhook_url: str, timeout: int, email_config: Dict = None,
                line_config: Dict = None) -> None:
    if email_config is None:
        email_config = {"enabled": False}
    if line_config is None:
        line_config = {"enabled": False}

    state = load_state()
    key = "ota-hard-tennis"

    current = fetch_ota_tennis_slots(config)
    old_slots = set(state.get(key, []))

    if not old_slots and not config.get("notify_on_first_run", False):
        new_slots = set()
    else:
        new_slots = current - old_slots

    if new_slots:
        sorted_slots = sorted(new_slots)
        lines = "\n".join(f"- {slot}" for slot in sorted_slots[:30])
        remaining = len(sorted_slots) - 30
        if remaining > 0:
            lines += f"\n- ... and {remaining} more"

        msg = (
            "[空き通知] 大田区 硬式テニスコート\n"
            f"new vacancies: {len(new_slots)}\n"
            f"{lines}"
        )
        if webhook_url:
            send_webhook(webhook_url, msg, timeout)

        # Send email if enabled
        if email_config.get("enabled"):
            send_email(
                email_config["smtp_server"],
                email_config["smtp_port"],
                email_config["email_from"],
                email_config["email_password"],
                email_config["email_to"],
                "[空き通知] 大田区 硬式テニスコート",
                msg
            )

        # Send LINE broadcast if enabled
        if line_config.get("enabled"):
            try:
                send_line_broadcast(line_config["channel_access_token"], msg, timeout)
                log("LINE broadcast sent")
            except Exception as e:
                log(f"Failed to send LINE broadcast: {e}")

        log(f"notification sent: {len(new_slots)} new slot(s)")
    else:
        log("no new vacancies")

    state[key] = sorted(current)
    save_state(state)


def main() -> int:
    parser = argparse.ArgumentParser(description="OTA hard tennis vacancy watcher")
    parser.add_argument("--config", default="config.yaml", help="Path to YAML config file")
    parser.add_argument("--once", action="store_true", help="Run only once")
    args = parser.parse_args()

    load_dotenv(BASE_DIR / ".env")

    config_path = (BASE_DIR / args.config).resolve() if not Path(args.config).is_absolute() else Path(args.config)
    try:
        config = load_config(config_path)
    except Exception as ex:
        log(f"failed to load config: {ex}")
        return 1

    webhook_url = os.getenv("WEBHOOK_URL", "").strip()
    timeout = int(os.getenv("REQUEST_TIMEOUT", "20"))
    interval_minutes = int(config.get("interval_minutes", 10))

    # Email settings
    email_enabled = os.getenv("EMAIL_ENABLED", "false").lower() == "true"
    email_config = {
        "enabled": email_enabled,
        "smtp_server": os.getenv("EMAIL_SMTP_SERVER", "smtp.gmail.com"),
        "smtp_port": int(os.getenv("EMAIL_SMTP_PORT", "587")),
        "email_from": os.getenv("EMAIL_FROM", ""),
        "email_password": os.getenv("EMAIL_FROM_PASSWORD", ""),
        "email_to": os.getenv("EMAIL_TO", ""),
    }

    # LINE settings
    line_enabled = os.getenv("LINE_ENABLED", "false").lower() == "true"
    line_config = {
        "enabled": line_enabled,
        "channel_access_token": os.getenv("LINE_CHANNEL_ACCESS_TOKEN", ""),
    }
    if line_enabled and not line_config["channel_access_token"]:
        log("LINE_ENABLED is true but LINE_CHANNEL_ACCESS_TOKEN is not set. define it in .env")
        return 1

    if not webhook_url and not email_enabled and not line_enabled:
        log("no notification channel is configured. set WEBHOOK_URL, EMAIL_ENABLED or LINE_ENABLED in .env")
        return 1

    if args.once:
        try:
            check_once(config, webhook_url, timeout, email_config, line_config)
            return 0
        except Exception as ex:
            log(f"failed: {ex}")
            return 1

    log(f"watcher started. interval={interval_minutes} minutes")
    while True:
        try:
            check_once(config, webhook_url, timeout, email_config, line_config)
        except Exception as ex:
            log(f"failed: {ex}")
        time.sleep(max(interval_minutes, 1) * 60)


if __name__ == "__main__":
    sys.exit(main())
