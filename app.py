from flask import Flask, render_template, request, redirect, url_for, session, jsonify, abort, send_file
from datetime import datetime, timedelta
import json, pytz, os, re, uuid, base64, requests, logging, sys, threading, time, glob, secrets, time
from reportGenerator import generate_report
from request_queue import RequestQueue
from functools import wraps

app = Flask(__name__)
app.secret_key = "super_secret_key"

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

RESULT_CACHE = {}

class SecurityConfig:
    @staticmethod
    def generate_session_token():
        return secrets.token_urlsafe(32)

def require_valid_session(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('can_generate_report', False):
            return jsonify({
                "success": False,
                "error": "Unauthorized access. Please generate a report through the proper form first."
            }), 403
        
        form_values = session.get('form_values')
        if not form_values or not form_values.get('train_model') or not form_values.get('date'):
            return jsonify({
                "success": False,
                "error": "Invalid session. Please start from the home page."
            }), 403
        
        return f(*args, **kwargs)
    return decorated_function

def get_user_device_info():
    user_agent = request.headers.get('User-Agent', '')
    
    if any(mobile in user_agent.lower() for mobile in ['mobile', 'android', 'iphone', 'ipad', 'tablet']):
        device_type = 'Mobile'
    else:
        device_type = 'PC'
    
    browser = 'Unknown'
    user_agent_lower = user_agent.lower()
    
    if 'chrome' in user_agent_lower and 'edge' not in user_agent_lower and 'opr' not in user_agent_lower:
        browser = 'Chrome'
    elif 'firefox' in user_agent_lower:
        browser = 'Firefox'
    elif 'safari' in user_agent_lower and 'chrome' not in user_agent_lower:
        browser = 'Safari'
    elif 'edge' in user_agent_lower:
        browser = 'Edge'
    elif 'opera' in user_agent_lower or 'opr' in user_agent_lower:
        browser = 'Opera'
    elif 'msie' in user_agent_lower or 'trident' in user_agent_lower:
        browser = 'Internet Explorer'
    
    return device_type, browser

with open('config.json', 'r', encoding='utf-8') as config_file:
    CONFIG = json.load(config_file)

PDF_CLEANUP_ENABLED = True
PDF_MAX_AGE_MINUTES = 30
PDF_CLEANUP_INTERVAL_MINUTES = 10
PDF_CLEANUP_BATCH_SIZE = 50

class PDFCleanupManager:
    
    def __init__(self):
        self.cleanup_running = False
        self.last_cleanup = datetime.now()
        self.stats = {
            'total_cleaned': 0,
            'cleanup_cycles': 0,
            'last_cleanup_time': None,
            'files_failed_to_delete': 0
        }
    
    def cleanup_old_pdfs(self):
        if not PDF_CLEANUP_ENABLED:
            return
        
        if self.cleanup_running:
            return
        
        self.cleanup_running = True
        try:
            pdf_files = glob.glob("*.pdf")
            
            if not pdf_files:
                return
            
            current_time = datetime.now()
            cutoff_time = current_time - timedelta(minutes=PDF_MAX_AGE_MINUTES)
            
            files_to_delete = []
            files_processed = 0
            
            for pdf_file in pdf_files[:PDF_CLEANUP_BATCH_SIZE]:
                try:
                    file_stat = os.stat(pdf_file)
                    file_modified_time = datetime.fromtimestamp(file_stat.st_mtime)
                    
                    if file_modified_time < cutoff_time:
                        files_to_delete.append((pdf_file, file_modified_time))
                    
                    files_processed += 1
                except Exception:
                    continue
            
            deleted_count = 0
            failed_count = 0
            
            for pdf_file, file_time in files_to_delete:
                try:
                    os.remove(pdf_file)
                    deleted_count += 1
                except Exception:
                    failed_count += 1
                    continue
            
            self.stats['total_cleaned'] += deleted_count
            self.stats['cleanup_cycles'] += 1
            self.stats['last_cleanup_time'] = current_time.isoformat()
            self.stats['files_failed_to_delete'] += failed_count
            self.last_cleanup = current_time
            
        except Exception:
            pass
        finally:
            self.cleanup_running = False
    
    def start_background_cleanup(self):
        if not PDF_CLEANUP_ENABLED:
            return
        
        def cleanup_worker():
            while PDF_CLEANUP_ENABLED:
                try:
                    time.sleep(PDF_CLEANUP_INTERVAL_MINUTES * 60)
                    self.cleanup_old_pdfs()
                except Exception:
                    time.sleep(60)
        
        cleanup_thread = threading.Thread(target=cleanup_worker, daemon=True)
        cleanup_thread.start()
    
    def get_cleanup_stats(self):
        return self.stats.copy()

pdf_cleanup_manager = PDFCleanupManager()

with open('static/js/script.js', 'r', encoding='utf-8') as js_file:
    SCRIPT_JS_CONTENT = js_file.read()
with open('static/css/styles.css', 'r', encoding='utf-8') as css_file:
    STYLES_CSS_CONTENT = css_file.read()

default_banner_path = 'static/images/sample_banner.png'
DEFAULT_BANNER_IMAGE = ""
if os.path.exists(default_banner_path):
    try:
        with open(default_banner_path, 'rb') as img_file:
            encoded_image = base64.b64encode(img_file.read()).decode('utf-8')
            DEFAULT_BANNER_IMAGE = f"data:image/png;base64,{encoded_image}"
    except Exception:
        pass

instruction_image_path = 'static/images/instruction.png'
DEFAULT_INSTRUCTION_IMAGE = ""
if os.path.exists(instruction_image_path):
    try:
        with open(instruction_image_path, 'rb') as img_file:
            encoded_image = base64.b64encode(img_file.read()).decode('utf-8')
            DEFAULT_INSTRUCTION_IMAGE = f"data:image/png;base64,{encoded_image}"
    except Exception:
        pass

mobile_instruction_image_path = 'static/images/mobile_instruction.png'
DEFAULT_MOBILE_INSTRUCTION_IMAGE = ""
if os.path.exists(mobile_instruction_image_path):
    try:
        with open(mobile_instruction_image_path, 'rb') as img_file:
            encoded_image = base64.b64encode(img_file.read()).decode('utf-8')
            DEFAULT_MOBILE_INSTRUCTION_IMAGE = f"data:image/png;base64,{encoded_image}"
    except Exception:
        pass

def configure_request_queue():
    max_concurrent = CONFIG.get("queue_max_concurrent", 1)
    cooldown_period = CONFIG.get("queue_cooldown_period", 3)
    batch_cleanup_threshold = CONFIG.get("queue_batch_cleanup_threshold", 10)
    cleanup_interval = CONFIG.get("queue_cleanup_interval", 30)
    heartbeat_timeout = CONFIG.get("queue_heartbeat_timeout", 90)
    
    return RequestQueue(
        max_concurrent=max_concurrent, 
        cooldown_period=cooldown_period,
        batch_cleanup_threshold=batch_cleanup_threshold,
        cleanup_interval=cleanup_interval,
        heartbeat_timeout=heartbeat_timeout
    )

request_queue = configure_request_queue()

with open('trains_en.json', 'r') as f:
    trains_data = json.load(f)
    trains_full = trains_data['trains']
    trains = [train['train_name'] for train in trains_data['trains']]

with open('stations_en.json', 'r') as f:
    stations_data = json.load(f)
    stations = stations_data['stations']

def check_maintenance():
    if CONFIG.get("is_maintenance", 0):
        return render_template(
            'notice.html',
            message=CONFIG.get("maintenance_message", ""),
            styles_css=STYLES_CSS_CONTENT,
            script_js=SCRIPT_JS_CONTENT
        )
    return None

@app.before_request
def block_cloudflare_noise():
    if request.path.startswith('/cdn-cgi/'):
        return '', 404

@app.after_request
def set_cache_headers(response):
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response

@app.route('/ads.txt')
def ads_txt():
    try:
        ads_txt_path = os.path.join(os.path.dirname(__file__), 'ads.txt')
        return send_file(ads_txt_path, mimetype='text/plain')
    except FileNotFoundError:
        abort(404)

@app.route('/')
def home():
    maintenance_response = check_maintenance()
    if maintenance_response:
        return maintenance_response

    error = session.pop('error', None)

    app_version = CONFIG.get("version", "1.0.0")
    config = CONFIG.copy()
    
    banner_image = CONFIG.get("image_link") or DEFAULT_BANNER_IMAGE
    if not banner_image:
        banner_image = ""

    bst_tz = pytz.timezone('Asia/Dhaka')
    bst_now = datetime.now(bst_tz)
    min_date = bst_now.replace(hour=0, minute=0, second=0, microsecond=0)+timedelta(days=1)
    max_date = min_date + timedelta(days=10)
    bst_midnight_utc = min_date.astimezone(pytz.UTC).strftime('%Y-%m-%dT%H:%M:%SZ')

    if request.method == 'GET' and not session.get('form_submitted', False):
        session.pop('form_values', None)
        session.pop('pdf_filename', None)
    else:
        session['form_submitted'] = False

    form_values = session.get('form_values', {})
    if not form_values:
        form_values = None

    return render_template(
        'index.html',
        error=error,
        app_version=app_version,
        CONFIG=config,
        is_banner_enabled=CONFIG.get("is_banner_enabled", 0),
        banner_image=banner_image,
        instruction_image=DEFAULT_INSTRUCTION_IMAGE,
        mobile_instruction_image=DEFAULT_MOBILE_INSTRUCTION_IMAGE,
        min_date=min_date.strftime("%Y-%m-%d"),
        max_date=max_date.strftime("%Y-%m-%d"),
        bst_midnight_utc=bst_midnight_utc,
        show_disclaimer=True,
        form_values=form_values,
        trains=trains,
        trains_full=trains_full,
        stations=stations,
        styles_css=STYLES_CSS_CONTENT,
        script_js=SCRIPT_JS_CONTENT
    )

@app.route('/report_result')
def report_result():
    maintenance_response = check_maintenance()
    if maintenance_response:
        return maintenance_response

    result_id = session.pop('result_id', None)
    result = RESULT_CACHE.pop(result_id, None) if result_id else None
    form_values = session.get('form_values', None)

    if not result:
        return redirect(url_for('home'))

    if session.get('report_result_viewed', False):
        session.clear()
        session['error'] = "Session expired or page was refreshed. Please start a new search."
        return redirect(url_for('home'))
    
    session['report_result_viewed'] = True
    session['can_generate_report'] = True

    return render_template(
        'report.html',
        report_data=result,
        form_values=form_values,
        styles_css=STYLES_CSS_CONTENT,
        script_js=SCRIPT_JS_CONTENT
    )

def process_report_request(train_model, journey_date_str, api_date_format, form_values, auth_token, device_key):
    try:
        if not auth_token or not device_key:
            return {"error": "AUTH_CREDENTIALS_REQUIRED"}
        
        result = generate_report(train_model, api_date_format, auth_token, device_key)
        if not result or not result.get('success'):
            error_msg = result.get('error', 'Unknown error occurred') if result else 'No data received. Please try a different train or date.'
            return {"error": error_msg}
        
        pdf_filename = result.get('filename')
        return {"success": True, "result": result, "form_values": form_values, "pdf_filename": pdf_filename}
    except Exception as e:
        return {"error": str(e)}

@app.route('/queue_wait')
def queue_wait():
    maintenance_response = check_maintenance()
    if maintenance_response:
        return maintenance_response
    
    request_id = session.get('queue_request_id')
    if not request_id:
        session['error'] = "Your request session has expired. Please search again."
        return redirect(url_for('home'))
    
    status = request_queue.get_request_status(request_id)
    if not status:
        session['error'] = "Your request could not be found. Please search again."
        return redirect(url_for('home'))
    
    if request.args.get('refresh_check') == 'true':
        request_queue.cancel_request(request_id)
        session.pop('queue_request_id', None)
        session['error'] = "Page was refreshed. Please start a new search."
        return redirect(url_for('home'))
    
    form_values = session.get('form_values', {})
    
    return render_template(
        'queue.html',
        request_id=request_id,
        status=status, 
        form_values=form_values,
        styles_css=STYLES_CSS_CONTENT,
        script_js=SCRIPT_JS_CONTENT
    )

@app.route('/queue_status/<request_id>')
def queue_status(request_id):
    status = request_queue.get_request_status(request_id)
    if not status:
        return jsonify({"error": "Request not found"}), 404
    
    if status["status"] == "failed":
        result = request_queue.get_request_result(request_id)
        if result and "error" in result:
            status["errorMessage"] = result["error"]
    
    return jsonify(status)

@app.route('/cancel_request/<request_id>', methods=['POST'])
def cancel_request(request_id):
    try:
        removed = request_queue.cancel_request(request_id)
        
        if session.get('queue_request_id') == request_id:
            session.pop('queue_request_id', None)
        
        stats = request_queue.get_queue_stats()
        if stats.get('cancelled_pending', 0) > 5:
            request_queue.force_cleanup()
        
        return jsonify({"cancelled": removed, "status": "success"})
    except Exception as e:
        return jsonify({"cancelled": False, "status": "error", "error": str(e)}), 500

@app.route('/cancel_request_beacon/<request_id>', methods=['POST'])
def cancel_request_beacon(request_id):
    try:
        request_queue.cancel_request(request_id)
        return '', 204
    except Exception:
        return '', 204

@app.route('/queue_heartbeat/<request_id>', methods=['POST'])
def queue_heartbeat(request_id):
    try:
        updated = request_queue.update_heartbeat(request_id)
        return jsonify({"status": "success", "active": updated})
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500

@app.route('/show_results')
def show_results():
    maintenance_response = check_maintenance()
    if maintenance_response:
        return maintenance_response

    request_id = session.get('queue_request_id')
    if not request_id:
        session['error'] = "Your request session has expired. Please search again."
        return redirect(url_for('home'))
    return redirect(url_for('show_results_with_id', request_id=request_id))

@app.route('/show_results/<request_id>')
def show_results_with_id(request_id):
    maintenance_response = check_maintenance()
    if maintenance_response:
        return maintenance_response

    viewed_requests = session.get('viewed_requests', [])
    if request_id in viewed_requests:
        session.clear()
        session['error'] = "Session expired or page was refreshed. Please start a new search."
        return redirect(url_for('home'))

    queue_result = request_queue.get_request_result(request_id)
    
    if not queue_result:
        session['error'] = "Your request has expired or could not be found. Please search again."
        return redirect(url_for('home'))
    if "error" in queue_result:
        session['error'] = queue_result["error"]
        return redirect(url_for('home'))
    
    if not queue_result.get("success"):
        session['error'] = "An error occurred while processing your request. Please try again."
        return redirect(url_for('home'))
    
    result = queue_result.get("result", {})
    form_values = queue_result.get("form_values", {})
    pdf_filename = queue_result.get("pdf_filename")
    
    if session.get('queue_request_id') == request_id:
        session.pop('queue_request_id', None)
    
    if 'viewed_requests' not in session:
        session['viewed_requests'] = []
    session['viewed_requests'].append(request_id)
    
    session['form_values'] = form_values
    session['pdf_filename'] = pdf_filename
    session['can_generate_report'] = True
    
    return render_template(
        'report.html',
        report_data=result,
        form_values=form_values,
        styles_css=STYLES_CSS_CONTENT,
        script_js=SCRIPT_JS_CONTENT
    )

@app.route('/report', methods=['GET', 'POST'])
def report():
    maintenance_response = check_maintenance()
    if maintenance_response:
        return maintenance_response
    
    if request.method == 'GET':
        can_access_report = session.pop('can_access_report_page', False)
        form_values = session.get('form_values', {})
        can_generate = session.get('can_generate_report', False)
        
        if not can_access_report:
            session.clear()
            session['error'] = "Session expired or page was refreshed. Please start a new search."
            return redirect(url_for('home'))
        
        if (not form_values or 
            not form_values.get('train_model') or 
            not form_values.get('date') or 
            not can_generate):
            session.clear()
            session['error'] = "Invalid session data. Please start a new search."
            return redirect(url_for('home'))
        
        session.pop('can_access_report_page', None)
        
        return render_template(
            'report.html',
            form_values=form_values,
            styles_css=STYLES_CSS_CONTENT,
            script_js=SCRIPT_JS_CONTENT
        )

    train_model_full = request.form.get('train_model', '').strip()
    journey_date_str = request.form.get('date', '').strip()
    
    device_type, browser = get_user_device_info()
    logger.info(f"Train Report Request - Train: '{train_model_full}', Date: '{journey_date_str}' | Device: {device_type}, Browser: {browser}")

    if not train_model_full or not journey_date_str:
        session['error'] = "Both Train Name and Journey Date are required."
        return redirect(url_for('home'))

    try:
        date_obj = datetime.strptime(journey_date_str, '%d-%b-%Y')
        api_date_format = date_obj.strftime('%Y-%m-%d')
    except ValueError:
        session['error'] = "Invalid date format. Use DD-MMM-YYYY (e.g. 15-Nov-2024)."
        return redirect(url_for('home'))

    model_match = re.match(r'.*\((\d+)\)$', train_model_full)
    if model_match:
        train_model = model_match.group(1)
    else:
        train_model = train_model_full.split('(')[0].strip()

    try:
        form_values = {
            'train_model': train_model_full,
            'date': journey_date_str
        }
        session['form_values'] = form_values
        session['form_submitted'] = True
        session['can_generate_report'] = True
        session['can_access_report_page'] = True

        if CONFIG.get("queue_enabled", True):
            request_id = request_queue.add_request(
                process_report_request,
                {
                    'train_model': train_model,
                    'journey_date_str': journey_date_str,
                    'api_date_format': api_date_format,
                    'form_values': form_values,
                    'auth_token': request.form.get('auth_token', ''),
                    'device_key': request.form.get('device_key', '')
                }
            )
            
            session['queue_request_id'] = request_id
            return redirect(url_for('queue_wait'))
        else:
            auth_token = request.form.get('auth_token', '')
            device_key = request.form.get('device_key', '')
            result = process_report_request(train_model, journey_date_str, api_date_format, form_values, auth_token, device_key)
            
            if "error" in result:
                session['error'] = result["error"]
                return redirect(url_for('home'))
            
            result_id = str(uuid.uuid4())
            RESULT_CACHE[result_id] = result["result"]
            session['result_id'] = result_id
            session['pdf_filename'] = result.get("pdf_filename")
            return redirect(url_for('report_result'))
    except Exception as e:
        session['error'] = f"{str(e)}"
        return redirect(url_for('home'))
    
@app.route('/generate_report_api', methods=['POST'])
@require_valid_session
def generate_report_api():
    maintenance_response = check_maintenance()
    if maintenance_response:
        return jsonify({"success": False, "error": "Site is under maintenance"})
    
    try:
        existing_pdf = session.get('pdf_filename')
        if existing_pdf and os.path.exists(existing_pdf):
            device_type, browser = get_user_device_info()
            return jsonify({
                "success": True,
                "filename": existing_pdf,
                "message": "Report ready for download"
            })
        
        if not request.is_json:
            return jsonify({"success": False, "error": "Invalid request format"}), 400
        
        data = request.get_json()
        if not data:
            return jsonify({"success": False, "error": "No data provided"}), 400
        
        train_model = data.get('train_model')
        date = data.get('date')
        
        if not train_model or not date or train_model.strip() == '' or date.strip() == '':
            return jsonify({"success": False, "error": "Missing or empty train model or date"}), 400
        
        session_form_values = session.get('form_values', {})
        if (train_model != session_form_values.get('train_model') or 
            date != session_form_values.get('date')):
            return jsonify({
                "success": False, 
                "error": "Request data doesn't match your session. Please start over."
            }), 403
        
        model_match = re.match(r'.*\((\d+)\)$', train_model)
        if model_match:
            train_number = model_match.group(1)
        else:
            train_number = train_model.split('(')[0].strip()
        
        try:
            date_obj = datetime.strptime(date, '%d-%b-%Y')
            api_date_format = date_obj.strftime('%Y-%m-%d')
        except ValueError:
            return jsonify({"success": False, "error": "Invalid date format"}), 400
        
        device_type, browser = get_user_device_info()
        
        result = generate_report(train_number, api_date_format)
        
        if result['success']:
            session['pdf_filename'] = result['filename']
            return jsonify({
                "success": True,
                "filename": result['filename'],
                "message": result['message']
            })
        else:
            return jsonify({
                "success": False,
                "error": result['error']
            })
            
    except Exception as e:
        return jsonify({
            "success": False,
            "error": f"An error occurred while generating the report: {str(e)}"
        }), 500

@app.route('/download_report/<filename>')
def download_report(filename):
    maintenance_response = check_maintenance()
    if maintenance_response:
        return maintenance_response
    
    try:
        if (not filename.endswith('.pdf') or 
            '..' in filename or 
            '/' in filename or 
            '\\' in filename or
            len(filename) > 100):
            abort(404)
        
        session_filename = session.get('pdf_filename')
        if not session_filename or session_filename != filename:
            if not session.get('form_values'):
                abort(404)
            else:
                session['error'] = "Unauthorized file access. Please generate a new report."
                return redirect(url_for('report'))
        
        file_path = filename
        if not os.path.exists(file_path):
            if not session.get('form_values'):
                abort(404)
            else:
                session['error'] = "Report file not found or has expired. Please generate a new report."
                return redirect(url_for('home'))
        
        device_type, browser = get_user_device_info()
        
        response = send_file(
            file_path,
            as_attachment=True,
            download_name=filename,
            mimetype='application/pdf'
        )
        
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
        
        @response.call_on_close
        def remove_file():
            try:
                if os.path.exists(file_path):
                    os.remove(file_path)
                session.pop('pdf_filename', None)
            except Exception:
                pass
        
        return response
        
    except Exception as e:
        if not session.get('form_values'):
            abort(404)
        else:
            session['error'] = "An error occurred while downloading the report."
            return redirect(url_for('home'))

@app.route('/queue_stats')
def queue_stats():
    try:
        stats = request_queue.get_queue_stats()
        return jsonify(stats)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/queue_cleanup', methods=['POST'])
def queue_cleanup():
    try:
        request_queue.force_cleanup()
        stats = request_queue.get_queue_stats()
        return jsonify({"status": "success", "message": "Cleanup completed", "stats": stats})
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500

@app.route('/search_trains', methods=['GET', 'POST'])
def search_trains():
    maintenance_response = check_maintenance()
    if maintenance_response:
        return jsonify({"error": "Service under maintenance"}), 503
    
    if request.method == 'GET':
        abort(404)
    
    try:
        data = request.get_json()
        origin = data.get('origin', '').strip()
        destination = data.get('destination', '').strip()
        auth_token = data.get('auth_token', '').strip()
        device_key = data.get('device_key', '').strip()
        
        device_type, browser = get_user_device_info()
        
        if not origin or not destination:
            return jsonify({"error": "Both origin and destination are required"}), 400
        
        if not auth_token or not device_key:
            return jsonify({"error": "Authentication credentials are required"}), 400
    
        
        today = datetime.now()
        date1 = today + timedelta(days=8)
        date2 = today + timedelta(days=9)
        
        date1_str = date1.strftime('%d-%b-%Y')
        date2_str = date2.strftime('%d-%b-%Y')
        
        trains_day1 = fetch_trains_for_date(origin, destination, date1_str, auth_token, device_key)
        trains_day2 = fetch_trains_for_date(origin, destination, date2_str, auth_token, device_key)
        
        common_trains = get_common_trains(trains_day1, trains_day2)
        
        return jsonify({
            "success": True,
            "trains": common_trains,
            "dates": [date1_str, date2_str]
        })
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

def fetch_trains_for_date(origin, destination, date_str, auth_token, device_key):
    url = "https://railspaapi.shohoz.com/v1.0/web/bookings/search-trips-v2"
    params = {
        'from_city': origin,
        'to_city': destination,
        'date_of_journey': date_str,
        'seat_class': 'S_CHAIR'
    }
    headers = {
        "Authorization": f"Bearer {auth_token}",
        "x-device-key": device_key
    }
    
    max_retries = 2
    retry_count = 0
    
    while retry_count < max_retries:
        try:
            response = requests.get(url, headers=headers, params=params, timeout=10)
            
            if response.status_code == 401:
                error_msg = "Invalid or expired authentication credentials. Please update your Auth Token and Device Key."
                raise Exception(error_msg)
            
            if response.status_code == 403:
                raise Exception("Rate limit exceeded. Please try again later.")
                
            if response.status_code >= 500:
                retry_count += 1
                if retry_count == max_retries:
                    raise Exception("We're unable to connect to the Bangladesh Railway website right now. Please try again in a few minutes.")
                continue
                
            response.raise_for_status()
            
            data = response.json()
            trains = data.get('data', {}).get('trains', [])
            
            return trains
            
        except requests.RequestException as e:
            status_code = e.response.status_code if e.response is not None else None
            if status_code == 401:
                error_msg = "Invalid or expired authentication credentials. Please update your Auth Token and Device Key."
                raise Exception(error_msg)
                    
            if hasattr(e, 'response') and e.response and e.response.status_code == 403:
                raise Exception("Rate limit exceeded. Please try again later.")
            retry_count += 1
            if retry_count == max_retries:
                return []
    
    return []

def get_common_trains(trains_day1, trains_day2):
    all_trains = {}
    
    for train in trains_day1:
        trip_number = train.get('trip_number', '')
        if trip_number and trip_number not in all_trains:
            all_trains[trip_number] = {
                'trip_number': trip_number,
                'departure_time': train.get('departure_date_time', ''),
                'arrival_time': train.get('arrival_date_time', ''),
                'travel_time': train.get('travel_time', ''),
                'origin_city': train.get('origin_city_name', ''),
                'destination_city': train.get('destination_city_name', ''),
                'sort_time': extract_time_for_sorting(train.get('departure_date_time', ''))
            }
    
    for train in trains_day2:
        trip_number = train.get('trip_number', '')
        if trip_number and trip_number not in all_trains:
            all_trains[trip_number] = {
                'trip_number': trip_number,
                'departure_time': train.get('departure_date_time', ''),
                'arrival_time': train.get('arrival_date_time', ''),
                'travel_time': train.get('travel_time', ''),
                'origin_city': train.get('origin_city_name', ''),
                'destination_city': train.get('destination_city_name', ''),
                'sort_time': extract_time_for_sorting(train.get('departure_date_time', ''))
            }
    
    trains_list = list(all_trains.values())
    trains_list.sort(key=lambda x: x.get('sort_time', ''))
    
    for train in trains_list:
        train.pop('sort_time', None)
    
    return trains_list

def extract_time_for_sorting(departure_time_str):
    try:
        if not departure_time_str:
            return "99:99"
            
        time_part = departure_time_str.split(',')[-1].strip()
        
        if 'am' in time_part.lower():
            time_clean = time_part.lower().replace('am', '').strip()
            hour, minute = time_clean.split(':')
            hour = int(hour)
            if hour == 12:
                hour = 0
        elif 'pm' in time_part.lower():
            time_clean = time_part.lower().replace('pm', '').strip()
            hour, minute = time_clean.split(':')
            hour = int(hour)
            if hour != 12:
                hour += 12
        else:
            return "99:99"
            
        return f"{hour:02d}:{minute}"
        
    except Exception:
        return "99:99"

@app.errorhandler(404)
def page_not_found(e):
    maintenance_response = check_maintenance()
    if maintenance_response:
        return maintenance_response
    return render_template('404.html', styles_css=STYLES_CSS_CONTENT, script_js=SCRIPT_JS_CONTENT), 404

@app.route('/pdf_cleanup_stats')
def pdf_cleanup_stats():
    try:
        stats = pdf_cleanup_manager.get_cleanup_stats()
        return jsonify({
            "success": True,
            "cleanup_enabled": PDF_CLEANUP_ENABLED,
            "max_age_minutes": PDF_MAX_AGE_MINUTES,
            "cleanup_interval_minutes": PDF_CLEANUP_INTERVAL_MINUTES,
            "stats": stats
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/pdf_cleanup_manual', methods=['POST'])
def pdf_cleanup_manual():
    try:
        pdf_cleanup_manager.cleanup_old_pdfs()
        stats = pdf_cleanup_manager.get_cleanup_stats()
        return jsonify({
            "success": True,
            "message": "Manual cleanup completed",
            "stats": stats
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/pdf_list')
def pdf_list():
    try:
        pdf_files = []
        for pdf_file in glob.glob("*.pdf"):
            try:
                file_stat = os.stat(pdf_file)
                pdf_files.append({
                    "filename": pdf_file,
                    "size": file_stat.st_size,
                    "created": datetime.fromtimestamp(file_stat.st_ctime).isoformat(),
                    "modified": datetime.fromtimestamp(file_stat.st_mtime).isoformat(),
                    "age_minutes": (datetime.now() - datetime.fromtimestamp(file_stat.st_mtime)).total_seconds() / 60
                })
            except Exception:
                continue
        
        return jsonify({
            "success": True,
            "total_files": len(pdf_files),
            "files": pdf_files
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

if __name__ == "__main__":
    pdf_cleanup_manager.start_background_cleanup()
    
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5003)), debug=False)
else:
    if not app.debug:
        app.logger.setLevel(logging.INFO)
        app.logger.addHandler(logging.StreamHandler(sys.stdout))
    
    pdf_cleanup_manager.start_background_cleanup()