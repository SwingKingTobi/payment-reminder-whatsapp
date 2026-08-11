#!/usr/bin/env python3
"""
Internet-Anteil-Erinnerung für WhatsApp-Gruppenchat
=====================================================

Sendet am 15. jeden Monats eine Ja/Nein-Frage in einen WhatsApp-Gruppenchat
("Hast du deinen Internet-Anteil überwiesen? Antworte mit Ja oder Nein").
Falls der Mitbewohner bis zum 20. nicht mit "Ja" geantwortet hat, wird
automatisch eine Erinnerung nachgeschickt.

WICHTIG - bitte vor dem ersten Lauf lesen:
-------------------------------------------
1. Dieses Skript nutzt WhatsApp Web über Selenium (kein offizielles API).
   Das ist gegen die WhatsApp-Nutzungsbedingungen für "automatisierte"
   Nutzung - bei diesem geringen Volumen (2x/Monat) ist das Risiko einer
   Sperre praktisch vernachlässigbar, aber es gibt keine Garantie.
2. Beim allerersten Start öffnet sich ein sichtbares Firefox-Fenster mit
   einem QR-Code. Einmal mit dem Handy scannen (WhatsApp > Verknüpfte
   Geräte). Die Session wird danach im Profilordner gespeichert, sodass
   spätere Läufe (auch headless) ohne erneutes Scannen funktionieren.
3. WhatsApp Web ändert gelegentlich seine HTML-Struktur. Falls das Skript
   irgendwann nichts mehr findet, müssen die CSS-Selektoren unten
   (Konstanten mit "_SELECTOR") angepasst werden.
4. Der Name in MITBEWOHNER_NAME muss exakt so lauten, wie er im Gruppenchat
   als Absendername angezeigt wird (z.B. sein gespeicherter Kontaktname
   oder seine Telefonnummer, falls nicht gespeichert).

Installation:
    pip install selenium
    (Firefox muss installiert sein; den passenden "geckodriver" lädt
    Selenium ab Version 4.6 automatisch selbst herunter - keine manuelle
    Installation nötig.)

Einrichtung als täglicher Cron-Job (einmal am Tag reicht, z.B. 9 Uhr):
    crontab -e
    0 9 * * * /usr/bin/python3 /pfad/zu/internet_reminder.py >> /pfad/zu/reminder.log 2>&1
"""

import json
import re
import time
from datetime import date, datetime
from pathlib import Path

from selenium import webdriver
from selenium.webdriver.firefox.options import Options
from selenium.webdriver.firefox.service import Service
from selenium.webdriver.firefox.firefox_profile import FirefoxProfile
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

# ============================== KONFIGURATION ==============================

GROUP_NAME = "WG Gruppe"              # exakter Name des Gruppenchats
MITBEWOHNER_NAME = "Vandad"            # Anzeigename des Mitbewohners im Chat

POLL_DAY = 11                       # Tag im Monat, an dem gefragt wird
REMINDER_DAY = 20                   # Tag im Monat, an dem ggf. erinnert wird

POLL_MESSAGE = (
    f"Hallo {MITBEWOHNER_NAME}! 📅 Kurze Erinnerung: Hast du deinen Internet-Anteil für diesen "
    "Monat schon überwiesen? Bitte antworte kurz mit *Ja* oder *Nein*."
)
REMINDER_MESSAGE = (
    f"Hey {MITBEWOHNER_NAME}, kurzer Reminder ⏰ – laut Chat steht deine Antwort zum "
    "Internet-Anteil diesen Monat noch aus (oder war ein Nein). "
    "Kannst du das bitte zeitnah überweisen? Danke! 🙏"
)

# Firefox-Profil, in dem die WhatsApp-Web-Session gespeichert wird
FIREFOX_PROFILE_DIR = str(Path.home() / ".whatsapp_reminder_profile")

# Statusdatei, merkt sich pro Monat, was schon erledigt wurde
STATE_FILE = Path(__file__).parent / "reminder_state.json"

HEADLESS = True   # nach erfolgreichem ersten QR-Login auf True stellen

# =============================== SELEKTOREN =================================
# Diese CSS-Selektoren können sich ändern, wenn WhatsApp Web sein Layout
# aktualisiert. Falls das Skript nichts mehr findet, hier zuerst nachsehen
# (z.B. mit den Firefox-DevTools auf web.whatsapp.com prüfen).

SEARCH_BOX_SELECTOR = 'div[contenteditable="true"][data-tab="3"]'
MESSAGE_BOX_SELECTOR = 'div[contenteditable="true"][data-tab="10"]'
MESSAGE_BUBBLE_SELECTOR = 'div.copyable-text'

# ============================================================================


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def get_month_state(today: date) -> dict:
    """Lädt den Status für den aktuellen Monat, legt bei Bedarf einen neuen an."""
    state = load_state()
    month_key = today.strftime("%Y-%m")
    if state.get("month") != month_key:
        state = {
            "month": month_key,
            "poll_sent": False,
            "poll_sent_at": None,
            "reminder_sent": False,
            "response": None,  # "ja" / "nein" / None
        }
        save_state(state)
    return state


def start_driver() -> webdriver.Firefox:
    # Persistentes Profil verwenden (existiert es noch nicht, legt Firefox
    # beim ersten Start automatisch eins im angegebenen Ordner an).
    Path(FIREFOX_PROFILE_DIR).mkdir(parents=True, exist_ok=True)
    profile = FirefoxProfile(FIREFOX_PROFILE_DIR)

    options = Options()
    options.profile = profile
    options.add_argument("--width=1200")
    options.add_argument("--height=900")
    if HEADLESS:
        options.add_argument("-headless")
    driver = webdriver.Firefox(service=Service(), options=options)
    driver.get("https://web.whatsapp.com")
    return driver


def wait_for_login(driver, timeout=120) -> None:
    """Wartet, bis WhatsApp Web eingeloggt ist (Suchfeld sichtbar)."""
    WebDriverWait(driver, timeout).until(
        EC.presence_of_element_located((By.CSS_SELECTOR, SEARCH_BOX_SELECTOR))
    )


def open_chat(driver, chat_name: str) -> None:
    search_box = WebDriverWait(driver, 30).until(
        EC.presence_of_element_located((By.CSS_SELECTOR, SEARCH_BOX_SELECTOR))
    )
    search_box.click()
    search_box.send_keys(chat_name)
    time.sleep(2)

    result = WebDriverWait(driver, 15).until(
        EC.presence_of_element_located(
            (By.XPATH, f'//span[@title="{chat_name}"]')
        )
    )
    result.click()
    time.sleep(2)


def send_message(driver, chat_name: str, text: str) -> None:
    open_chat(driver, chat_name)
    msg_box = WebDriverWait(driver, 15).until(
        EC.presence_of_element_located((By.CSS_SELECTOR, MESSAGE_BOX_SELECTOR))
    )
    msg_box.click()
    for line in text.split("\n"):
        msg_box.send_keys(line)
        msg_box.send_keys(webdriver.common.keys.Keys.SHIFT, webdriver.common.keys.Keys.ENTER)
    msg_box.send_keys(webdriver.common.keys.Keys.ENTER)
    time.sleep(2)


def get_replies_from(driver, chat_name: str, sender_name: str, since: datetime) -> list[str]:
    """
    Liest alle Nachrichten des angegebenen Absenders im geöffneten Chat,
    die nach 'since' gesendet wurden, und gibt deren Text zurück.
    """
    open_chat(driver, chat_name)
    time.sleep(2)

    bubbles = driver.find_elements(By.CSS_SELECTOR, MESSAGE_BUBBLE_SELECTOR)
    replies = []

    for bubble in bubbles:
        pre_plain = bubble.get_attribute("data-pre-plain-text")
        if not pre_plain:
            continue
        # Format: "[10:03, 15.08.2026] Absendername: "
        match = re.match(r"\[(\d{1,2}:\d{2}), (\d{1,2}\.\d{1,2}\.\d{4})\] (.+): $", pre_plain)
        if not match:
            continue
        time_str, date_str, sender = match.groups()
        if sender.strip() != sender_name:
            continue
        try:
            msg_dt = datetime.strptime(f"{date_str} {time_str}", "%d.%m.%Y %H:%M")
        except ValueError:
            continue
        if msg_dt < since:
            continue

        text = bubble.text.strip()
        if text:
            replies.append(text)

    return replies


def classify_reply(text: str) -> str | None:
    t = text.strip().lower()
    if re.search(r"\bja\b", t):
        return "ja"
    if re.search(r"\bnein\b", t):
        return "nein"
    return None


def daily_check() -> None:
    today = date.today()
    state = get_month_state(today)

    # ">=" statt "==": falls der Rechner am 15. selbst nicht lief, wird die
    # Frage beim nächsten Lauf trotzdem noch nachgeholt (solange sie in
    # diesem Monat noch nicht gesendet wurde).
    needs_send = (today.day >= POLL_DAY and not state["poll_sent"])
    needs_check = state["poll_sent"] and not state.get("response") == "ja"
    needs_reminder = (
        today.day >= REMINDER_DAY
        and state["poll_sent"]
        and not state["reminder_sent"]
        and state.get("response") != "ja"
    )

    if not (needs_send or needs_check or needs_reminder):
        return  # heute gibt es nichts zu tun

    driver = start_driver()
    try:
        wait_for_login(driver)

        if needs_send:
            send_message(driver, GROUP_NAME, POLL_MESSAGE)
            state["poll_sent"] = True
            state["poll_sent_at"] = datetime.now().isoformat()
            save_state(state)
            print(f"[{today}] Frage gesendet.")

        if needs_check and state.get("poll_sent_at"):
            since = datetime.fromisoformat(state["poll_sent_at"])
            replies = get_replies_from(driver, GROUP_NAME, MITBEWOHNER_NAME, since)
            for reply in replies:
                classification = classify_reply(reply)
                if classification:
                    state["response"] = classification  # letzte eindeutige Antwort gewinnt
            save_state(state)
            if state.get("response"):
                print(f"[{today}] Antwort erkannt: {state['response']}")

        # Nach der Prüfung ggf. Reminder auslösen (auch wenn erst jetzt Ja/Nein bekannt wurde)
        if (
            today.day >= REMINDER_DAY
            and state["poll_sent"]
            and not state["reminder_sent"]
            and state.get("response") != "ja"
        ):
            send_message(driver, GROUP_NAME, REMINDER_MESSAGE)
            state["reminder_sent"] = True
            save_state(state)
            print(f"[{today}] Erinnerung gesendet.")

    finally:
        driver.quit()


if __name__ == "__main__":
    daily_check()