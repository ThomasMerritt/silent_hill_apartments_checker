import os
import re
import time
import gc
import smtplib
from email.mime.text import MIMEText
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
from dotenv import load_dotenv

# Absolute path for .env to ensure stability in background deployment
script_dir = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(script_dir, ".env"))

# --- CONFIGURATION ---
# High Priority: Checked every 5 minutes
HIGH_PRIORITY_URLS = [
    "https://riedman.com/floorplans/laurel-springs-apartments/1-bedroom-1-bath-598/",
    "https://riedman.com/floorplans/laurel-ridge-apartments/1-bedroom-1-bath-598-2/",
]

# Standard Priority: Checked every 30 minutes (every 6th cycle)
STANDARD_PRIORITY_URLS = [
    "https://riedman.com/floorplans/winchester-apartments-townhomes/1-bedroom-1-bath-545-2/",
    "https://riedman.com/floorplans/winchester-apartments-townhomes/1-bedroom-1-bath-640-2/",
    "https://riedman.com/floorplans/royal-villa-apartments/1-bedroom-1-bath-567/",
    "https://riedman.com/floorplans/alpine-village-apartments/1-bedroom-1-bath-545/",
    "https://riedman.com/floorplans/the-hammocks-at-fairview/1-bedroom-1-bath-w-finished-walkout-basement-garage-1669/",
    "https://riedman.com/floorplans/the-hammocks-at-fairview/1-bedroom-1-bath-with-garage-1008/",
    "https://riedman.com/floorplans/alpine-village-apartments/1-bedroom-1-bath-640/",
]

EMAIL_SENDER = os.getenv("EMAIL_SENDER")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD")
EMAIL_RECIPIENT = os.getenv("EMAIL_RECIPIENT")

# Universal path logic for Mac (/tmp) vs Pi (/dev/shm)
if os.path.exists("/dev/shm"):
    OUTPUT_DIR = "/dev/shm/apartment_outputs"
else:
    OUTPUT_DIR = "/tmp/apartment_outputs"

TARGET_DATE = "6/1"
INTERVAL_SECONDS = 3600
sent_notifications = set()

def get_pi_temp():
    """Reads hardware temp on Linux/Pi; returns 0 on Mac."""
    if os.path.exists("/sys/class/thermal/thermal_zone0/temp"):
        try:
            with open("/sys/class/thermal/thermal_zone0/temp", "r") as f:
                return int(f.read()) / 1000
        except:
            return 0
    return 0

def refresh_html_cache(url_list):
    if not url_list: return
    if not os.path.exists(OUTPUT_DIR): os.makedirs(OUTPUT_DIR)

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--disable-gpu", "--disable-dev-shm-usage", "--no-sandbox",
                "--disable-setuid-sandbox", "--no-first-run", "--no-zygote",
                "--single-process", "--disable-extensions",
                "--js-flags='--max-old-space-size=512'"
            ]
        )
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        )
        page = context.new_page()

        # Domain-level blocking to save Pi CPU cycles
        def intercept_route(route):
            if "riedman.com" not in route.request.url:
                route.abort()
            elif route.request.resource_type in ["image", "media", "font", "stylesheet"]:
                route.abort()
            else:
                route.continue_()

        page.route("**/*", intercept_route)

        print(f"[{datetime.now().strftime('%H:%M:%S')}] Scraping {len(url_list)} URLs...")
        
        for url in url_list:
            url_parts = url.strip('/').split('/')
            unique_name = "-".join(url_parts[-2:])
            output_file = os.path.join(OUTPUT_DIR, f"{unique_name}.html")

            try:
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                try:
                    page.wait_for_selector(".rentpress-shortcode-single-floorplan-information-wrapper", timeout=10000)
                except:
                    pass

                element = page.query_selector(".rentpress-shortcode-single-floorplan-information-wrapper")
                if element:
                    html_content = element.inner_html()
                    with open(output_file, "w", encoding="utf-8") as f:
                        f.write(html_content)
                
                time.sleep(2) # Thermal breather
            except Exception as e:
                print(f"      ! Error scraping {unique_name}: {e}")
        
        browser.close()
        gc.collect() # Force RAM release

def send_notification_email(new_matches, target_date):
    if not new_matches: return
    recipient_list = [email.strip() for email in EMAIL_RECIPIENT.split(",")]
    subject = f"NEW Apartment Alert: {len(new_matches)} Units Found for {target_date}"
    rows = "".join([f"<tr><td>{m['property']}</td><td>{m['unit']}</td><td>{m['price']}</td><td>{m['size']}</td><td>{m['available']}</td><td><a href='{m['url']}'>View</a></td></tr>" for m in new_matches])
    body = f"<html><body><h2>New Units Found for {target_date}</h2><table border='1' cellpadding='6' cellspacing='0'><tr><th>Property</th><th>Unit</th><th>Price</th><th>Size</th><th>Available</th><th>Link</th></tr>{rows}</table></body></html>"

    msg = MIMEText(body, "html")
    msg['Subject'] = subject
    msg['From'] = EMAIL_SENDER
    msg['To'] = ", ".join(recipient_list)

    try:
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
            server.login(EMAIL_SENDER, EMAIL_PASSWORD)
            server.sendmail(EMAIL_SENDER, recipient_list, msg.as_string())
        print(f"      >>> SUCCESS: Email alert sent.")
    except Exception as e:
        print(f"      ! Email failed: {e}")

def run_apartment_scanner(target_date_str, current_batch):
    global sent_notifications
    current_date = datetime.now()
    target_dt = datetime.strptime(f"{target_date_str}/{current_date.year}", "%m/%d/%Y")
    notification_start_date = target_dt - timedelta(days=21)
    is_in_urgent_window = current_date >= notification_start_date

    new_matches_to_email = []

    for url in current_batch:
        url_parts = url.strip('/').split('/')
        unique_name = "-".join(url_parts[-2:])
        file_path = os.path.join(OUTPUT_DIR, f"{unique_name}.html")

        if not os.path.exists(file_path): continue

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                soup = BeautifulSoup(f.read(), 'html.parser')

            no_avail = soup.find("h4", class_="rentpress-no-units-headline")
            if no_avail and "No Apartments Available" in no_avail.get_text():
                continue

            unit_containers = soup.find_all("div", class_=re.compile(r'rentpress-shortcode-unit-card'))
            
            for unit in unit_containers:
                unit_text = unit.get_text(separator=" | ", strip=True)
                parts = [p.strip() for p in unit_text.split("|")]
                unit_id = parts[0]
                price = (parts[1] if len(parts) > 1 else "N/A")
                size = (parts[3] if len(parts) > 3 else "N/A")
                
                show_unit, available_label = False, ""
                date_pattern = re.search(r'Available On (\d{1,2}/\d{1,2}/\d{4})', unit_text)
                
                if date_pattern:
                    if datetime.strptime(date_pattern.group(1), "%m/%d/%Y") == target_dt:
                        show_unit, available_label = True, f"Available On {date_pattern.group(1)}"
                elif "Available Now" in unit_text and is_in_urgent_window:
                    show_unit, available_label = True, "Available Now"

                if show_unit:
                    notification_key = f"{unique_name}-{unit_id}"
                    if notification_key not in sent_notifications:
                        print(f"      [NEW MATCH] {unique_name} Unit {unit_id}")
                        new_matches_to_email.append({
                            "property": unique_name, "unit": unit_id, "price": price, 
                            "size": size, "available": available_label, "url": url
                        })
                        sent_notifications.add(notification_key)
        except Exception as e:
            print(f"      ! Error parsing {unique_name}: {e}")

    if new_matches_to_email:
        send_notification_email(new_matches_to_email, target_date_str)

if __name__ == "__main__":
    print(f"Starting Tiered Apartment Monitor (Target: {TARGET_DATE}).")
    cycle_count = 0
    
    while True:
        try:
            temp = get_pi_temp()
            if temp > 75:
                print(f"!!! Thermal Guard: {temp}°C. Cooling down...")
                time.sleep(180)
                continue

            # Batching logic: High priority every cycle, Standard every 6th
            current_batch = HIGH_PRIORITY_URLS.copy()
            if cycle_count % 6 == 0:
                print(">>> Including Standard Priority URLs in this cycle.")
                current_batch.extend(STANDARD_PRIORITY_URLS)

            refresh_html_cache(current_batch)
            run_apartment_scanner(TARGET_DATE, current_batch)
            
            cycle_count += 1
            print(f"Cycle {cycle_count} complete (Temp: {get_pi_temp()}°C). Sleeping {INTERVAL_SECONDS} seconds...")
            time.sleep(INTERVAL_SECONDS)
            
        except KeyboardInterrupt:
            print("\nMonitor stopped.")
            break
        except Exception as e:
            print(f"\nCRITICAL ERROR: {e}")
            time.sleep(60)