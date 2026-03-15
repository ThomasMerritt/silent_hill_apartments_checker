import os
import re
import time
import smtplib
from email.mime.text import MIMEText
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
from dotenv import load_dotenv 

# Load variables from .env
load_dotenv()

# --- CONFIGURATION ---
URLS = [
    "https://riedman.com/floorplans/laurel-springs-apartments/1-bedroom-1-bath-598/",
    "https://riedman.com/floorplans/laurel-ridge-apartments/1-bedroom-1-bath-598-2/",
    "https://riedman.com/floorplans/winchester-apartments-townhomes/1-bedroom-1-bath-545-2/",
    "https://riedman.com/floorplans/winchester-apartments-townhomes/1-bedroom-1-bath-640-2/",
    "https://riedman.com/floorplans/royal-villa-apartments/1-bedroom-1-bath-567/",
    "https://riedman.com/floorplans/alpine-village-apartments/1-bedroom-1-bath-640/",
    "https://riedman.com/floorplans/alpine-village-apartments/1-bedroom-1-bath-545/",
    "https://riedman.com/floorplans/the-hammocks-at-fairview/1-bedroom-1-bath-w-finished-walkout-basement-garage-1669/",
    "https://riedman.com/floorplans/the-hammocks-at-fairview/1-bedroom-1-bath-with-garage-1008/",
    "https://riedman.com/floorplans/laurel-springs-apartments/studio-apartment-378/"
]

EMAIL_SENDER = os.getenv("EMAIL_SENDER")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD")
EMAIL_RECIPIENT = os.getenv("EMAIL_RECIPIENT")
OUTPUT_DIR = "outputs"
TARGET_DATE = "6/1"
INTERVAL_SECONDS = 300

# This set lives outside the loop to remember what we've already sent
sent_notifications = set()

def refresh_html_cache():
    if not os.path.exists(OUTPUT_DIR):
        os.makedirs(OUTPUT_DIR)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        print(f"[{datetime.now().strftime('%H:%M:%S')}] Scraping {len(URLS)} URLs...")
        for url in URLS:
            url_parts = url.strip('/').split('/')
            unique_name = "-".join(url_parts[-2:])
            output_file = os.path.join(OUTPUT_DIR, f"{unique_name}.html")

            try:
                page.goto(url, wait_until="networkidle", timeout=60000)
                html_content = page.content()
                with open(output_file, "w", encoding="utf-8") as f:
                    f.write(html_content)
            except Exception as e:
                print(f"      ! Error scraping {unique_name}: {e}")
        
        browser.close()

def send_notification_email(new_matches, target_date):
    if not new_matches:
        return

    # Split the env string into a list of individual emails
    # This handles both a single email or a comma-separated list
    recipient_list = [email.strip() for email in EMAIL_RECIPIENT.split(",")]

    subject = f"NEW Apartment Alert: {len(new_matches)} Units Found for {target_date}"
    rows = "".join([f"<tr><td>{m['property']}</td><td>{m['unit']}</td><td>{m['price']}</td><td>{m['size']}</td><td>{m['available']}</td><td><a href='{m['url']}'>View</a></td></tr>" for m in new_matches])
    body = f"<html><body><h2>New Units Found for {target_date}</h2><table border='1' cellpadding='6' cellspacing='0'><tr><th>Property</th><th>Unit</th><th>Price</th><th>Size</th><th>Available</th><th>Link</th></tr>{rows}</table></body></html>"

    msg = MIMEText(body, "html")
    msg['Subject'] = subject
    msg['From'] = EMAIL_SENDER
    # The 'To' header should be a comma-separated string
    msg['To'] = ", ".join(recipient_list)

    try:
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
            server.login(EMAIL_SENDER, EMAIL_PASSWORD)
            # sendmail requires a LIST of addresses for the second argument
            server.sendmail(EMAIL_SENDER, recipient_list, msg.as_string())
        print(f"      >>> NEW units found! Email sent to: {msg['To']}")
    except Exception as e:
        print(f"      ! Email failed: {e}")

def run_apartment_scanner(target_date_str):
    global sent_notifications
    current_date = datetime.now()
    target_dt = datetime.strptime(f"{target_date_str}/{current_date.year}", "%m/%d/%Y")
    notification_start_date = target_dt - timedelta(days=21)
    is_in_urgent_window = current_date >= notification_start_date

    new_matches_to_email = []

    for url in URLS:
        url_parts = url.strip('/').split('/')
        unique_name = "-".join(url_parts[-2:])
        file_path = os.path.join(OUTPUT_DIR, f"{unique_name}.html")

        if not os.path.exists(file_path): continue

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                soup = BeautifulSoup(f.read(), 'html.parser')

            unit_containers = soup.find_all("div", class_=re.compile(r'rentpress-shortcode-unit-card'))
            
            for unit in unit_containers:
                unit_text = unit.get_text(separator=" | ", strip=True)
                parts = [p.strip() for p in unit_text.split("|")]
                
                unit_id = parts[0]
                price = (parts[1] if len(parts) > 1 else "N/A")
                size = (parts[3] if len(parts) > 3 else "N/A")
                
                show_unit, available_label = False, ""

                # Date Match Logic
                date_pattern = re.search(r'Available On (\d{1,2}/\d{1,2}/\d{4})', unit_text)
                if date_pattern:
                    if datetime.strptime(date_pattern.group(1), "%m/%d/%Y") == target_dt:
                        show_unit, available_label = True, f"Available On {date_pattern.group(1)}"
                elif "Available Now" in unit_text and is_in_urgent_window:
                    show_unit, available_label = True, "Available Now"

                if show_unit:
                    # Create a unique key for this specific unit
                    notification_key = f"{unique_name}-{unit_id}"
                    
                    if notification_key not in sent_notifications:
                        print(f"      [NEW MATCH] {unique_name} Unit {unit_id}")
                        new_matches_to_email.append({
                            "property": unique_name, "unit": unit_id, "price": price, 
                            "size": size, "available": available_label, "url": url
                        })
                        # Add to the "already sent" set
                        sent_notifications.add(notification_key)
                    else:
                        # Optional: uncomment for verbose debugging
                        # print(f"      [Skipping] Unit {unit_id} (Already notified)")
                        pass

        except Exception as e:
            print(f"      ! Error parsing {unique_name}: {e}")

    if new_matches_to_email:
        send_notification_email(new_matches_to_email, target_date_str)
    else:
        print("      No NEW units found since last check.")

# --- THE PERPETUAL TIMER ---
if __name__ == "__main__":
    print(f"Starting Apartment Monitor (Target: {TARGET_DATE}). Press Ctrl+C to stop.")
    
    while True:
        try:
            refresh_html_cache()
            run_apartment_scanner(TARGET_DATE)
            
            print(f"Cycle complete. Sleeping for 5 minutes...")
            time.sleep(INTERVAL_SECONDS)
            
        except KeyboardInterrupt:
            print("\nMonitor stopped by user.")
            break
        except Exception as e:
            print(f"\nCRITICAL ERROR in main loop: {e}")
            print("Retrying in 60 seconds...")
            time.sleep(60)

            # 1. Add the remote using 'origin' as the name

