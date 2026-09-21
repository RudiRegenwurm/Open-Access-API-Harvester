from __future__ import annotations

import json
import time
from pathlib import Path

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options


def collect_controls(driver, scope: str) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for css in ("input", "textarea", "select", "button"):
        for el in driver.find_elements(By.CSS_SELECTOR, css):
            try:
                item = {
                    "scope": scope,
                    "tag": el.tag_name,
                    "type": el.get_attribute("type") or "",
                    "name": el.get_attribute("name") or "",
                    "id": el.get_attribute("id") or "",
                    "placeholder": el.get_attribute("placeholder") or "",
                    "aria_label": el.get_attribute("aria-label") or "",
                    "required": el.get_attribute("required") or "",
                    "value": el.get_attribute("value") or "",
                    "text": (el.text or "").strip(),
                }
                out.append(item)
            except Exception as exc:
                out.append({"scope": scope, "error": repr(exc)})
    return out


opts = Options()
opts.add_argument("--headless=new")
opts.add_argument("--no-sandbox")
opts.add_argument("--disable-dev-shm-usage")
opts.add_argument("--window-size=1600,2000")
opts.set_capability("goog:loggingPrefs", {"performance": "ALL"})

driver = webdriver.Chrome(options=opts)
try:
    driver.get("https://signpath.org/apply.html")
    time.sleep(10)

    result = {
        "url": driver.current_url,
        "title": driver.title,
        "controls": collect_controls(driver, "top"),
        "frames": [],
    }

    frames = driver.find_elements(By.TAG_NAME, "iframe")
    for idx, frame in enumerate(frames):
        entry = {
            "index": idx,
            "src": frame.get_attribute("src") or "",
            "title": frame.get_attribute("title") or "",
            "controls": [],
        }
        try:
            driver.switch_to.frame(frame)
            time.sleep(1)
            entry["controls"] = collect_controls(driver, f"frame:{idx}")
            entry["body_text"] = driver.find_element(By.TAG_NAME, "body").text[:20000]
        except Exception as exc:
            entry["error"] = repr(exc)
        finally:
            driver.switch_to.default_content()
        result["frames"].append(entry)

    # Inspect custom combobox options used by HubSpot for maintainer type,
    # build system, and discovery channel. The visible inputs intentionally have
    # no name; HubSpot stores the selected values in adjacent hidden named inputs.
    driver.switch_to.frame(frames[0])
    select_candidates = []
    named_hidden = {
        "2-198696014/maintainer",
        "2-198696014/build_system",
        "2-198696014/discovery_channel",
    }
    for hidden_name in named_hidden:
        hidden = driver.find_element(By.CSS_SELECTOR, f'input[type="hidden"][name="{hidden_name}"]')
        container = hidden.find_element(By.XPATH, "./ancestor::div[.//input[not(@name) and @type='text']][1]")
        visible = container.find_element(By.CSS_SELECTOR, "input[type='text']:not([name])")
        entry = {
            "hidden_name": hidden_name,
            "hidden_value": hidden.get_attribute("value") or "",
            "visible_id": visible.get_attribute("id") or "",
            "options": [],
        }
        try:
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", visible)
            visible.click()
            time.sleep(0.5)
            options = []
            for opt in driver.find_elements(By.CSS_SELECTOR, "[role='option']"):
                if opt.is_displayed():
                    txt = (opt.text or "").strip()
                    if txt:
                        options.append({
                            "text": txt,
                            "data_value": opt.get_attribute("data-value") or "",
                            "id": opt.get_attribute("id") or "",
                        })
            entry["options"] = options
            driver.execute_script("arguments[0].blur();", visible)
        except Exception as exc:
            entry["error"] = repr(exc)
        select_candidates.append(entry)
    driver.switch_to.default_content()
    result["select_candidates"] = select_candidates

    # Capture HubSpot-related network URLs seen by the real browser; no cookies/tokens are printed.
    seen = []
    for log in driver.get_log("performance"):
        try:
            msg = json.loads(log["message"])["message"]
            if msg.get("method") != "Network.responseReceived":
                continue
            url = msg["params"]["response"]["url"]
            if "hsforms" in url or "hubspot" in url:
                seen.append({
                    "url": url,
                    "status": msg["params"]["response"].get("status"),
                    "mimeType": msg["params"]["response"].get("mimeType"),
                })
        except Exception:
            pass
    result["hubspot_network"] = seen

    Path("signpath-form-controls.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))
finally:
    driver.quit()
