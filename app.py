import json
import os
import re
import subprocess
import zipfile
from flask import Flask, Response, render_template_string
import requests
import time
import ast

# --- Configuration ---
# In a real deployed app, use environment variables. For this example, we leave it blank.
# The hosting platform (Canvas) will provide the key at runtime.
GEMINI_API_KEY = ""
JS_PARSERS_DIR = 'webtoepub_js_parsers'
GENERATED_DIR = 'generated_parsers'
OUTPUT_JSON = 'parsers_data.json'
OUTPUT_ZIP = 'parsers.zip'

# --- Flask App Setup ---
app = Flask(__name__)

# --- Helper Functions ---

def to_snake_case(name):
    """Converts CamelCase to snake_case for filenames."""
    name = re.sub('(.)([A-Z][a-z]+)', r'\1_\2', name)
    name = re.sub('([a-z0-9])([A-Z])', r'\1_\2', name).lower()
    return name.replace('_parser', '_parser')

def run_command(command):
    """Runs a shell command and returns its output."""
    try:
        result = subprocess.run(command, check=True, capture_output=True, text=True, shell=True)
        return result.stdout
    except subprocess.CalledProcessError as e:
        print(f"Error running command: {command}")
        print(f"Stderr: {e.stderr}")
        raise

# --- Core Logic: Parser Conversion ---

def stream_logs(message):
    """Helper to format messages for Server-Sent Events."""
    return f"data: {message}\n\n"

def extract_parser_data():
    """Uses the Node.js script to extract data from JS files into a JSON file."""
    yield stream_logs("Starting parser data extraction...")
    if not os.path.exists(JS_PARSERS_DIR):
        raise FileNotFoundError(f"Source directory not found: {JS_PARSERS_DIR}")
    
    yield stream_logs("Running Node.js extractor script (generate_json.js)...")
    try:
        run_command("node generate_json.js")
        yield stream_logs(f"✅ Success! Extracted data to {OUTPUT_JSON}")
    except Exception as e:
        yield stream_logs(f"❌ ERROR: Failed to run Node.js script. {e}")
        return
    
    with open(OUTPUT_JSON, 'r', encoding='utf-8') as f:
        all_parsers_data = json.load(f)
    yield stream_logs(f"Found data for {len(all_parsers_data)} parsers.")


def call_gemini_api(prompt):
    """Calls the Gemini API to convert JS to Python."""
    api_url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-preview-05-20:generateContent?key={GEMINI_API_KEY}"
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": 4096,
        }
    }
    
    # Simple exponential backoff
    for i in range(3): # Retry up to 3 times
        try:
            response = requests.post(api_url, json=payload, timeout=120)
            response.raise_for_status()
            result = response.json()
            candidate = result.get('candidates', [{}])[0]
            content = candidate.get('content', {}).get('parts', [{}])[0]
            return content.get('text', '')
        except requests.exceptions.RequestException as e:
            print(f"API request failed (attempt {i+1}): {e}")
            time.sleep(2 ** i) # 1s, 2s, 4s
    return None


def convert_and_validate_parsers():
    """Main generator function to convert, validate, and yield progress."""
    if not os.path.exists(OUTPUT_JSON):
        yield stream_logs(f"❌ ERROR: {OUTPUT_JSON} not found. Run extraction first.")
        return

    with open(OUTPUT_JSON, 'r', encoding='utf-8') as f:
        parsers_to_convert = json.load(f)
    
    if os.path.exists(GENERATED_DIR):
        import shutil
        shutil.rmtree(GENERATED_DIR)
    os.makedirs(GENERATED_DIR)
    
    total_parsers = len(parsers_to_convert)
    for i, parser_data in enumerate(parsers_to_convert):
        js_filename = parser_data['js_filename']
        class_name = parser_data['class_name']
        yield stream_logs(f"({i+1}/{total_parsers}) Converting {js_filename} -> {class_name}...")

        try:
            with open(os.path.join(JS_PARSERS_DIR, js_filename), 'r', encoding='utf-8') as f:
                js_code = f.read()
        except FileNotFoundError:
            yield stream_logs(f"   - ⚠️ WARNING: JS file not found, skipping.")
            continue

        python_code = None
        validation_error = None
        for attempt in range(3): # Allow up to 3 self-correction attempts
            if attempt == 0:
                prompt = f"""
                You are an expert code converter. Convert the following JavaScript web scraper parser class into a Python class.

                **Rules:**
                1. The new Python class must inherit from `lncrawl.parser.WebToEpubParser`.
                2. Use the exact class name: `{class_name}`.
                3. The `base_url` must be a Python list of strings: {json.dumps(parser_data['base_urls'])}.
                4. Implement the following methods if their corresponding selector is present: `find_content`, `extract_title`, `extract_author`, `find_cover_image_url`.
                5. The methods should use `dom.select_one('{{selector}}')` to find the element.
                6. For methods where no selector was extracted, add a comment indicating that manual implementation is needed and call the super method.
                7. Always include a placeholder `get_chapter_urls` method that returns an empty list and has a comment explaining it needs manual implementation.
                8. The final output must be ONLY the Python code block, with no explanations or markdown fences.

                **Extracted Selectors:**
                - Content: {parser_data['selectors'].get('content')}
                - Title: {parser_data['selectors'].get('title')}
                - Author: {parser_data['selectors'].get('author')}
                - Cover: {parser_data['selectors'].get('cover')}

                **JavaScript Source Code:**
                ```javascript
                {js_code}
                ```
                """
            else: # Self-correction prompt
                prompt = f"""
                The Python code you previously generated had a syntax error. Please fix it.

                **Error:**
                {validation_error}

                **Incorrect Python Code:**
                ```python
                {python_code}
                ```

                Return ONLY the corrected, valid Python code, with no explanations.
                """
                yield stream_logs(f"   - 🤖 Self-correction attempt {attempt}...")

            python_code = call_gemini_api(prompt)

            if not python_code:
                yield stream_logs("   - ❌ ERROR: No response from Gemini API.")
                break
            
            # Clean up the response from markdown fences
            python_code = re.sub(r'```python\n|```', '', python_code).strip()

            # Validate syntax
            try:
                ast.parse(python_code)
                validation_error = None
                yield stream_logs("   - ✅ Code is syntactically valid.")
                break # Exit correction loop
            except SyntaxError as e:
                validation_error = str(e)
                yield stream_logs(f"   - ❌ ERROR: Invalid Python syntax generated: {e}")

        if validation_error is None and python_code:
            # Save the valid code
            python_filename = to_snake_case(class_name) + ".py"
            first_char = class_name[0].lower()
            sub_dir_name = first_char if first_char.isalpha() else '_'
            sub_dir_path = os.path.join(GENERATED_DIR, 'en', sub_dir_name)
            os.makedirs(sub_dir_path, exist_ok=True)
            file_path = os.path.join(sub_dir_path, python_filename)
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(f"# Auto-generated from {js_filename}\n")
                f.write(python_code)
        else:
            yield stream_logs(f"   - ❌ FAILED to generate valid code for {js_filename} after multiple attempts.")
    
    yield stream_logs("\n\nAll parsers converted. Now creating ZIP file...")
    # Create ZIP
    with zipfile.ZipFile(OUTPUT_ZIP, 'w', zipfile.ZIP_DEFLATED) as zipf:
        root_dir = GENERATED_DIR
        for root, _, files in os.walk(root_dir):
            for file in files:
                file_path = os.path.join(root, file)
                archive_name = os.path.relpath(file_path, root_dir)
                zipf.write(file_path, os.path.join('sources', archive_name))
    
    yield stream_logs(f"✅ ZIP file '{OUTPUT_ZIP}' created.")
    
    # Upload to file.io
    yield stream_logs(f"Uploading to file sharing service...")
    try:
        with open(OUTPUT_ZIP, 'rb') as f:
            response = requests.post('https://file.io', files={'file': f}, timeout=60)
            response.raise_for_status()
            download_link = response.json().get('link')
            yield stream_logs(f"🎉 DONE! Download your parsers here: {download_link}")
    except Exception as e:
        yield stream_logs(f"❌ ERROR: Could not upload the file. {e}")
    
    yield stream_logs("---END---")


# --- Flask Routes ---

@app.route('/')
def index():
    """Render the main UI."""
    return render_template_string(open("templates/index.html").read())

@app.route('/start-conversion')
def start_conversion():
    """Endpoint to start the conversion process and stream logs."""
    def generate():
        try:
            yield from extract_parser_data()
            yield from convert_and_validate_parsers()
        except Exception as e:
            yield stream_logs(f"❌ A critical error occurred: {e}")
            yield stream_logs("---END---")
    
    return Response(generate(), mimetype='text/event-stream')

if __name__ == '__main__':
    if not os.path.exists('node_modules'):
        print("Node.js dependencies not found. Running 'npm install'...")
        try:
            run_command("npm install")
            print("npm install successful.")
        except Exception as e:
            print(f"Failed to install Node.js dependencies: {e}")
            exit(1)
            
    app.run(debug=True, host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))
