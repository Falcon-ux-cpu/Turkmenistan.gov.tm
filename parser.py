import os
import re
import json
import shutil
import requests
from bs4 import BeautifulSoup
from PIL import Image
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders

BASE_URL = "https://turkmenistan.gov.tm"

CATEGORIES = [
    {
        "url": "https://turkmenistan.gov.tm/tk/bolum/wakalaryn-jummusinde",
        "subject_prefix": "Türkmenistan: Wakalaryň jümmüşinde"
    },
    {
        "url": "https://turkmenistan.gov.tm/tk/bolum/resmi",
        "subject_prefix": "Türkmenistan: Resmi"
    },
    {
        "url": "https://turkmenistan.gov.tm/tk/bolum/kanunlar",
        "subject_prefix": "Türkmenistan: Kanunlar"
    }
]

SENT_IDS_FILE = "sent_ids.json"
TEMP_DIR = "temp_files"

def load_sent_ids():
    if os.path.exists(SENT_IDS_FILE):
        with open(SENT_IDS_FILE, "r", encoding="utf-8") as f:
            try:
                return set(json.load(f))
            except json.JSONDecodeError:
                return set()
    return set()

def save_sent_ids(sent_ids):
    with open(SENT_IDS_FILE, "w", encoding="utf-8") as f:
        json.dump(list(sent_ids), f, ensure_ascii=False, indent=2)

def clean_temp_dir():
    if os.path.exists(TEMP_DIR):
        shutil.rmtree(TEMP_DIR)
    os.makedirs(TEMP_DIR, exist_ok=True)

def download_file(url, local_filename):
    response = requests.get(url, stream=True, timeout=30)
    response.raise_for_status()
    with open(local_filename, 'wb') as f:
        for chunk in response.iter_content(chunk_size=8192):
            f.write(chunk)
    return local_filename

def optimize_image(image_path, max_width=800):
    try:
        with Image.open(image_path) as img:
            if img.mode in ("RGBA", "P"):
                img = img.convert("RGB")
            
            width, height = img.size
            if width > max_width:
                new_height = int((max_width / width) * height)
                img = img.resize((max_width, new_height), Image.Resampling.LANCZOS)
            
            img.save(image_path, "JPEG", quality=85, optimize=True)
    except Exception as e:
        print(f"Ошибка при сжатии изображения {image_path}: {e}")

def get_articles_from_category(category_url):
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    res = requests.get(category_url, headers=headers, timeout=20)
    res.raise_for_status()
    
    soup = BeautifulSoup(res.text, "html.parser")
    articles = []
    
    # Поиск ссылок вида /tk/habar/12345/slug
    for a in soup.find_all("a", href=True):
        href = a['href']
        match = re.search(r'/tk/habar/(\d+)', href)
        if match:
            post_id = match.group(1)
            full_url = href if href.startswith("http") else BASE_URL + href
            if not any(art['id'] == post_id for art in articles):
                articles.append({'id': post_id, 'url': full_url})
                
    return articles

def parse_and_process_article(article_url):
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    res = requests.get(article_url, headers=headers, timeout=20)
    res.raise_for_status()
    
    soup = BeautifulSoup(res.text, "html.parser")
    
    title_el = soup.select_one("h1.post_title")
    title = title_el.get_text(strip=True) if title_el else "Без заголовка"
    
    date_el = soup.select_one("time.post_date")
    date = date_el.get_text(strip=True) if date_el else ""
    
    content_el = soup.select_one("div.post_text") or soup.select_one("div.post_content")
    if not content_el:
        return None, [], title, date

    downloaded_attachments = []
    file_counter = 0

    # Обработка тегов <img>
    for img in content_el.find_all("img"):
        src = img.get("src")
        if not src:
            continue
        
        img_url = src if src.startswith("http") else BASE_URL + src
        img["style"] = "max-width: 100%; height: auto; display: block; margin: 10px 0;"
        img["src"] = img_url
        
        file_counter += 1
        ext = os.path.splitext(img_url.split('?')[0])[1] or ".jpg"
        local_file = os.path.join(TEMP_DIR, f"image_{file_counter}{ext}")
        
        try:
            download_file(img_url, local_file)
            optimize_image(local_file)
            downloaded_attachments.append(local_file)
        except Exception as e:
            print(f"Не удалось скачать картинку {img_url}: {e}")

    # Обработка внешних вложений (PDF, DOCX и т.д.)
    for a in content_el.find_all("a", href=True):
        href = a['href']
        file_url = href if href.startswith("http") else BASE_URL + href
        a['href'] = file_url
        
        if re.search(r'\.(pdf|docx?|xlsx?|zip|rar)$', href, re.IGNORECASE):
            file_counter += 1
            filename = os.path.basename(href.split('?')[0])
            local_file = os.path.join(TEMP_DIR, f"{file_counter}_{filename}")
            try:
                download_file(file_url, local_file)
                downloaded_attachments.append(local_file)
            except Exception as e:
                print(f"Не удалось скачать файл {file_url}: {e}")

    html_content = f"""
    <html>
    <head>
        <style>
            body {{ font-family: Arial, sans-serif; line-height: 1.6; color: #333; }}
            h1 {{ color: #1a5276; }}
            .date {{ color: #7f8c8d; font-size: 0.9em; margin-bottom: 20px; }}
            img {{ max-width: 100% !important; height: auto !important; }}
        </style>
    </head>
    <body>
        <h1>{title}</h1>
        <div class="date">{date}</div>
        <hr>
        <div>{str(content_el)}</div>
        <br><hr>
        <p><small>Источник: <a href="{article_url}">{article_url}</a></small></p>
    </body>
    </html>
    """
    
    return html_content, downloaded_attachments, title, date

def send_email(subject, html_body, attachments):
    smtp_server = os.environ.get("SMTP_SERVER")
    smtp_port = int(os.environ.get("SMTP_PORT", 587))
    smtp_user = os.environ.get("EMAIL_USER")
    smtp_pass = os.environ.get("EMAIL_PASSWORD")
    to_email = os.environ.get("TO_EMAIL", smtp_user)

    msg = MIMEMultipart()
    msg['From'] = smtp_user
    msg['To'] = to_email
    msg['Subject'] = subject

    msg.attach(MIMEText(html_body, 'html', 'utf-8'))

    for file_path in attachments:
        if not os.path.exists(file_path):
            continue
        filename = os.path.basename(file_path)
        with open(file_path, "rb") as attachment:
            part = MIMEBase("application", "octet-stream")
            part.set_payload(attachment.read())
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", f"attachment; filename=\"{filename}\"")
        msg.attach(part)

    with smtplib.SMTP(smtp_server, smtp_port) as server:
        server.starttls()
        server.login(smtp_user, smtp_pass)
        server.send_message(msg)

def main():
    sent_ids = load_sent_ids()
    new_ids_found = False

    for cat in CATEGORIES:
        print(f"Проверка категории: {cat['url']}")
        try:
            articles = get_articles_from_category(cat['url'])
        except Exception as e:
            print(f"Ошибка при получении списка статей {cat['url']}: {e}")
            continue

        for art in articles:
            post_id = art['id']
            if post_id in sent_ids:
                continue

            print(f"Найдена новая статья ID: {post_id}")
            clean_temp_dir()

            try:
                html, attachments, title, date = parse_and_process_article(art['url'])
                if not html:
                    continue

                subject = f"{cat['subject_prefix']}: {title}"
                send_email(subject, html, attachments)
                print(f"Успешно отправлено письмо: {title}")

                sent_ids.add(post_id)
                new_ids_found = True

            except Exception as e:
                print(f"Ошибка при обработке статьи {art['url']}: {e}")
            finally:
                clean_temp_dir()

    if new_ids_found:
        save_sent_ids(sent_ids)
        print("Список отправленных ID обновлен.")

if __name__ == "__main__":
    main()
