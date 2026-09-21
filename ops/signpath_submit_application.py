from __future__ import annotations

import json
import time
from pathlib import Path

from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait


PROJECT = {
    "2-198696014/name": "Notanda",
    "2-198696014/repository_url": "https://github.com/RudiRegenwurm/Open-Access-API-Harvester",
    "2-198696014/homepage_url": "https://notanda.io",
    "2-198696014/download_url": "https://github.com/RudiRegenwurm/Open-Access-API-Harvester/releases/tag/v1.3.0-installer-preview.1",
    "2-198696014/privacy_policy_url": "https://notanda.io/privacy.html",
    "2-198696014/wikipedia_url": "",
    "2-198696014/tagline": (
        "Local open-source application for reproducible discovery, acquisition, "
        "verification, and preservation of open-access scientific literature."
    ),
    "2-198696014/description": (
        "Notanda is a local open-source application that builds reproducible scientific "
        "evidence corpora from open-access literature. It discovers publications through "
        "scholarly metadata providers, verifies and acquires legitimate open-access full "
        "text, records provenance and integrity information, and keeps the resulting corpus "
        "under the user's control."
    ),
    "2-198696014/reputation": (
        "Notanda is a new open-source project and does not yet have a large public user base. "
        "The public repository contains a reproducible CI/test history; the standalone "
        "installer lifecycle has been validated on Windows, macOS and Ubuntu; and the first "
        "public Installer Preview with exact SHA-256 provenance is available at the download "
        "URL above. Code signing is being requested before recommending the Windows installer "
        "to non-technical beta users."
    ),
    "0-1/firstname": "Rudolf",
    "0-1/lastname": "Kiechle",
    "0-1/email": "beta@notanda.io",
    "0-1/company": "",
    "2-198696014/discovery_source": "ChatGPT project work on Notanda installer signing",
}

CUSTOM = {
    "2-198696014/maintainer": "Individual maintainer(s)",
    "2-198696014/build_system": "GitHub Actions",
    "2-198696014/discovery_channel": "AI / LLM tools",
}

REQUIRED_CONSENTS = [
    "LEGAL_CONSENT.subscription_type_2099806800",
    "LEGAL_CONSENT.processing",
]

OPTIONAL_MARKETING = "LEGAL_CONSENT.subscription_type_408277728"


def set_field(driver, name: str, value: str) -> None:
    el = driver.find_element(By.NAME, name)
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
    el.clear()
    if value:
        el.send_keys(value)


def select_custom(driver, hidden_name: str, label: str) -> None:
    hidden = driver.find_element(By.CSS_SELECTOR, f'input[type="hidden"][name="{hidden_name}"]')
    container = hidden.find_element(
        By.XPATH,
        "./ancestor::div[.//input[not(@name) and @type='text']][1]",
    )
    visible = container.find_element(By.CSS_SELECTOR, "input[type='text']:not([name])")
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", visible)
    visible.click()

    option = WebDriverWait(driver, 10).until(
        lambda d: next(
            (
                el
                for el in d.find_elements(By.CSS_SELECTOR, "[role='option']")
                if el.is_displayed() and (el.text or "").strip() == label
            ),
            None,
        )
    )
    option.click()

    WebDriverWait(driver, 10).until(
        lambda d: (d.find_element(By.CSS_SELECTOR, f'input[type="hidden"][name="{hidden_name}"]').get_attribute("value") or "").strip()
    )


opts = Options()
opts.add_argument("--headless=new")
opts.add_argument("--no-sandbox")
opts.add_argument("--disable-dev-shm-usage")
opts.add_argument("--window-size=1600,2200")
opts.set_capability("goog:loggingPrefs", {"performance": "ALL"})

driver = webdriver.Chrome(options=opts)
result: dict[str, object] = {
    "submitted": False,
    "project": "Notanda",
    "release_url": PROJECT["2-198696014/download_url"],
    "account_email": PROJECT["0-1/email"],
    "marketing_consent": False,
}
try:
    driver.get("https://signpath.org/apply.html")
    WebDriverWait(driver, 30).until(lambda d: len(d.find_elements(By.TAG_NAME, "iframe")) >= 1)
    time.sleep(4)

    frame = driver.find_elements(By.TAG_NAME, "iframe")[0]
    driver.switch_to.frame(frame)

    WebDriverWait(driver, 20).until(
        lambda d: d.find_elements(By.NAME, "2-198696014/name")
    )

    for name, value in PROJECT.items():
        set_field(driver, name, value)

    for hidden_name, label in CUSTOM.items():
        select_custom(driver, hidden_name, label)

    for name in REQUIRED_CONSENTS:
        cb = driver.find_element(By.NAME, name)
        if not cb.is_selected():
            driver.execute_script("arguments[0].click();", cb)
        if not cb.is_selected():
            raise RuntimeError(f"Required consent did not become selected: {name}")

    marketing = driver.find_element(By.NAME, OPTIONAL_MARKETING)
    if marketing.is_selected():
        driver.execute_script("arguments[0].click();", marketing)
    if marketing.is_selected():
        raise RuntimeError("Optional marketing consent unexpectedly remained selected")

    # Verify custom values and required fields before the irreversible submit click.
    result["selected"] = {
        name: driver.find_element(By.CSS_SELECTOR, f'input[type="hidden"][name="{name}"]').get_attribute("value")
        for name in CUSTOM
    }
    result["required_consents"] = {
        name: driver.find_element(By.NAME, name).is_selected()
        for name in REQUIRED_CONSENTS
    }

    submit = driver.find_element(By.CSS_SELECTOR, "button[type='submit']")
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", submit)
    submit.click()

    # Let HubSpot's invisible reCAPTCHA and network submission complete.
    time.sleep(12)

    body_text = (driver.find_element(By.TAG_NAME, "body").text or "").strip()
    result["post_submit_body_excerpt"] = body_text[:6000]

    # Record only endpoint/status, never cookies, request bodies, or CAPTCHA tokens.
    submission_responses = []
    for log in driver.get_log("performance"):
        try:
            msg = json.loads(log["message"])["message"]
            if msg.get("method") != "Network.responseReceived":
                continue
            response = msg["params"]["response"]
            url = response.get("url", "")
            if "hsforms" in url and (
                "submit" in url.lower() or "submission" in url.lower()
            ):
                submission_responses.append({
                    "url": url.split("?")[0],
                    "status": response.get("status"),
                    "mimeType": response.get("mimeType"),
                })
        except Exception:
            pass
    result["submission_responses"] = submission_responses

    success_text = body_text.lower()
    text_success = any(
        token in success_text
        for token in (
            "thank you",
            "thanks for",
            "application has been",
            "submission has been",
            "we'll be in touch",
            "we will be in touch",
        )
    )
    network_success = any(
        int(r.get("status") or 0) in (200, 201, 202, 204)
        for r in submission_responses
    )

    # HubSpot often replaces the form with a thank-you state after a successful submit.
    form_still_visible = bool(driver.find_elements(By.NAME, "2-198696014/name"))
    result["form_still_visible"] = form_still_visible
    result["text_success"] = text_success
    result["network_success"] = network_success
    result["submitted"] = bool(network_success and (text_success or not form_still_visible))

    if result["submitted"]:
        driver.save_screenshot("signpath-application-confirmation.png")
    else:
        # Capture validation errors without exposing the filled values.
        errors = []
        for el in driver.find_elements(By.CSS_SELECTOR, "[role='alert'], .hs-error-msg, [data-error]"):
            txt = (el.text or "").strip()
            if txt:
                errors.append(txt)
        result["errors"] = sorted(set(errors))
        raise RuntimeError(
            "SignPath application did not produce a verifiable successful submission state"
        )
finally:
    driver.switch_to.default_content()
    Path("signpath-application-result.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    # Print a redacted evidence summary only.
    print(json.dumps({
        "submitted": result.get("submitted"),
        "project": result.get("project"),
        "release_url": result.get("release_url"),
        "marketing_consent": result.get("marketing_consent"),
        "selected": result.get("selected"),
        "required_consents": result.get("required_consents"),
        "submission_responses": result.get("submission_responses"),
        "form_still_visible": result.get("form_still_visible"),
        "text_success": result.get("text_success"),
        "network_success": result.get("network_success"),
        "errors": result.get("errors"),
    }, indent=2, ensure_ascii=False))
    driver.quit()
