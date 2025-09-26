import json
import os
import re
import subprocess
import zipfile
import time
import ast
from flask import Flask, jsonify, render_template_string, request

# --- Configuration ---
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
JS_PARSERS_DIR = 'webtoepub_js_parsers'
GENERATED_DIR = 'generated_parsers'
OUTPUT_JSON = 'parsers_data.json'
OUTPUT_ZIP = 'parsers.zip'

# --- Flask App Setup ---
app = Flask(__name__)

# --- Helper Functions ---
def to_snake_case(name):
    name = re.sub('(.)([A-Z][a-z]+)', r'\1_\2', name)
    name = re.sub('([a-z0-9])([A-Z])', r'\1_\2', name).lower()
    return name.replace('_parser', '_parser')

def call_gemini_api(prompt):
    """Calls the Gemini API with retries and robust error handling."""
    if not GEMINI_API_KEY:
        raise ValueError("GEMINI_API_KEY is not set on the server.")
        
    api_url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-preview-05-20:generateContent?key={GEMINI_API_KEY}"
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 4096}
    }
    
    for attempt in range(3):
        try:
            response = requests.post(api_url, json=payload, timeout=120)
            response.raise_for_status()
            result = response.json()
            
            # Defensive parsing of the Gemini response
            if 'candidates' in result and result['candidates']:
                candidate = result['candidates'][0]
                if 'content' in candidate and 'parts' in candidate['content'] and candidate['content']['parts']:
                    return candidate['content']['parts'][0].get('text', '')
            
            # Handle cases where the API returns a valid response but no content (e.g., safety blocks)
            return f"Error: Gemini API returned an unexpected response structure: {json.dumps(result)}"
            
        except requests.exceptions.RequestException as e:
            print(f"API request failed (attempt {attempt+1}): {e}")
            if attempt < 2:
                time.sleep(2 ** attempt) # Exponential backoff
        except Exception as e:
            print(f"An unexpected error occurred during API call: {e}")
            return f"Error: An unexpected error occurred: {e}"

    return None # Return None after all retries fail

# --- Flask Routes ---

@app.route('/')
def index():
    """Render the main user interface."""
    return render_template_string(open("templates/index.html").read())

@app.route('/extract-data', methods=['POST'])
def extract_data():
    """Runs the Node.js extractor script and returns the resulting JSON data."""
    try:
        if os.path.exists(GENERATED_DIR):
            import shutil
            shutil.rmtree(GENERATED_DIR)
        os.makedirs(GENERATED_DIR)

        # Ensure node modules are installed before running the script
        if not os.path.exists('node_modules'):
             subprocess.run("npm install", check=True, capture_output=True, text=True, shell=True)

        subprocess.run("node generate_json.js", check=True, capture_output=True, text=True, shell=True)
        
        with open(OUTPUT_JSON, 'r', encoding='utf-8') as f:
            all_parsers_data = json.load(f)
        
        return jsonify(all_parsers_data)
    except subprocess.CalledProcessError as e:
        return jsonify({"error": "Failed to run Node.js extractor script.", "details": e.stderr}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/convert-single-parser', methods=['POST'])
def convert_single_parser():
    """Receives data for ONE parser, converts it, validates it, and saves the file."""
    try:
        parser_data = request.json
        js_filename = parser_data['js_filename']
        class_name = parser_data['class_name']

        try:
            with open(os.path.join(JS_PARSERS_DIR, js_filename), 'r', encoding='utf-8') as f:
                js_code = f.read()
        except FileNotFoundError:
            return jsonify({"status": "skipped", "reason": f"JS file '{js_filename}' not found on server."}), 404

        python_code, validation_error = None, None
        for attempt in range(3): # Self-correction loop
            prompt = ""
            if attempt == 0:
                prompt = f"""
                You are an expert code converter. Convert the following JavaScript web scraper parser class into a Python class.
                **Rules:**
                1. The new Python class must inherit from `lncrawl.parser.WebToEpubParser`.
                2. Use the exact class name: `{class_name}`.
                3. The `base_url` must be a Python list of strings: {json.dumps(parser_data['base_urls'])}.
                4. Implement `find_content`, `extract_title`, `extract_author`, and `find_cover_image_url` if a selector is provided.
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
            else:
                prompt = f"""
                The Python code you previously generated had a syntax error. Please fix it.
                **Error:** {validation_error}
                **Incorrect Python Code:**
                ```python
                {python_code}
                ```
                Return ONLY the corrected, valid Python code, with no explanations.
                """
            
            python_code = call_gemini_api(prompt)
            if not python_code:
                validation_error = "No response from API after multiple retries."
                continue

            if python_code.startswith("Error:"):
                 validation_error = python_code
                 continue

            python_code = re.sub(r'```python\n|```', '', python_code).strip()
            
            try:
                ast.parse(python_code)
                validation_error = None
                break
            except SyntaxError as e:
                validation_error = str(e)

        if validation_error is None and python_code:
            python_filename = to_snake_case(class_name) + ".py"
            first_char = class_name[0].lower()
            sub_dir_name = first_char if first_char.isalpha() else '_'
            sub_dir_path = os.path.join(GENERATED_DIR, 'en', sub_dir_name)
            os.makedirs(sub_dir_path, exist_ok=True)
            with open(os.path.join(sub_dir_path, python_filename), 'w', encoding='utf-8') as f:
                f.write(f"# Auto-generated from {js_filename}\n{python_code}")
            return jsonify({"status": "success", "filename": python_filename})
        else:
            return jsonify({"status": "failed", "error": validation_error}), 500
    
    except Exception as e:
        # This is the crucial top-level error handler
        print(f"FATAL ERROR in /convert-single-parser: {e}")
        return jsonify({"status": "failed", "error": f"A critical server error occurred: {e}"}), 500


@app.route('/zip-and-upload', methods=['POST'])
def zip_and_upload():
    """Creates a ZIP of all generated files and uploads it to a public service."""
    try:
        with zipfile.ZipFile(OUTPUT_ZIP, 'w', zipfile.ZIP_DEFLATED) as zipf:
            for root, _, files in os.walk(GENERATED_DIR):
                for file in files:
                    file_path = os.path.join(root, file)
                    archive_name = os.path.relpath(file_path, GENERATED_DIR)
                    zipf.write(file_path, os.path.join('sources', archive_name))

        with open(OUTPUT_ZIP, 'rb') as f:
            response = requests.post('https://file.io', files={'file': f}, timeout=60)
            response.raise_for_status()
            download_link = response.json().get('link')
            return jsonify({"status": "success", "download_link": download_link})
    except Exception as e:
        return jsonify({"status": "failed", "error": f"Could not zip or upload the file: {e}"}), 500

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))
