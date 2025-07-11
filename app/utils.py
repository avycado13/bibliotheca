from datetime import date, timedelta, datetime
import pytz
from .models import ReadingLog
from sqlalchemy import func
import calendar
from PIL import Image, ImageDraw, ImageFont
from io import BytesIO
import requests
import os
import time
from flask import current_app

# Rate limiting configuration
# Adjust these values based on API provider limits and your needs
API_RATE_LIMIT_DELAY = 0.5  # seconds between API calls (0.5 second for faster bulk imports)
MAX_RETRIES = 3             # maximum number of retry attempts
RETRY_DELAY = 2.0           # base seconds to wait before retrying (exponential backoff)

# Note: OpenLibrary has no official rate limits but recommends being respectful
# Google Books API has a limit of 1000 requests per day for free tier
# Increase delays if you experience frequent API throttling

def rate_limited_request(url, timeout=5, max_retries=MAX_RETRIES, params=None):
    """
    Make a rate-limited HTTP request with retry logic
    """
    for attempt in range(max_retries):
        try:
            # Add delay to respect rate limits
            time.sleep(API_RATE_LIMIT_DELAY)
            
            response = requests.get(url, timeout=timeout, params=params)
            response.raise_for_status()  # Raise an exception for bad status codes
            return response
        except requests.exceptions.RequestException as e:
            current_app.logger.warning(f"API request attempt {attempt + 1} failed for {url}: {e}")
            if attempt < max_retries - 1:
                time.sleep(RETRY_DELAY * (attempt + 1))  # Exponential backoff
            else:
                current_app.logger.error(f"API request failed after {max_retries} attempts for {url}")
                raise
    return None

def fetch_book_data(isbn):
    url = f"https://openlibrary.org/api/books?bibkeys=ISBN:{isbn}&format=json&jscmd=data"
    try:
        response = rate_limited_request(url)
        data = response.json()
    except Exception as e:
        current_app.logger.error(f"Failed to fetch book data for ISBN {isbn}: {e}")
        return None
    book_key = f"ISBN:{isbn}"
    if book_key in data:
        book = data[book_key]
        title = book.get('title', '')
        authors = ', '.join([a['name'] for a in book.get('authors', [])])
        cover_url = book.get('cover', {}).get('large') or book.get('cover', {}).get('medium') or book.get('cover', {}).get('small')
        
        # Extract additional metadata
        description = book.get('notes', {}).get('value') if isinstance(book.get('notes'), dict) else book.get('notes')
        published_date = book.get('publish_date', '')
        page_count = book.get('number_of_pages')
        subjects = book.get('subjects', [])
        categories = ', '.join([s['name'] if isinstance(s, dict) else str(s) for s in subjects[:5]])  # Limit to 5 categories
        publishers = book.get('publishers', [])
        publisher = publishers[0]['name'] if publishers and isinstance(publishers[0], dict) else (publishers[0] if publishers else '')
        languages = book.get('languages', [])
        language = languages[0]['key'].split('/')[-1] if languages and isinstance(languages[0], dict) else (languages[0] if languages else '')
        
        return {
            'title': title,
            'author': authors,
            'cover': cover_url,
            'description': description,
            'published_date': published_date,
            'page_count': page_count,
            'categories': categories,
            'publisher': publisher,
            'language': language
        }
    return None

def get_google_books_cover(isbn, fetch_title_author=False):
    url = f"https://www.googleapis.com/books/v1/volumes?q=isbn:{isbn}"
    try:
        response = rate_limited_request(url, timeout=5)
        data = response.json()
        items = data.get("items")
        if items:
            volume_info = items[0]["volumeInfo"]
            image_links = volume_info.get("imageLinks", {})
            cover_url = image_links.get("thumbnail") or image_links.get("smallThumbnail")
            
            if fetch_title_author:
                title = volume_info.get('title')
                authors = ", ".join(volume_info.get('authors', []))
                description = volume_info.get('description', '')
                published_date = volume_info.get('publishedDate', '')
                page_count = volume_info.get('pageCount')
                categories = ', '.join(volume_info.get('categories', []))
                publisher = volume_info.get('publisher', '')
                language = volume_info.get('language', '')
                average_rating = volume_info.get('averageRating')
                rating_count = volume_info.get('ratingsCount')
                
                return {
                    'cover': cover_url,
                    'title': title,
                    'author': authors,
                    'description': description,
                    'published_date': published_date,
                    'page_count': page_count,
                    'categories': categories,
                    'publisher': publisher,
                    'language': language,
                    'average_rating': average_rating,
                    'rating_count': rating_count
                }
            return cover_url
    except Exception:
        pass
    if fetch_title_author:
        return None # Or return a dict with None values if preferred
    return None

def format_date(date):
    return date.strftime("%Y-%m-%d") if date else None

def get_reading_streak(timezone):
    # Get all unique dates with a reading log, sorted descending
    dates = (
        ReadingLog.query.with_entities(ReadingLog.date)
        .distinct()
        .order_by(ReadingLog.date.desc())
        .all()
    )
    dates = [d[0] for d in dates]

    streak_offset = current_app.config.get('READING_STREAK_OFFSET', 0)
    if not dates:
        return streak_offset

    # Use the configured timezone for "today"
    now_ca = datetime.now(timezone)
    today = now_ca.date()

    # Ensure the most recent date matches today
    if dates[0] != today:
        return streak_offset
    streak = 0
    streak = 1  # Start with the first day logged
    for i in range(1, len(dates)):
        # Check if the current date is exactly one day after the previous date
        if (dates[i] - dates[i - 1]).days == 1:
            streak += 1
        else:
            break  # Stop counting if a day is missed

    return streak + streak_offset

def generate_month_review_image(books, month, year):
    import calendar
    from PIL import Image, ImageDraw, ImageFont
    from io import BytesIO
    import requests
    import os

    img_size = 1080
    cols = 4
    cover_w, cover_h = 200, 300
    padding = 30
    # Increase title_height to give more space for the text
    title_height = 220
    grid_w = cols * cover_w + (cols - 1) * padding
    rows = ((len(books) - 1) // cols) + 1 if books else 1
    grid_h = rows * cover_h + (rows - 1) * padding
    # Move grid lower to avoid overlap
    grid_top = title_height + 40
    grid_left = (img_size - grid_w) // 2

    # Try bookshelf background
    bg_path = os.path.abspath(os.path.join(os.path.dirname(__file__), 'static', 'bookshelf.png'))
    print("Looking for bookshelf background at:", bg_path)
    try:
        bg = Image.open(bg_path).convert('RGBA').resize((img_size, img_size))
        print("Bookshelf background loaded!")
    except Exception as e:
        print("Failed to load bookshelf background:", e)
        bg = Image.new('RGBA', (img_size, img_size), (255, 230, 200, 255))

    draw = ImageDraw.Draw(bg)

    # Draw month title in white
    month_name = f"{calendar.month_name[month].upper()} {year}"
    max_width = img_size - 80  # 40px margin on each side
    font_size = 220
    font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    if not os.path.exists(font_path):
        font_path = os.path.join(os.path.dirname(__file__), "static", "Arial.ttf")
    while font_size > 10:
        try:
            font = ImageFont.truetype(font_path, font_size)
        except Exception as e:
            print("Font load failed:", e)
            font = ImageFont.load_default()
        bbox = draw.textbbox((0, 0), month_name, font=font)
        w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
        if w <= max_width:
            break
        font_size -= 10
    shadow_offset = 4
    # Draw shadow for readability
    draw.text(((img_size - w) // 2 + shadow_offset, 40 + shadow_offset), month_name, fill=(0,0,0,128), font=font)
    # Draw main text in white
    draw.text(((img_size - w) // 2, 40), month_name, fill=(255, 255, 255), font=font)

    # Place covers
    for idx, book in enumerate(books):
        row = idx // cols
        col = idx % cols
        x = grid_left + col * (cover_w + padding)
        y = grid_top + row * (cover_h + padding)
        cover_url = getattr(book, 'cover_url', None)
        try:
            if cover_url:
                response = rate_limited_request(cover_url, timeout=10)
                cover = Image.open(BytesIO(response.content)).convert("RGBA")
                cover = cover.resize((cover_w, cover_h))
            else:
                raise Exception("No cover")
        except Exception:
            cover = Image.new('RGBA', (cover_w, cover_h), (220, 220, 220, 255))
        bg.paste(cover, (x, y), cover if cover.mode == 'RGBA' else None)

    return bg.convert('RGB')

import threading
import uuid
from .models import Task, Book, db
import csv
import io

# Background task management
active_tasks = {}

def run_bulk_import_task(app, task_id, csv_content, file_format='isbn_only'):
    """Background task function for bulk import"""
    
    with app.app_context():
        task = Task.query.get(task_id)
        if not task:
            return
        
        try:
            task.set_running()
            
            # Parse CSV content
            csv_file = io.StringIO(csv_content)
            reader = csv.reader(csv_file)
            
            # Skip header if present
            first_row = next(reader, None)
            if not first_row:
                task.set_failed("Empty CSV file")
                return
            
            # Check if first row is header (contains non-ISBN data)
            if file_format == 'isbn_only' and not first_row[0].replace('-', '').isdigit():
                # Skip header row
                pass
            else:
                # Reset reader to include first row
                csv_file.seek(0)
                reader = csv.reader(csv_file)
            
            # Count total items first
            all_rows = list(reader)
            task.total_items = len(all_rows)
            db.session.commit()
            
            success_count = 0
            error_count = 0
            
            for i, row in enumerate(all_rows, 1):
                if not row or not row[0].strip():
                    continue
                
                isbn = row[0].strip().replace('-', '')
                task.update_progress(i, f"Processing ISBN: {isbn}")
                
                try:
                    # Check if book already exists
                    existing_book = Book.get_book_by_isbn(isbn)
                    if existing_book:
                        current_app.logger.info(f"Book with ISBN {isbn} already exists, skipping")
                        continue
                    
                    # Try to fetch book data
                    book_data = fetch_book_data(isbn)
                    if not book_data or not book_data.get('title'):
                        # Fallback to Google Books
                        google_data = get_google_books_cover(isbn, fetch_title_author=True)
                        if google_data and google_data.get('title'):
                            book_data = google_data
                    
                    if book_data and book_data.get('title') and book_data.get('author'):
                        # Create new book
                        new_book = Book(
                            isbn=isbn,
                            title=book_data.get('title', 'Unknown Title'),
                            author=book_data.get('author', 'Unknown Author'),
                            cover_url=book_data.get('cover'),
                            description=book_data.get('description'),
                            published_date=book_data.get('published_date'),
                            page_count=book_data.get('page_count'),
                            categories=book_data.get('categories'),
                            publisher=book_data.get('publisher'),
                            language=book_data.get('language'),
                            average_rating=book_data.get('average_rating'),
                            rating_count=book_data.get('rating_count'),
                            want_to_read=True
                        )
                        new_book.save()
                        success_count += 1
                        app.logger.info(f"Successfully imported: {book_data['title']}")
                    else:
                        error_count += 1
                        app.logger.warning(f"Could not find book data for ISBN: {isbn}")
                
                except Exception as e:
                    error_count += 1
                    app.logger.error(f"Error importing ISBN {isbn}: {e}")
                
                # Update progress
                task.update_progress(i, success_count=success_count, error_count=error_count)
            
            # Task completed
            result = {
                'total_processed': len(all_rows),
                'successful_imports': success_count,
                'errors': error_count,
                'message': f"Import completed: {success_count} books imported, {error_count} errors"
            }
            task.set_completed(result)
            
        except Exception as e:
            app.logger.error(f"Bulk import task failed: {e}")
            task.set_failed(str(e))
        finally:
            # Clean up
            if task_id in active_tasks:
                del active_tasks[task_id]

def start_bulk_import_task(csv_content, file_format='isbn_only'):
    """Start a background bulk import task"""
    from flask import current_app
    
    task_id = str(uuid.uuid4())
    task = Task(
        id=task_id,
        name="Bulk Import",
        description="Importing books from CSV file"
    )
    db.session.add(task)
    db.session.commit()
    
    # Start background thread
    thread = threading.Thread(
        target=run_bulk_import_task,
        args=(current_app._get_current_object(), task_id, csv_content, file_format)
    )
    thread.daemon = True
    thread.start()
    
    active_tasks[task_id] = thread
    return task_id