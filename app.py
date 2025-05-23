import os
from flask import Flask, render_template, request, redirect, url_for, flash, send_from_directory, session
from datetime import datetime
import json
import sqlite3
from werkzeug.security import generate_password_hash, check_password_hash
import re
import config
import io # NEW: For handling file streams
import uuid # NEW: For generating unique temporary filenames
import shutil # NEW IMPORT FOR SHUTIL.MOVE

# NEW IMPORTS FOR IMAGE HANDLING AND METADATA
from PIL import Image # For image handling
import piexif # For EXIF metadata manipulation (ensure you ran: pip install piexif)
# /NEW IMPORTS FOR IMAGE HANDLING AND METADATA

# --- Flask App Configuration ---
app = Flask(__name__)
app.jinja_env.globals.update(now=datetime.utcnow) # Make 'now()' function available in templates
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['SECRET_KEY'] = config.SECRET_KEY
app.config['DATABASE'] = config.DATABASE
app.config['ALLOWED_EXTENSIONS'] = config.ALLOWED_EXTENSIONS

# Your Copyright Information - IMPORTANT: CUSTOMIZE THIS!
COPYRIGHT_TEXT = "© Ben Derico - All Rights Reserved"

# Ensure the upload folder exists
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

# --- Database Setup ---
def get_db():
    """Connects to the SQLite database."""
    db = sqlite3.connect(app.config['DATABASE'])
    db.row_factory = sqlite3.Row # This makes results behave like dictionaries
    return db

def init_db():
    """Initializes the database schema."""
    with app.app_context():
        db = get_db()
        cursor = db.cursor()
        # Create photos table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS photos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                filename TEXT NOT NULL UNIQUE,
                timestamp TEXT NOT NULL
            )
        ''')
        # Create claims table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS claims (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                photo_id INTEGER NOT NULL,  -- New: Link to photo
                first_name TEXT NOT NULL,
                email TEXT,                 -- New: Optional email
                timestamp TEXT NOT NULL,
                FOREIGN KEY (photo_id) REFERENCES photos (id) ON DELETE CASCADE
            )
        ''')
        # Create users table (for admin)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL
            )
        ''')
        db.commit()

        # --- Set up Admin User (Run once!) ---
        cursor.execute("SELECT * FROM users WHERE username = ?", ('admin',))
        if cursor.fetchone() is None:
            admin_password_hash = generate_password_hash(config.ADMIN_PASSWORD)
            cursor.execute(
                "INSERT INTO users (username, password_hash) VALUES (?, ?)",
                ('admin', admin_password_hash)
            )
            db.commit()
            print("Admin user 'admin' created with password from config.py")
        # --- End Admin User Setup ---

@app.cli.command('init-db')
def init_db_command():
    """Clear existing data and create new tables."""
    init_db()
    print('Initialized the database.')

# --- Helper Functions ---
def allowed_file(filename):
    """Checks if the uploaded file has an allowed image extension."""
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in app.config['ALLOWED_EXTENSIONS']

# --- Routes (Web Pages) ---

@app.route('/')
def index():
    """
    The main photo stream page. Displays uploaded photos and their associated
    claimed names and emails if available.
    """
    db = get_db()
    # Fetch photos, ordered by timestamp (newest first)
    photos_from_db = db.execute("SELECT id, filename, timestamp FROM photos ORDER BY timestamp DESC").fetchall()

    all_photos_data = []
    for photo_entry in photos_from_db:
        photo_id = photo_entry['id']
        filename = photo_entry['filename']
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)

        if os.path.exists(filepath):
            # Fetch claim for this specific photo ID
            claim_data = db.execute(
                "SELECT first_name, email FROM claims WHERE photo_id = ?",
                (photo_id,)
            ).fetchone()

            claimed_name = claim_data['first_name'] if claim_data else None
            claimed_email = claim_data['email'] if claim_data else None

            all_photos_data.append({
                'id': photo_id, # Pass photo ID to template
                'filename': filename,
                'timestamp': datetime.fromisoformat(photo_entry['timestamp']),
                'display_time': datetime.fromisoformat(photo_entry['timestamp']).strftime('%Y-%m-%d %H:%M:%S'),
                'claimed_name': claimed_name,
                'claimed_email': claimed_email # Pass email to template
            })
    db.close()
    return render_template('index.html', photos=all_photos_data)

@app.route('/upload', methods=['POST'])
def upload_file():
    uploaded_files = request.files.getlist('file') 

    if not uploaded_files:
        flash('No files selected for upload.')
        return redirect(url_for('upload_page'))

    success_count = 0
    error_messages = []

    for file in uploaded_files:
        if file.filename == '':
            continue

        # Phase 1: Determine final filenames and a unique temporary path
        timestamp_str = datetime.now().strftime('%Y%m%d%H%M%S%f')
        original_filename_base, original_filename_ext = os.path.splitext(file.filename)

        # Clean base name: replace any non-alphanumeric (except . and -) with underscore
        cleaned_original_filename = re.sub(r'[^\w.-]', '_', original_filename_base).strip()
        if len(cleaned_original_filename) > 50:
            cleaned_original_filename = cleaned_original_filename[:50]

        # Clean extension: ensure it only contains alphanumeric and dot, all lowercase
        cleaned_ext = re.sub(r'[^\w.]', '', original_filename_ext).lower() 
        if cleaned_ext and not cleaned_ext.startswith('.'):
             cleaned_ext = '.' + cleaned_ext.lstrip('.')
        elif not cleaned_ext and original_filename_ext: # Fallback if extension cleaning makes it empty
             cleaned_ext = ".bin" # Generic binary extension

        final_filename = f"{timestamp_str}_{cleaned_original_filename}{cleaned_ext}"
        final_filepath = os.path.join(app.config['UPLOAD_FOLDER'], final_filename)

        # Generate a unique temporary filename using uuid
        temp_unique_name = str(uuid.uuid4()) + cleaned_ext
        temp_filepath = os.path.join(app.config['UPLOAD_FOLDER'], temp_unique_name)

        file_saved_to_temp = False # Flag to track if the file was saved to temp_filepath
        metadata_processing_successful_in_try = False # Flag for metadata success (inside try)

        try:
            # Phase 2: Save the original uploaded file to the unique temporary location
            file.save(temp_filepath)
            file_saved_to_temp = True

            # Phase 3: Attempt to process metadata from the temporary file
            try:
                img = Image.open(temp_filepath)

                # --- NEW AGGRESSIVE EXIF STRATEGY START ---
                # Always create a FRESH EXIF structure to avoid issues with existing malformed data
                # This might overwrite ALL existing EXIF data, but guarantees your copyright
                fresh_exif_dict = {"0th": {}, "Exif": {}, "GPS": {}, "Interop": {}, "1st": {}, "thumbnail": None}

                # Set Copyright tag in the 0th IFD
                fresh_exif_dict["0th"][piexif.ImageIFD.Copyright] = COPYRIGHT_TEXT.encode('utf-8')

                exif_bytes = piexif.dump(fresh_exif_dict) # Dump the fresh EXIF dict to bytes
                # --- NEW AGGRESSIVE EXIF STRATEGY END ---

                # --- VERBOSE DEBUGGING PRINTS (Inside try block) ---
                print(f"\n--- DEBUG EXIF PROCESSING for {file.filename} ---")
                print(f"COPYRIGHT_TEXT: {COPYRIGHT_TEXT}")
                # print(f"Original EXIF info (raw): {img.info.get('exif')}") # Can be very long/noisy
                print(f"Fresh EXIF dict (before dump): {fresh_exif_dict}")
                # print(f"EXIF bytes (after dump, first 100): {exif_bytes[:100]}...") # raw bytes can be confusing
                print(f"Image format (detected by PIL): {img.format}")
                print(f"--- DEBUG EXIF PROCESSING END ---\n")
                # --- VERBOSE DEBUGGING PRINTS END ---

                save_params = {'exif': exif_bytes, 'format': img.format}
                if img.format == 'JPEG':
                    save_params['quality'] = 90

                img.save(temp_filepath, **save_params) # Save modified image over temp file
                metadata_processing_successful_in_try = True # Flag that this part succeeded

            except Exception as e:
                error_messages.append(f"Error adding copyright metadata to {file.filename}: {e}. Uploaded without metadata.")

            finally:
                # Phase 4: Move the (potentially modified) temporary file to its final name
                # --- DEBUG FINAL MOVE START ---
                print(f"\n--- DEBUG FINAL MOVE START for {file.filename} ---")
                print(f"temp_filepath: '{temp_filepath}'")
                print(f"final_filepath: '{final_filepath}'")
                print(f"temp_exists (before move): {os.path.exists(temp_filepath)}")
                print(f"--- DEBUG FINAL MOVE END ---\n")

                try:
                    shutil.move(temp_filepath, final_filepath) # *** shutil.move ***

                    # Store photo info in the database only if the file was successfully moved
                    db = get_db()
                    db.execute(
                        "INSERT INTO photos (filename, timestamp) VALUES (?, ?)",
                        (final_filename, datetime.now().isoformat())
                    )
                    db.commit()
                    db.close()

                    if metadata_processing_successful_in_try: # Only count as success if metadata was also processed
                        success_count += 1

                except Exception as move_e: # This handles the critical os.rename/shutil.move failure
                    error_messages.append(f"CRITICAL MOVE/DB ERROR for {file.filename}: {move_e}. Temp file '{os.path.basename(temp_filepath)}' might remain in uploads.")
                    if os.path.exists(temp_filepath):
                        os.remove(temp_filepath)

        except Exception as e: # This catches errors if file.save(temp_filepath) itself failed (Phase 2)
            error_messages.append(f"CRITICAL SAVE ERROR: Could not save original {file.filename} to temp: {e}. File not uploaded.")
            if os.path.exists(temp_filepath):
                os.remove(temp_filepath)

    if success_count > 0:
        flash(f'Successfully uploaded {success_count} photo(s)!')
    if error_messages:
        for msg in error_messages:
            flash(msg, 'error')

    return redirect(url_for('index'))

@app.route('/uploads/<filename>')
def uploaded_file(filename):
    """Serves uploaded files directly from the 'uploads' folder."""
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

@app.route('/claim/<int:photo_id>', methods=['GET', 'POST'])
def claim_photo_by_id(photo_id):
    db = get_db()
    photo_info = db.execute("SELECT filename, timestamp FROM photos WHERE id = ?", (photo_id,)).fetchone()
    if photo_info is None:
        flash('Photo not found.')
        db.close()
        return redirect(url_for('index'))

    if request.method == 'POST':
        first_name = request.form['first_name'].strip()
        email = request.form.get('email', '').strip()
        claimed_time_str = request.form['claimed_time'].strip()

        if not first_name:
            flash('Please enter your first name.')
            db.close()
            return redirect(url_for('claim_photo_by_id', photo_id=photo_id))

        existing_claim = db.execute("SELECT id FROM claims WHERE photo_id = ?", (photo_id,)).fetchone()
        if existing_claim:
            flash('This photo has already been claimed.')
            db.close()
            return redirect(url_for('index'))

        try:
            claimed_time = datetime.fromisoformat(claimed_time_str.replace('Z', '+00:00'))
            if claimed_time.tzinfo is not None:
                claimed_time = claimed_time.replace(tzinfo=None)
        except ValueError:
            flash('Invalid timestamp received. Please try again.')
            db.close()
            return redirect(url_for('claim_photo_by_id', photo_id=photo_id))

        db.execute(
            "INSERT INTO claims (photo_id, first_name, email, timestamp) VALUES (?, ?, ?, ?)",
            (photo_id, first_name, email, claimed_time.isoformat())
        )
        db.commit()
        db.close()
        flash(f'Thanks, {first_name}! Your claim for photo {photo_id} has been recorded.')
        return render_template('claim_success.html', first_name=first_name, photo_id=photo_id)

    db.close()
    return render_template('claim.html', photo_id=photo_id, photo_filename=photo_info['filename'])

@app.route('/upload-page')
def upload_page():
    if not session.get('logged_in'):
        flash('Please log in to access the upload page.')
        return redirect(url_for('admin_login'))
    return render_template('upload_page.html')

@app.route('/admin_login', methods=['GET', 'POST'])
def admin_login():
    """Handles the login for the admin panel."""
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')

        db = get_db()
        user = db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        db.close()

        if user and check_password_hash(user['password_hash'], password):
            session['logged_in'] = True
            flash('Logged in successfully!')
            return redirect(url_for('admin_panel'))
        else:
            flash('Incorrect username or password.')
            return render_template('admin_login.html')
    return render_template('admin_login.html')

@app.route('/admin_logout')
def admin_logout():
    """Logs the admin out."""
    session.pop('logged_in', None)
    flash('You have been logged out.')
    return redirect(url_for('index'))

@app.before_request
def check_admin_auth():
    """Checks if user is logged in before accessing admin panel or upload page."""
    protected_endpoints = ['admin_panel', 'upload_page']
    if request.endpoint in protected_endpoints and not session.get('logged_in'):
        flash('Please log in to access this page.')
        return redirect(url_for('admin_login'))

@app.route('/admin', methods=['GET', 'POST'])
def admin_panel():
    """
    Admin page for managing claims (view, edit, delete).
    Access is protected by session.
    """
    db = get_db()

    if request.method == 'POST':
        action = request.form.get('action')
        claim_id_to_process = request.form.get('claim_id')

        if action == 'edit':
            new_name = request.form.get('new_name', '').strip()
            new_timestamp_str = request.form.get('new_timestamp', '').strip()

            if not new_name:
                flash('First name cannot be empty during edit.')
                claims = db.execute("SELECT * FROM claims ORDER BY timestamp DESC").fetchall()
                db.close()
                return render_template('admin.html', claims=claims, logged_in=True)

            try:
                new_timestamp = datetime.fromisoformat(new_timestamp_str.replace('Z', '+00:00'))
                if new_timestamp.tzinfo is not None:
                    new_timestamp = new_timestamp.replace(tzinfo=None)
            except ValueError:
                flash('Invalid date/time format for editing. Please use whereupon-MM-DDTHH:MM:SS.FFFZ format.')
                claims = db.execute("SELECT * FROM claims ORDER BY timestamp DESC").fetchall()
                db.close()
                return render_template('admin.html', claims=claims, logged_in=True)

            db.execute(
                "UPDATE claims SET first_name = ?, timestamp = ? WHERE id = ?",
                (new_name, new_timestamp.isoformat(), claim_id_to_process)
            )
            db.commit()
            flash('Claim updated successfully.')

        elif action == 'delete':
            db.execute("DELETE FROM claims WHERE id = ?", (claim_id_to_process,))
            db.commit()
            flash('Claim deleted successfully.')

    claims = db.execute("SELECT * FROM claims ORDER BY timestamp DESC").fetchall()
    db.close()
    return render_template('admin.html', claims=claims, logged_in=True)

# --- Run the app ---
if __name__ == '__main__':
    # For development, debug=True provides automatic reloading and detailed errors.
    # For production, set debug=False and use a production WSGI server.
    app.run(debug=True, host='0.0.0.0')
