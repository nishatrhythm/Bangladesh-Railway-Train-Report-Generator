import requests, os, textwrap, json, pytz
from typing import Dict, List, Tuple
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib import colors
    from reportlab.lib.units import inch
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    REPORTLAB_AVAILABLE = True
except ImportError:
    REPORTLAB_AVAILABLE = False

API_BASE_URL = 'https://railspaapi.shohoz.com/v1.0'
SEAT_AVAILABILITY = {'AVAILABLE': 1, 'IN_PROCESS': 2}

BANGLA_COACH_ORDER = [
    "KA", "KHA", "GA", "GHA", "UMA", "CHA", "SCHA", "JA", "JHA", "NEO",
    "TA", "THA", "DA", "DHA", "TO", "THO", "DOA", "DANT", "XTR1", "XTR2", 
    "XTR3", "XTR4", "XTR5", "SLR", "STD"
]
COACH_INDEX = {coach: idx for idx, coach in enumerate(BANGLA_COACH_ORDER)}

TOKEN = None
TOKEN_TIMESTAMP = None

def set_token(token: str):
    global TOKEN, TOKEN_TIMESTAMP
    TOKEN = token
    TOKEN_TIMESTAMP = datetime.now(timezone.utc)

def sort_seat_number(seat: str) -> tuple:
    parts = seat.split('-')
    coach = parts[0]
    coach_order = COACH_INDEX.get(coach, len(BANGLA_COACH_ORDER) + 1)
    coach_fallback = coach if coach not in COACH_INDEX else ""
    
    if len(parts) == 2:
        try:
            return (coach_order, coach_fallback, int(parts[1]), '')
        except ValueError:
            return (coach_order, coach_fallback, 0, parts[1])
    elif len(parts) == 3:
        try:
            return (coach_order, coach_fallback, int(parts[2]), parts[1])
        except ValueError:
            return (coach_order, coach_fallback, 0, parts[1])
    
    return (len(BANGLA_COACH_ORDER) + 1, seat, 0, '')

def analyze_issued_tickets(data: Dict, auth_token: str, device_key: str) -> Dict:
    layout = data.get("data", {}).get("seatLayout", [])
    if not layout:
        return {}
    
    issued_seats = []
    
    for floor in layout:
        for row in floor["layout"]:
            for seat in row:
                if seat["seat_number"] and seat["ticket_type"] in [1, 3]:
                    issued_seats.append(seat["seat_number"])
    
    issued_seats_sorted = sorted(issued_seats, key=sort_seat_number)
    
    return {
        "issued_tickets": issued_seats_sorted,
        "count": len(issued_seats_sorted)
    }

def get_seat_layout_for_route(trip_id: str, trip_route_id: str, auth_token: str, device_key: str) -> Tuple[Dict, bool, str]:
    url = f"{API_BASE_URL}/app/bookings/seat-layout"
    headers = {
        "Authorization": f"Bearer {auth_token}",
        "x-device-key": device_key
    }
    params = {"trip_id": trip_id, "trip_route_id": trip_route_id}
    
    max_retries = 2
    retry_count = 0
    
    while retry_count < max_retries:
        try:
            response = requests.get(url, headers=headers, params=params)
            
            if response.status_code == 401:
                try:
                    error_data = response.json()
                    error_messages = error_data.get("error", {}).get("messages", [])
                    if isinstance(error_messages, list):
                        for msg in error_messages:
                            if "You are not authorized for this request" in msg or "Please login first" in msg:
                                return {}, True, "AUTH_DEVICE_KEY_EXPIRED"
                            elif "Invalid User Access Token!" in msg:
                                return {}, True, "AUTH_TOKEN_EXPIRED"
                    return {}, True, "AUTH_TOKEN_EXPIRED"
                except ValueError:
                    return {}, True, "AUTH_TOKEN_EXPIRED"
            
            if response.status_code == 403:
                return {}, True, "Currently we are experiencing high traffic. Please try again after some time."
            
            if response.status_code >= 500:
                retry_count += 1
                if retry_count == max_retries:
                    return {}, True, "We're unable to connect to the Bangladesh Railway website right now. Please try again in a few minutes."
                continue
            
            response.raise_for_status()
            data = response.json()
            return analyze_issued_tickets(data, auth_token, device_key), False, ""
            
        except requests.RequestException as e:
            status_code = e.response.status_code if e.response is not None else None
            if status_code == 401:
                try:
                    error_data = e.response.json()
                    error_messages = error_data.get("error", {}).get("messages", [])
                    if isinstance(error_messages, list):
                        for msg in error_messages:
                            if "You are not authorized for this request" in msg or "Please login first" in msg:
                                return {}, True, "AUTH_DEVICE_KEY_EXPIRED"
                            elif "Invalid User Access Token!" in msg:
                                return {}, True, "AUTH_TOKEN_EXPIRED"
                    return {}, True, "AUTH_TOKEN_EXPIRED"
                except ValueError:
                    return {}, True, "AUTH_TOKEN_EXPIRED"
            
            retry_count += 1
            if retry_count == max_retries:
                return {}, True, f"Failed to fetch seat layout: {str(e)}"
    
    return {}, True, "Maximum retries exceeded"

def normalize_city_name_for_comparison(city_name: str) -> str:
    return city_name.lower().replace("'", "")

def get_route_availability(from_city: str, to_city: str, date_str: str, target_model: str, auth_token: str, device_key: str) -> Dict:
    url = f"{API_BASE_URL}/app/bookings/search-trips-v2"
    params = {
        "from_city": from_city,
        "to_city": to_city,
        "date_of_journey": date_str,
        "seat_class": "SHULOV"
    }
    headers = {
        "Authorization": f"Bearer {auth_token}",
        "x-device-key": device_key
    }
    
    max_retries = 2
    retry_count = 0
    
    while retry_count < max_retries:
        try:
            response = requests.get(url, headers=headers, params=params)
            
            if response.status_code == 401:
                try:
                    error_data = response.json()
                    error_messages = error_data.get("error", {}).get("messages", [])
                    if isinstance(error_messages, list):
                        for msg in error_messages:
                            if "You are not authorized for this request" in msg or "Please login first" in msg:
                                raise Exception("AUTH_DEVICE_KEY_EXPIRED")
                            elif "Invalid User Access Token!" in msg:
                                raise Exception("AUTH_TOKEN_EXPIRED")
                    raise Exception("AUTH_TOKEN_EXPIRED")
                except ValueError:
                    raise Exception("AUTH_TOKEN_EXPIRED")
            
            if response.status_code == 403:
                return None
            
            if response.status_code == 422:
                return None
            
            if response.status_code >= 500:
                retry_count += 1
                if retry_count == max_retries:
                    return None
                continue
            
            response.raise_for_status()
            
            trains = response.json().get("data", {}).get("trains", [])
            for train in trains:
                if train.get("train_model") == target_model:
                    returned_origin = train.get("origin_city_name", "")
                    returned_destination = train.get("destination_city_name", "")
                    
                    if (normalize_city_name_for_comparison(from_city) == normalize_city_name_for_comparison(returned_origin) and
                        normalize_city_name_for_comparison(to_city) == normalize_city_name_for_comparison(returned_destination)):
                        return train
            
            return None
            
        except requests.RequestException as e:
            status_code = e.response.status_code if e.response is not None else None
            if status_code == 401:
                try:
                    error_data = e.response.json()
                    error_messages = error_data.get("error", {}).get("messages", [])
                    if isinstance(error_messages, list):
                        for msg in error_messages:
                            if "You are not authorized for this request" in msg or "Please login first" in msg:
                                raise Exception("AUTH_DEVICE_KEY_EXPIRED")
                            elif "Invalid User Access Token!" in msg:
                                raise Exception("AUTH_TOKEN_EXPIRED")
                    raise Exception("AUTH_TOKEN_EXPIRED")
                except ValueError:
                    raise Exception("AUTH_TOKEN_EXPIRED")
                    
            if hasattr(e, 'response') and e.response and e.response.status_code == 403:
                return None
            if retry_count == max_retries - 1:
                return None
            retry_count += 1

def fetch_train_data(model: str, departure_date: str) -> Dict:
    url = "https://railspaapi.shohoz.com/v1.0/web/train-routes"
    payload = {
        "model": model,
        "departure_date_time": departure_date
    }
    headers = {'Content-Type': 'application/json'}
    
    max_retries = 2
    retry_count = 0
    
    while retry_count < max_retries:
        try:
            response = requests.post(url, json=payload, headers=headers)
            if response.status_code == 403:
                raise Exception("Currently we are experiencing high traffic. Please try again after some time.")
            
            if response.status_code >= 500:
                retry_count += 1
                if retry_count == max_retries:
                    raise Exception("We're unable to connect to the Bangladesh Railway website right now. Please try again in a few minutes.")
                continue
            
            response.raise_for_status()
            return response.json().get('data')
        except requests.RequestException as e:
            if hasattr(e, 'response') and e.response and e.response.status_code == 403:
                raise Exception("Currently we are experiencing high traffic. Please try again after some time.")
            if retry_count == max_retries - 1:
                return None
            retry_count += 1

def process_single_route(trip_id: str, trip_route_id: str, from_station: str, to_station: str, seat_type: str, auth_token: str, device_key: str) -> Tuple[str, str, str, Dict]:
    result, has_error, error_message = get_seat_layout_for_route(trip_id, trip_route_id, auth_token, device_key)
    
    if has_error:
        return from_station, to_station, seat_type, {"error": error_message}
    
    return from_station, to_station, seat_type, result

class NumberedCanvas(canvas.Canvas):
    def __init__(self, *args, **kwargs):
        canvas.Canvas.__init__(self, *args, **kwargs)
        self._saved_page_states = []
        self.primary_green = colors.Color(0x00/255.0, 0x67/255.0, 0x47/255.0)
        
        try:
            try:
                pdfmetrics.getFont('PlusJakartaSans-Regular')
                self.page_font = 'PlusJakartaSans-Regular'
            except:
                self.page_font = 'Helvetica'
        except:
            self.page_font = 'Helvetica'

    def showPage(self):
        self._saved_page_states.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        num_pages = len(self._saved_page_states)
        for (page_num, page_state) in enumerate(self._saved_page_states):
            self.__dict__.update(page_state)
            self.draw_page_number(page_num + 1, num_pages)
            canvas.Canvas.showPage(self)
        canvas.Canvas.save(self)

    def draw_page_number(self, page_num, total_pages):
        self.setFont(self.page_font, 10)
        self.setFillColor(self.primary_green)
        
        # Enhanced page footer with professional styling
        page_text = f"Page {page_num} of {total_pages}"
        footer_text = f"Bangladesh Railway Report Generator  •  {page_text}  •  Generated on {datetime.now(pytz.timezone('Asia/Dhaka')).strftime('%d %B %Y')}"
        
        # Draw a line above the footer
        self.setStrokeColor(self.primary_green)
        self.setLineWidth(0.5)
        self.line(40, 45, A4[0] - 40, 45)
        
        # Draw the footer text
        self.setFont(self.page_font, 8)
        self.drawCentredString(A4[0]/2, 32, footer_text)

def create_route_summary_data(issued_matrices: Dict, fare_matrices: Dict, stations: List[str]) -> Dict:
    route_summary = {}
    
    seat_types_with_data = []
    for seat_type in ["S_CHAIR", "SHOVAN", "SNIGDHA", "F_SEAT", "F_CHAIR", "AC_S", "F_BERTH", "AC_B", "SHULOV", "AC_CHAIR"]:
        has_data = any(
            any(len(seats) > 0 for seats in from_routes.values())
            for from_routes in issued_matrices[seat_type].values()
        )
        if has_data:
            seat_types_with_data.append(seat_type)
    
    for from_station in stations:
        route_summary[from_station] = {}
        for to_station in stations:
            if from_station != to_station:
                route_summary[from_station][to_station] = {}
                
                for seat_type in seat_types_with_data:
                    count = 0
                    fare = 0
                    if (from_station in issued_matrices[seat_type] and 
                        to_station in issued_matrices[seat_type][from_station]):
                        count = len(issued_matrices[seat_type][from_station][to_station])
                    
                    if (from_station in fare_matrices[seat_type] and 
                        to_station in fare_matrices[seat_type][from_station]):
                        fare = fare_matrices[seat_type][from_station][to_station]
                    
                    route_summary[from_station][to_station][seat_type] = {
                        "count": count,
                        "fare": fare
                    }
    
    return route_summary, seat_types_with_data

def format_seat_list(seats: List[str], for_pdf: bool = False) -> str:
    if not seats:
        return "None"
    
    seat_str = ", ".join(seats)
    if for_pdf:
        wrapped_lines = textwrap.wrap(seat_str, width=60)
        return wrapped_lines
    else:
        wrapped = textwrap.fill(seat_str, width=60)
        return wrapped

def generate_pdf_report(issued_matrices: Dict, fare_matrices: Dict, stations: List[str], train_data: Dict, config: Dict):
    
    if not REPORTLAB_AVAILABLE:
        return
    
    try:
        font_regular_path = os.path.join("static/fonts", "PlusJakartaSans-Regular.ttf")
        font_bold_path = os.path.join("static/fonts", "PlusJakartaSans-Bold.ttf")
        font_bengali_path = os.path.join("static/fonts", "NotoSansBengali-Regular.ttf")
        
        pdfmetrics.registerFont(TTFont('PlusJakartaSans-Regular', font_regular_path))
        pdfmetrics.registerFont(TTFont('PlusJakartaSans-Bold', font_bold_path))
        pdfmetrics.registerFont(TTFont('NotoSansBengali-Regular', font_bengali_path))
        
        regular_font = 'PlusJakartaSans-Regular'
        bold_font = 'PlusJakartaSans-Bold'
        bengali_font = 'NotoSansBengali-Regular'
        use_taka_symbol = True
        
    except Exception:
        regular_font = 'Helvetica'
        bold_font = 'Helvetica-Bold'
        bengali_font = 'Helvetica'
        use_taka_symbol = False
    
    timestamp = datetime.now(pytz.timezone('Asia/Dhaka')).strftime("%Y%m%d_%H%M%S")
    filename = f"BDRAILWAY_ISSUED_TICKETS_REPORT_{config['train_model']}_{timestamp}.pdf"
    
    try:
        doc = SimpleDocTemplate(filename, pagesize=A4, 
                              rightMargin=40, leftMargin=40, 
                              topMargin=40, bottomMargin=40)
        
        # Professional color scheme using website primary color
        primary_green = colors.Color(0x00/255.0, 0x67/255.0, 0x47/255.0)  # Website primary color #006747
        secondary_green = colors.Color(0x28/255.0, 0x8A/255.0, 0x5C/255.0)  # Lighter green
        light_green = colors.Color(0xE8/255.0, 0xF5/255.0, 0xF0/255.0)  # Very light green
        accent_gray = colors.Color(0x6C/255.0, 0x75/255.0, 0x7D/255.0)  # Professional gray
        
        styles = getSampleStyleSheet()
        
        # Enhanced title style with professional design
        title_style = ParagraphStyle(
            'CustomTitle',
            parent=styles['Heading1'],
            fontSize=24,
            spaceAfter=10,
            spaceBefore=20,
            alignment=1,
            textColor=primary_green,
            fontName=bold_font,
            leading=28
        )
        
        # Subtitle style for train name
        subtitle_style = ParagraphStyle(
            'SubtitleStyle',
            parent=styles['Normal'],
            fontSize=16,
            spaceAfter=30,
            alignment=1,
            textColor=secondary_green,
            fontName=bold_font
        )
        
        heading_style = ParagraphStyle(
            'CustomHeading',
            parent=styles['Heading2'],
            fontSize=16,
            spaceAfter=10,
            spaceBefore=10,
            textColor=primary_green,
            fontName=bold_font,
            borderWidth=0,
            borderPadding=0,
            leftIndent=0
        )
        
        # Enhanced section title style with full width matching table width
        section_title_style = ParagraphStyle(
            'SectionTitleStyle',
            parent=styles['Heading2'],
            fontSize=18,
            spaceAfter=25,
            spaceBefore=20,
            textColor=primary_green,
            fontName=bold_font,
            alignment=1,
            leftIndent=0,
            rightIndent=0
        )
        
        info_style = ParagraphStyle(
            'InfoStyle',
            parent=styles['Normal'],
            fontSize=11,
            spaceAfter=8,
            leftIndent=10,
            fontName=regular_font,
            textColor=colors.black
        )
        
        story = []
        
        # Professional header without unsupported emojis
        story.append(Paragraph("BANGLADESH RAILWAY", title_style))
        story.append(Paragraph("ISSUED TICKETS REPORT", subtitle_style))

        
        # Professional disclaimer with better styling and full width
        disclaimer_style = ParagraphStyle(
            'DisclaimerStyle',
            parent=styles['Normal'],
            fontSize=9,
            spaceAfter=35,
            spaceBefore=5,
            textColor=colors.Color(0.7, 0.2, 0.2),
            borderWidth=1,
            borderColor=colors.Color(0.9, 0.7, 0.7),
            fontName=regular_font,
            alignment=1,
            backColor=colors.Color(1.0, 0.98, 0.98),
            borderPadding=8,
            leading=12,
            leftIndent=0,
            rightIndent=0
        )
        
        disclaimer_text = ("DISCLAIMER: This report is generated for informational purposes only. "
                          "Data accuracy is not guaranteed and is not officially affiliated with Bangladesh Railway. "
                          "Please verify all information independently before making any decisions.")
        
        # Create disclaimer as a table to ensure full width
        disclaimer_table = Table([[Paragraph(disclaimer_text, disclaimer_style)]], colWidths=[7.2*inch])
        disclaimer_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), colors.Color(1.0, 0.98, 0.98)),
            ('BORDER', (0, 0), (-1, -1), 1, colors.Color(0.9, 0.7, 0.7)),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('LEFTPADDING', (0, 0), (-1, -1), 8),
            ('RIGHTPADDING', (0, 0), (-1, -1), 8),
            ('TOPPADDING', (0, 0), (-1, -1), 8),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 8)
        ]))
        story.append(disclaimer_table)
        story.append(Spacer(1, 20))
        
        # Enhanced report information section
        github_url = "https://github.com/nishatrhythm/Bangladesh-Railway-Train-Report-Generator"
        github_link = f'<a href="{github_url}" color="blue">GitHub Repository</a>'

        website1_url = "https://seat.onrender.com"
        website1_link = f'<a href="{website1_url}" color="blue">Train Seat Matrix</a>'

        website2_url = "https://trainseat.onrender.com"
        website2_link = f'<a href="{website2_url}" color="blue">Train Seat Availability</a>'

        config_data = [
            ["Train Model", config['train_model']],
            ["Journey Date", config['date_of_journey']],
            ["Train Name", train_data['train_name']],
            ["Running Days", ', '.join(train_data['days'])],
            ["Total Stations", str(len(stations))],
            ["Report Generated", datetime.now(pytz.timezone('Asia/Dhaka')).strftime("%d %B %Y  |  %H:%M:%S")],
            ["Source Code", github_link],
            ["Utility Website 1", website1_link],
            ["Utility Website 2", website2_link]
        ]
        
        config_table_data = []
        for i, (key, value) in enumerate(config_data):
            if i >= 6:
                config_table_data.append([Paragraph(key, info_style), Paragraph(value, info_style)])
            else:
                config_table_data.append([Paragraph(key, info_style), Paragraph(str(value), info_style)])
        
        # Enhanced configuration table design
        config_table = Table(config_table_data, colWidths=[2.2*inch, 3.3*inch])
        config_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), light_green),
            ('TEXTCOLOR', (0, 0), (0, -1), primary_green),
            ('FONTNAME', (0, 0), (0, -1), bold_font),
            ('FONTNAME', (1, 0), (1, -1), regular_font),
            ('FONTSIZE', (0, 0), (-1, -1), 11),
            ('ALIGN', (0, 0), (0, -1), 'LEFT'),
            ('ALIGN', (1, 0), (1, -1), 'LEFT'),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('GRID', (0, 0), (-1, -1), 1, secondary_green),
            ('LEFTPADDING', (0, 0), (-1, -1), 15),
            ('RIGHTPADDING', (0, 0), (-1, -1), 15),
            ('TOPPADDING', (0, 0), (-1, -1), 10),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 10),
            ('ROWBACKGROUNDS', (0, 0), (-1, -1), [light_green, colors.white])
        ]))
        
        story.append(config_table)
        story.append(Spacer(1, 30))
        
        # Enhanced stations section
        story.append(Paragraph("TRAIN ROUTE STATIONS", heading_style))
        story.append(Spacer(1, 10))
        
        stations_data = []
        cols = 3
        for i in range(0, len(stations), cols):
            row = []
            for j in range(cols):
                if i + j < len(stations):
                    station_num = i + j + 1
                    station_name = stations[i + j]
                    row.append(f"{station_num:2d}. {station_name}")
                else:
                    row.append("")
            stations_data.append(row)
        
        stations_table = Table(stations_data, colWidths=[2.4*inch, 2.4*inch, 2.4*inch])
        stations_table.setStyle(TableStyle([
            ('FONTNAME', (0, 0), (-1, -1), regular_font),
            ('FONTSIZE', (0, 0), (-1, -1), 10),
            ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ('TOPPADDING', (0, 0), (-1, -1), 6),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
            ('LEFTPADDING', (0, 0), (-1, -1), 12),
            ('BACKGROUND', (0, 0), (-1, -1), light_green),
            ('GRID', (0, 0), (-1, -1), 0.5, secondary_green)
        ]))
        
        story.append(stations_table)
        story.append(PageBreak())
        
        
        # Enhanced overall summary section with full width header and proper centering
        summary_header_para = Paragraph("OVERALL SUMMARY", ParagraphStyle(
            'SummaryHeaderPara',
            parent=styles['Normal'],
            fontSize=18,
            fontName=bold_font,
            textColor=primary_green,
            alignment=1,
            leading=22
        ))
        
        section_header_table = Table([[summary_header_para]], colWidths=[7.2*inch], rowHeights=[50])
        section_header_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), light_green),
            ('BORDER', (0, 0), (-1, -1), 2, secondary_green),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('LEFTPADDING', (0, 0), (-1, -1), 15),
            ('RIGHTPADDING', (0, 0), (-1, -1), 15),
            ('TOPPADDING', (0, 0), (-1, -1), 15),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 15)
        ]))
        story.append(section_header_table)
        story.append(Spacer(1, 20))
        
        summary_data = [["Seat Type", "Available Routes", "Total Issued Tickets"]]
        
        seat_types = ["S_CHAIR", "SHOVAN", "SNIGDHA", "F_SEAT", "F_CHAIR", "AC_S", "F_BERTH", "AC_B", "SHULOV", "AC_CHAIR"]
        
        for seat_type in seat_types:
            routes_with_tickets = 0
            total_tickets = 0
            
            for from_routes in issued_matrices[seat_type].values():
                for seats in from_routes.values():
                    if seats:
                        routes_with_tickets += 1
                        total_tickets += len(seats)
            
            if routes_with_tickets > 0:
                summary_data.append([seat_type, str(routes_with_tickets), str(total_tickets)])
        
        if len(summary_data) > 1:
            summary_table = Table(summary_data, colWidths=[2.4*inch, 2.4*inch, 2.4*inch])
            summary_table.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), primary_green),
                ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
                ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
                ('FONTNAME', (0, 0), (-1, 0), bold_font),
                ('FONTNAME', (0, 1), (-1, -1), regular_font),
                ('FONTSIZE', (0, 0), (-1, 0), 12),
                ('FONTSIZE', (0, 1), (-1, -1), 11),
                ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
                ('TOPPADDING', (0, 0), (-1, -1), 10),
                ('BOTTOMPADDING', (0, 1), (-1, -1), 10),
                ('BACKGROUND', (0, 1), (-1, -1), light_green),
                ('GRID', (0, 0), (-1, -1), 1, secondary_green),
                ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                ('ROWBACKGROUNDS', (0, 1), (-1, -1), [light_green, colors.white])
            ]))
            story.append(summary_table)
        else:
            no_data_style = ParagraphStyle(
                'NoDataStyle',
                parent=styles['Normal'],
                fontSize=12,
                alignment=1,
                textColor=accent_gray,
                fontName=regular_font,
                spaceAfter=20,
                spaceBefore=20
            )
            story.append(Paragraph("No issued tickets found for any seat type.", no_data_style))

        story.append(PageBreak())
        
        
        route_summary, seat_types_with_data = create_route_summary_data(issued_matrices, fare_matrices, stations)
        
        # Enhanced route-wise summary section with full width header and proper centering
        route_header_para = Paragraph("ROUTE-WISE ISSUED TICKET SUMMARY", ParagraphStyle(
            'RouteHeaderPara',
            parent=styles['Normal'],
            fontSize=18,
            fontName=bold_font,
            textColor=primary_green,
            alignment=1,
            leading=22
        ))
        
        route_section_header_table = Table([[route_header_para]], colWidths=[7.2*inch], rowHeights=[50])
        route_section_header_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), light_green),
            ('BORDER', (0, 0), (-1, -1), 2, secondary_green),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('LEFTPADDING', (0, 0), (-1, -1), 15),
            ('RIGHTPADDING', (0, 0), (-1, -1), 15),
            ('TOPPADDING', (0, 0), (-1, -1), 15),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 15)
        ]))
        story.append(route_section_header_table)
        story.append(Spacer(1, 10))

        train_info_style = ParagraphStyle(
            'TrainInfoStyle',
            parent=styles['Normal'],
            fontSize=14,
            spaceAfter=5,
            alignment=1,
            textColor=primary_green,
            fontName=bold_font
        )
        
        route_info_style = ParagraphStyle(
            'RouteInfoStyle',
            parent=styles['Normal'],
            fontSize=11,
            spaceAfter=20,
            alignment=1,
            textColor=accent_gray,
            fontName=regular_font
        )
        
        story.append(Paragraph(f"{train_data['train_name']}", train_info_style))
        
        origin_station = stations[0] if stations else "Unknown"
        destination_station = stations[-1] if stations else "Unknown"
        route_text = f"{origin_station} → {destination_station}"
        story.append(Paragraph(route_text, route_info_style))
        
        if seat_types_with_data:
            table_headers = ["From Station", "To Station"] + seat_types_with_data
            route_table_data = []
            
            header_row = []
            for header in table_headers:
                header_row.append(Paragraph(header, ParagraphStyle('RouteHeaderStyle',
                                                                  parent=styles['Normal'],
                                                                  fontName=bold_font,
                                                                  fontSize=10,
                                                                  alignment=1,
                                                                  textColor=colors.white)))
            route_table_data.append(header_row)
            
            for from_station in stations:
                for to_station in stations:
                    if from_station != to_station and from_station in route_summary:
                        has_tickets = any(
                            route_summary[from_station][to_station][seat_type]["count"] > 0 
                            for seat_type in seat_types_with_data
                        )
                        
                        if has_tickets:
                            row = []
                            row.append(Paragraph(from_station, ParagraphStyle('RouteCellStyle',
                                                                             parent=styles['Normal'],
                                                                             fontSize=10,
                                                                             alignment=1,
                                                                             fontName=regular_font)))
                            row.append(Paragraph(to_station, ParagraphStyle('RouteCellStyle',
                                                                           parent=styles['Normal'],
                                                                           fontSize=10,
                                                                           alignment=1,
                                                                           fontName=regular_font)))
                            
                            for seat_type in seat_types_with_data:
                                count = route_summary[from_station][to_station][seat_type]["count"]
                                fare = route_summary[from_station][to_station][seat_type]["fare"]
                                if count > 0:
                                    if use_taka_symbol:
                                        count_text = f'<font size="10" color="#006747" face="{bold_font}">{count}</font><br/><font size="8" color="gray" face="{bengali_font}">৳</font><font size="8" color="gray" face="{regular_font}"> {fare:.0f}</font>'
                                    else:
                                        count_text = f'<font size="10" color="#006747" face="{bold_font}">{count}</font><br/><font size="8" color="gray" face="{regular_font}">BDT {fare:.0f}</font>'
                                    
                                    count_style = ParagraphStyle('RouteCountStyle',
                                                            parent=styles['Normal'],
                                                            fontSize=10,
                                                            alignment=1,
                                                            fontName=regular_font)
                                else:
                                    count_text = "—"
                                    count_style = ParagraphStyle('RouteCountStyle',
                                                            parent=styles['Normal'],
                                                            fontSize=10,
                                                            alignment=1,
                                                            fontName=regular_font,
                                                            textColor=accent_gray)
                                row.append(Paragraph(count_text, count_style))
                            
                            route_table_data.append(row)
            
            if len(route_table_data) > 1:
                num_seat_types = len(seat_types_with_data)
                station_col_width = 1.3 * inch
                seat_col_width = (7.2 * inch - 2 * station_col_width) / num_seat_types if num_seat_types > 0 else 0.5 * inch
                
                col_widths = [station_col_width, station_col_width] + [seat_col_width] * num_seat_types
                
                route_summary_table = Table(route_table_data, colWidths=col_widths, repeatRows=1)
                route_summary_table.setStyle(TableStyle([
                    ('BACKGROUND', (0, 0), (-1, 0), primary_green),
                    ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
                    ('FONTNAME', (0, 0), (-1, 0), bold_font),
                    ('FONTSIZE', (0, 0), (-1, 0), 10),
                    ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
                    ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                    
                    ('BACKGROUND', (0, 1), (-1, -1), colors.white),
                    ('FONTSIZE', (0, 1), (-1, -1), 10),
                    ('GRID', (0, 0), (-1, -1), 0.5, secondary_green),
                    ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, light_green]),
                    
                    ('LEFTPADDING', (0, 0), (-1, -1), 6),
                    ('RIGHTPADDING', (0, 0), (-1, -1), 6),
                    ('TOPPADDING', (0, 0), (-1, -1), 8),
                    ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
                ]))
                
                story.append(route_summary_table)
            else:
                story.append(Paragraph("No routes with issued tickets found.", no_data_style))
        else:
            story.append(Paragraph("No issued tickets found for any seat type.", no_data_style))
        
        story.append(PageBreak())
        
        
        # Enhanced detailed seat matrix sections
        for seat_type in seat_types:
            has_issued_tickets = any(
                any(len(seats) > 0 for seats in from_routes.values())
                for from_routes in issued_matrices[seat_type].values()
            )
            
            if has_issued_tickets:
                # Enhanced matrix title style with full width and proper centering
                matrix_header_para = Paragraph(f"ISSUED TICKETS — {seat_type}", ParagraphStyle(
                    'MatrixHeaderPara',
                    parent=styles['Normal'],
                    fontSize=18,
                    fontName=bold_font,
                    textColor=primary_green,
                    alignment=1,
                    leading=22
                ))
                
                matrix_title_table = Table([[matrix_header_para]], colWidths=[7.2*inch], rowHeights=[50])
                matrix_title_table.setStyle(TableStyle([
                    ('BACKGROUND', (0, 0), (-1, -1), light_green),
                    ('BORDER', (0, 0), (-1, -1), 2, secondary_green),
                    ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                    ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
                    ('LEFTPADDING', (0, 0), (-1, -1), 15),
                    ('RIGHTPADDING', (0, 0), (-1, -1), 15),
                    ('TOPPADDING', (0, 0), (-1, -1), 15),
                    ('BOTTOMPADDING', (0, 0), (-1, -1), 15)
                ]))
                
                story.append(matrix_title_table)
                story.append(Spacer(1, 20))
                
                table_data = []
                headers = ["From Station", "To Station", "Count", "Seat Numbers"]
                table_data.append([Paragraph(h, ParagraphStyle('HeaderStyle', 
                                                              parent=styles['Normal'],
                                                              fontName=bold_font,
                                                              fontSize=11,
                                                              alignment=1,
                                                              textColor=colors.white)) for h in headers])
                
                for from_city in stations:
                    for to_city in stations:
                        if from_city in issued_matrices[seat_type] and to_city in issued_matrices[seat_type][from_city]:
                            issued_seats = issued_matrices[seat_type][from_city][to_city]
                            if issued_seats:
                                seat_count = len(issued_seats)
                                seat_lines = format_seat_list(issued_seats, for_pdf=True)
                                
                                lines_per_page = 35
                                
                                for i in range(0, len(seat_lines), lines_per_page):
                                    chunk_lines = seat_lines[i:i + lines_per_page]
                                    seat_str = "\n".join(chunk_lines)
                                    
                                    from_para = Paragraph(
                                        from_city if i == 0 else f"{from_city} (cont.)",
                                        ParagraphStyle('CellStyle',
                                                       parent=styles['Normal'],
                                                       fontSize=10,
                                                       alignment=1,
                                                       fontName=regular_font)
                                    )
                                    to_para = Paragraph(
                                        to_city if i == 0 else f"{to_city} (cont.)",
                                        ParagraphStyle('CellStyle',
                                                       parent=styles['Normal'],
                                                       fontSize=10,
                                                       alignment=1,
                                                       fontName=regular_font)
                                    )
                                    count_para = Paragraph(
                                        str(seat_count) if i == 0 else "",
                                        ParagraphStyle('CellStyle',
                                                       parent=styles['Normal'],
                                                       fontSize=11,
                                                       alignment=1,
                                                       fontName=bold_font,
                                                       textColor=primary_green)
                                    )
                                    seats_para = Paragraph(
                                        seat_str,
                                        ParagraphStyle('SeatStyle',
                                                       parent=styles['Normal'],
                                                       fontSize=9,
                                                       alignment=0,
                                                       fontName=regular_font,
                                                       leading=11)
                                    )
                                    
                                    table_data.append([from_para, to_para, count_para, seats_para])
                
                if len(table_data) > 1:
                    col_widths = [1.4*inch, 1.4*inch, 0.8*inch, 3.6*inch]
                    
                    pdf_table = Table(table_data, colWidths=col_widths, repeatRows=1, splitByRow=True)
                    pdf_table.setStyle(TableStyle([
                        ('BACKGROUND', (0, 0), (-1, 0), primary_green),
                        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
                        ('ALIGN', (0, 0), (2, -1), 'CENTER'),
                        ('ALIGN', (3, 0), (3, -1), 'LEFT'),
                        ('FONTNAME', (0, 0), (-1, 0), bold_font),
                        ('FONTSIZE', (0, 0), (-1, 0), 11),
                        ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
                        ('TOPPADDING', (0, 0), (-1, 0), 12),
                        ('BACKGROUND', (0, 1), (-1, -1), colors.white),
                        ('GRID', (0, 0), (-1, -1), 1, secondary_green),
                        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
                        ('LEFTPADDING', (0, 0), (-1, -1), 10),
                        ('RIGHTPADDING', (0, 0), (-1, -1), 10),
                        ('TOPPADDING', (0, 1), (-1, -1), 8),
                        ('BOTTOMPADDING', (0, 1), (-1, -1), 8),
                        ('FONTNAME', (0, 1), (-1, -1), regular_font),
                        ('FONTSIZE', (0, 1), (2, -1), 10),
                        ('FONTSIZE', (3, 1), (3, -1), 9),
                        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, light_green])
                    ]))
                    
                    story.append(pdf_table)
                    story.append(Spacer(1, 30))
                
                story.append(PageBreak())
        
        doc.build(story, canvasmaker=NumberedCanvas)
        return filename
        
    except Exception:
        return None

def generate_report(train_model: str, date_of_journey: str, auth_token: str, device_key: str) -> Dict:
    try:
        try:
            date_obj = datetime.strptime(date_of_journey, "%Y-%m-%d")
            api_date_format = date_obj.strftime("%Y-%m-%d")
            api_search_date_format = date_obj.strftime("%d-%b-%Y")
        except ValueError:
            return {"success": False, "error": "Invalid date format"}
        
        train_data = fetch_train_data(train_model, api_date_format)
        
        if not train_data:
            return {"success": False, "error": f"No train data found for model {train_model}"}
        
        journey_day = date_obj.strftime("%a")
        journey_day_full = date_obj.strftime("%A")
        running_days = train_data.get('days', [])
        
        if journey_day not in running_days:
            return {"success": False, "error": f"{train_data['train_name']} does not run on {journey_day_full}."}
        
        stations = [route['city'] for route in train_data['routes']]
        station_dates = {}
        current_date = date_obj
        previous_time = None
        
        for route in train_data['routes']:
            station = route['city']
            dep_time_str = route.get('departure_time') or route.get('arrival_time')
            
            if dep_time_str:
                time_part = dep_time_str.split(' ')[0]
                am_pm = dep_time_str.split(' ')[1].lower()
                hour, minute = map(int, time_part.split(':'))
                
                if am_pm == "pm" and hour != 12:
                    hour += 12
                elif am_pm == "am" and hour == 12:
                    hour = 0
                    
                current_time = timedelta(hours=hour, minutes=minute)
                
                if previous_time is not None and current_time < previous_time:
                    current_date += timedelta(days=1)
                    
                previous_time = current_time
            
            station_dates[station] = current_date.strftime("%d-%b-%Y")
        
        seat_types = ["AC_B", "AC_S", "SNIGDHA", "F_BERTH", "F_SEAT", "F_CHAIR",
                      "S_CHAIR", "SHOVAN", "SHULOV", "AC_CHAIR"]
        
        issued_matrices = {seat_type: {} for seat_type in seat_types}
        fare_matrices = {seat_type: {} for seat_type in seat_types}

        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = []
            
            for i, from_city in enumerate(stations):
                for j, to_city in enumerate(stations):
                    if i < j:
                        route_date = station_dates[from_city]
                        future = executor.submit(get_route_availability, from_city, to_city, route_date, train_model, auth_token, device_key)
                        futures.append((future, from_city, to_city))
            
            for future, from_city, to_city in futures:
                route_train_data = future.result()
                
                if route_train_data:
                    for seat_type in route_train_data.get("seat_types", []):
                        seat_type_name = seat_type["type"]
                        
                        if seat_type_name in issued_matrices:
                            fare = float(seat_type["fare"])
                            vat_amount = float(seat_type["vat_amount"])
                            if seat_type_name in ["AC_B", "F_BERTH"]:
                                fare += 50
                            
                            if from_city not in fare_matrices[seat_type_name]:
                                fare_matrices[seat_type_name][from_city] = {}
                            fare_matrices[seat_type_name][from_city][to_city] = fare + vat_amount
                            
                            issued_info, has_error, error_msg = get_seat_layout_for_route(
                                seat_type["trip_id"], seat_type["trip_route_id"], auth_token, device_key
                            )
                            
                            if not has_error and issued_info.get("count", 0) > 0:
                                if from_city not in issued_matrices[seat_type_name]:
                                    issued_matrices[seat_type_name][from_city] = {}
                                issued_matrices[seat_type_name][from_city][to_city] = issued_info["issued_tickets"]
                            else:
                                if from_city not in issued_matrices[seat_type_name]:
                                    issued_matrices[seat_type_name][from_city] = {}
                                issued_matrices[seat_type_name][from_city][to_city] = []
        
        train_info = {
            'train_name': train_data.get('train_name', f"Train {train_model}"),
            'days': train_data.get('days', ['Daily']),
            'origin_city': stations[0] if stations else 'N/A',
            'destination_city': stations[-1] if stations else 'N/A'
        }
        
        config = {
            'train_model': train_model,
            'date_of_journey': date_of_journey
        }
        
        pdf_filename = generate_pdf_report(issued_matrices, fare_matrices, stations, train_info, config)
        
        return {
            "success": True,
            "filename": pdf_filename,
            "message": "Report generated successfully!"
        }
        
    except Exception as e:
        return {"success": False, "error": str(e)}