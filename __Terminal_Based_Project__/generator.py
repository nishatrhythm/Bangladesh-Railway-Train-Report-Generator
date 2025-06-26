import requests, os, textwrap
from typing import Dict, List, Tuple
from colorama import Fore, Style, init
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from tabulate import tabulate
from dotenv import load_dotenv

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

load_dotenv('.env')
init(autoreset=True)

API_BASE_URL = 'https://railspaapi.shohoz.com/v1.0'
SEAT_AVAILABILITY = {'AVAILABLE': 1, 'IN_PROCESS': 2}

def load_config():
    train_model = os.getenv("TRAIN_MODEL")
    date_of_journey = os.getenv("DATE_OF_JOURNEY")
    
    if not train_model:
        raise Exception("TRAIN_MODEL not configured in environment variables.")
    if not date_of_journey:
        raise Exception("DATE_OF_JOURNEY not configured in environment variables.")
    
    return {
        'train_model': train_model,
        'date_of_journey': date_of_journey
    }

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

def fetch_token() -> str:
    mobile_number = os.getenv("MOBILE_NUMBER")
    password = os.getenv("PASSWORD")
    
    if not mobile_number or not password:
        raise Exception("Mobile number or password not configured in environment.")
    
    url = f"{API_BASE_URL}/app/auth/sign-in"
    payload = {"mobile_number": mobile_number, "password": password}
    
    max_retries = 2
    retry_count = 0
    
    while retry_count < max_retries:
        try:
            response = requests.post(url, json=payload)
            if response.status_code == 403:
                raise Exception("Rate limit exceeded. Please try again later.")
            
            if response.status_code == 422:
                raise Exception("Invalid credentials. Please check mobile number and password.")
            
            if response.status_code >= 500:
                retry_count += 1
                if retry_count == max_retries:
                    raise Exception("We're unable to connect to the Bangladesh Railway website right now. Please try again in a few minutes.")
                continue
            
            response.raise_for_status()
            
            data = response.json()
            return data["data"]["token"]
        except requests.RequestException as e:
            if hasattr(e, 'response') and e.response and e.response.status_code == 403:
                raise Exception("Rate limit exceeded. Please try again later.")
            if retry_count == max_retries - 1:
                raise Exception(f"Failed to fetch token: {str(e)}")
            retry_count += 1

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

def analyze_issued_tickets(data: Dict) -> Dict:
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

def get_seat_layout_for_route(trip_id: str, trip_route_id: str) -> Tuple[Dict, bool, str]:
    global TOKEN
    
    if not TOKEN:
        TOKEN = fetch_token()
        set_token(TOKEN)
    
    url = f"{API_BASE_URL}/app/bookings/seat-layout"
    headers = {"Authorization": f"Bearer {TOKEN}"}
    params = {"trip_id": trip_id, "trip_route_id": trip_route_id}
    
    max_retries = 2
    retry_count = 0
    
    while retry_count < max_retries:
        try:
            response = requests.get(url, headers=headers, params=params)
            
            if response.status_code == 401:
                TOKEN = fetch_token()
                set_token(TOKEN)
                headers["Authorization"] = f"Bearer {TOKEN}"
                response = requests.get(url, headers=headers, params=params)
            
            if response.status_code == 403:
                return {}, True, "Rate limit exceeded. Please try again later."
            
            if response.status_code == 422:
                error_data = response.json()
                error_messages = error_data.get("error", {}).get("messages", [])
                error_msg = error_messages[0] if isinstance(error_messages, list) and error_messages else "Route not available"
                return {}, True, error_msg
            
            if response.status_code >= 500:
                retry_count += 1
                if retry_count == max_retries:
                    return {}, True, "We're unable to connect to the Bangladesh Railway website right now. Please try again in a few minutes."
                continue
            
            response.raise_for_status()
            data = response.json()
            
            issued_tickets_info = analyze_issued_tickets(data)
            return issued_tickets_info, False, ""
            
        except requests.RequestException as e:
            if hasattr(e, 'response') and e.response and e.response.status_code == 403:
                return {}, True, "Rate limit exceeded. Please try again later."
            if retry_count == max_retries - 1:
                return {}, True, f"Request failed: {str(e)}"
            retry_count += 1

def fetch_train_data(model: str, departure_date: str) -> Dict:
    global TOKEN
    
    if not TOKEN:
        TOKEN = fetch_token()
        set_token(TOKEN)
        
    url = f"{API_BASE_URL}/app/train-routes"
    payload = {
        "model": model,
        "departure_date_time": departure_date
    }
    headers = {'Content-Type': 'application/json', 'Authorization': f'Bearer {TOKEN}'}

    max_retries = 2
    retry_count = 0
    
    while retry_count < max_retries:
        try:
            response = requests.post(url, json=payload, headers=headers)
            if response.status_code == 403:
                raise Exception("Rate limit exceeded. Please try again later.")
            
            if response.status_code >= 500:
                retry_count += 1
                if retry_count == max_retries:
                    raise Exception("We're unable to connect to the Bangladesh Railway website right now. Please try again in a few minutes.")
                continue
            
            response.raise_for_status()
            return response.json().get('data')
        except requests.RequestException as e:
            if hasattr(e, 'response') and e.response and e.response.status_code == 403:
                raise Exception("Rate limit exceeded. Please try again later.")
            if retry_count == max_retries - 1:
                print(f"{Fore.RED}Failed to fetch train data: {str(e)}")
                return None
            retry_count += 1

def normalize_city_name_for_comparison(city_name: str) -> str:
    return city_name.lower().replace("'", "")

def get_route_availability(from_city: str, to_city: str, date_str: str, target_model: str) -> Dict:
    url = f"{API_BASE_URL}/app/bookings/search-trips-v2"
    params = {
        "from_city": from_city,
        "to_city": to_city,
        "date_of_journey": date_str,
        "seat_class": "SHULOV"
    }
    
    max_retries = 2
    retry_count = 0
    
    while retry_count < max_retries:
        try:
            response = requests.get(url, params=params)
            if response.status_code == 403:
                print(f"{Fore.YELLOW}Rate limit exceeded for {from_city} to {to_city}. Skipping...")
                return None
            
            if response.status_code >= 500:
                retry_count += 1
                if retry_count == max_retries:
                    print(f"{Fore.YELLOW}Server error for {from_city} to {to_city}. Skipping...")
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
            if hasattr(e, 'response') and e.response and e.response.status_code == 403:
                print(f"{Fore.YELLOW}Rate limit exceeded for {from_city} to {to_city}. Skipping...")
                return None
            if retry_count == max_retries - 1:
                print(f"{Fore.RED}Failed to fetch route data for {from_city} to {to_city}: {str(e)}")
                return None
            retry_count += 1

class NumberedCanvas(canvas.Canvas):
    def __init__(self, *args, **kwargs):
        canvas.Canvas.__init__(self, *args, **kwargs)
        self._saved_page_states = []
        self.custom_green = colors.Color(0x00/255.0, 0x67/255.0, 0x47/255.0)
        
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
        self.setFillColor(self.custom_green)
        page_text = f"Page {page_num} of {total_pages}"
        self.drawCentredString(A4[0]/2, 30, page_text)

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
        print(f"{Fore.RED}Cannot generate PDF: reportlab library not installed")
        print(f"{Fore.YELLOW}Install with: pip install reportlab")
        return
    
    try:
        font_regular_path = os.path.join("assets", "PlusJakartaSans-Regular.ttf")
        font_bold_path = os.path.join("assets", "PlusJakartaSans-Bold.ttf")
        font_bengali_path = os.path.join("assets", "NotoSansBengali-Regular.ttf")
        
        pdfmetrics.registerFont(TTFont('PlusJakartaSans-Regular', font_regular_path))
        pdfmetrics.registerFont(TTFont('PlusJakartaSans-Bold', font_bold_path))
        pdfmetrics.registerFont(TTFont('NotoSansBengali-Regular', font_bengali_path))
        
        regular_font = 'PlusJakartaSans-Regular'
        bold_font = 'PlusJakartaSans-Bold'
        bengali_font = 'NotoSansBengali-Regular'
        use_taka_symbol = True
        
        print(f"{Fore.GREEN}Custom fonts loaded successfully")
        
    except Exception as e:
        print(f"{Fore.YELLOW}Warning: Could not load custom fonts ({str(e)}). Using default fonts.")
        regular_font = 'Helvetica'
        bold_font = 'Helvetica-Bold'
        bengali_font = 'Helvetica'
        use_taka_symbol = False
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"BDRAILWAY_ISSUED_TICKETS_REPORT_{config['train_model']}_{timestamp}.pdf"
    print(f"\n{Fore.CYAN}Generating PDF report: {filename}")
    
    try:
        doc = SimpleDocTemplate(
            filename,
            pagesize=A4,
            rightMargin=50,
            leftMargin=50,
            topMargin=50,
            bottomMargin=70
        )
        custom_green = colors.Color(0x00/255.0, 0x67/255.0, 0x47/255.0)
        light_green = colors.Color(0xE8/255.0, 0xF5/255.0, 0xF0/255.0)
        
        styles = getSampleStyleSheet()
        
        title_style = ParagraphStyle(
            'CustomTitle',
            parent=styles['Heading1'],
            fontSize=18,
            spaceAfter=30,
            alignment=1,
            textColor=custom_green,
            fontName=bold_font
        )
        
        heading_style = ParagraphStyle(
            'CustomHeading',
            parent=styles['Heading2'],
            fontSize=14,
            spaceAfter=15,
            spaceBefore=10,
            textColor=custom_green,
            fontName=bold_font
        )
        
        matrix_title_style = ParagraphStyle(
            'MatrixTitleStyle',
            parent=styles['Heading2'],
            fontSize=16,
            spaceAfter=20,
            spaceBefore=10,
            textColor=custom_green,
            fontName=bold_font,
            alignment=1,
            borderWidth=2,
            borderColor=custom_green,
            borderPadding=10,
            backColor=light_green
        )
        
        info_style = ParagraphStyle(
            'InfoStyle',
            parent=styles['Normal'],
            fontSize=11,
            spaceAfter=8,
            leftIndent=20,
            fontName=regular_font
        )

        disclaimer_style = ParagraphStyle(
            'DisclaimerStyle',
            parent=styles['Normal'],
            fontSize=6.5,
            spaceAfter=45,
            spaceBefore=1,
            textColor=colors.red,
            borderWidth=0.5,
            borderColor=colors.red,
            fontName=regular_font,
            alignment=1,
            backColor=colors.Color(1.0, 0.95, 0.95)
        )

        currency_style = ParagraphStyle(
            'CurrencyStyle',
            parent=styles['Normal'],
            fontSize=6,
            alignment=1,
            fontName=bengali_font if use_taka_symbol else regular_font,
            textColor=colors.gray
        )
        
        story = []
        story.append(Paragraph("BANGLADESH RAILWAY ISSUED TICKETS REPORT", title_style))
        story.append(Spacer(1, 1))
        
        disclaimer_text = "The data in this report may not be fully accurate and not affiliated by Bangladesh Railway. Please verify and cross-check all information before using it."
        story.append(Paragraph(disclaimer_text, disclaimer_style))
        story.append(Spacer(1, 20))
        
        github_url = "https://github.com/nishatrhythm/Bangladesh-Railway-Train-Report-Generator"
        github_link = f'<a href="{github_url}" color="blue">GitHub Repository</a>'

        website1_url = "https://seat.onrender.com"
        website1_link = f'<a href="{website1_url}" color="blue">Seat Matrix</a>'

        website2_url = "https://trainseat.onrender.com"
        website2_link = f'<a href="{website2_url}" color="blue">Seat Availability</a>'

        config_data = [
            ["Train Model:", config['train_model']],
            ["Journey Date:", config['date_of_journey']],
            ["Train Name:", train_data['train_name']],
            ["Running Days:", ', '.join(train_data['days'])],
            ["Total Stations:", str(len(stations))],
            ["Report Generated:", datetime.now().strftime("%d-%b-%Y    %H:%M:%S")],
            ["Source Code:", github_link],
            ["Utility Website 1:", website1_link],
            ["Utility Website 2:", website2_link]
        ]
        
        config_table_data = []
        for i, (key, value) in enumerate(config_data):
            if i >= 6:
                config_table_data.append([key, Paragraph(value, info_style)])
            else:
                config_table_data.append([key, value])
        
        config_table = Table(config_table_data, colWidths=[2*inch, 3*inch])
        config_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), light_green),
            ('TEXTCOLOR', (0, 0), (0, -1), custom_green),
            ('FONTNAME', (0, 0), (0, -1), bold_font),
            ('FONTNAME', (1, 0), (1, -1), regular_font),
            ('FONTSIZE', (0, 0), (-1, -1), 11),
            ('ALIGN', (0, 0), (0, -1), 'RIGHT'),
            ('ALIGN', (1, 0), (1, -1), 'LEFT'),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('GRID', (0, 0), (-1, -1), 1, custom_green),
            ('LEFTPADDING', (0, 0), (-1, -1), 15),
            ('RIGHTPADDING', (0, 0), (-1, -1), 15),
            ('TOPPADDING', (0, 0), (-1, -1), 8),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 8)
        ]))
        
        story.append(config_table)
        story.append(Spacer(1, 30))
        
        story.append(Paragraph("Train Route Stations", heading_style))
        
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
        
        stations_table = Table(stations_data, colWidths=[2.3*inch, 2.3*inch, 2.3*inch])
        stations_table.setStyle(TableStyle([
            ('FONTNAME', (0, 0), (-1, -1), regular_font),
            ('FONTSIZE', (0, 0), (-1, -1), 10),
            ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ('TOPPADDING', (0, 0), (-1, -1), 4),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
            ('LEFTPADDING', (0, 0), (-1, -1), 10)
        ]))
        
        story.append(stations_table)
        story.append(PageBreak())
        
        story.append(Paragraph("OVERALL SUMMARY", title_style))
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
            summary_table = Table(summary_data, colWidths=[2.2*inch, 2.2*inch, 2.2*inch])
            summary_table.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), custom_green),
                ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
                ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
                ('FONTNAME', (0, 0), (-1, 0), bold_font),
                ('FONTNAME', (0, 1), (-1, -1), regular_font),
                ('FONTSIZE', (0, 0), (-1, 0), 12),
                ('FONTSIZE', (0, 1), (-1, -1), 11),
                ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
                ('TOPPADDING', (0, 0), (-1, -1), 8),
                ('BOTTOMPADDING', (0, 1), (-1, -1), 8),
                ('BACKGROUND', (0, 1), (-1, -1), light_green),
                ('GRID', (0, 0), (-1, -1), 1, custom_green),
                ('VALIGN', (0, 0), (-1, -1), 'MIDDLE')
            ]))
            story.append(summary_table)
        else:
            story.append(Paragraph("No issued tickets found for any seat type.", styles['Normal']))

        story.append(PageBreak())
        
        route_summary, seat_types_with_data = create_route_summary_data(issued_matrices, fare_matrices, stations)
        
        story.append(Paragraph("ROUTE-WISE ISSUED TICKET SUMMARY", title_style))
        story.append(Spacer(1, 5))

        train_info_style = ParagraphStyle(
            'TrainInfoStyle',
            parent=styles['Normal'],
            fontSize=12,
            spaceAfter=3,
            alignment=1,
            textColor=custom_green,
            fontName=bold_font
        )
        
        route_info_style = ParagraphStyle(
            'RouteInfoStyle',
            parent=styles['Normal'],
            fontSize=10,
            spaceAfter=15,
            alignment=1,
            textColor=colors.black,
            fontName=regular_font
        )
        
        story.append(Paragraph(train_data['train_name'], train_info_style))
        
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
                                                                fontSize=9,
                                                                alignment=1,
                                                                textColor=colors.white)))
            route_table_data.append(header_row)
            
            for from_station in stations:
                for to_station in stations:
                    if from_station != to_station and from_station in route_summary and to_station in route_summary[from_station]:
                        has_tickets = any(
                            route_summary[from_station][to_station][seat_type]["count"] > 0 
                            for seat_type in seat_types_with_data
                        )
                        
                        if has_tickets:
                            row = []
                            row.append(Paragraph(from_station, ParagraphStyle('RouteCellStyle',
                                                                            parent=styles['Normal'],
                                                                            fontSize=8,
                                                                            alignment=1,
                                                                            fontName=regular_font)))
                            row.append(Paragraph(to_station, ParagraphStyle('RouteCellStyle',
                                                                        parent=styles['Normal'],
                                                                        fontSize=8,
                                                                        alignment=1,
                                                                        fontName=regular_font)))
                            
                            for seat_type in seat_types_with_data:
                                count = route_summary[from_station][to_station][seat_type]["count"]
                                fare = route_summary[from_station][to_station][seat_type]["fare"]
                                if count > 0:
                                    if use_taka_symbol:
                                        count_text = f'<font size="8" color="black" face="{regular_font}">{count}</font><br/><font size="6" color="gray" face="{bengali_font}">৳</font><font size="6" color="gray" face="{regular_font}"> {fare:.0f}</font>'
                                    else:
                                        count_text = f'<font size="8" color="black" face="{regular_font}">{count}</font><br/><font size="6" color="gray" face="{regular_font}">BDT {fare:.0f}</font>'
                                    
                                    count_style = ParagraphStyle('RouteCountStyle',
                                                            parent=styles['Normal'],
                                                            fontSize=8,
                                                            alignment=1,
                                                            fontName=regular_font)
                                else:
                                    count_text = "—"
                                    count_style = ParagraphStyle('RouteCountStyle',
                                                            parent=styles['Normal'],
                                                            fontSize=8,
                                                            alignment=1,
                                                            fontName=regular_font,
                                                            textColor=colors.gray)
                                row.append(Paragraph(count_text, count_style))
                            route_table_data.append(row)
            
            if len(route_table_data) > 1:
                num_seat_types = len(seat_types_with_data)
                station_col_width = 1.2 * inch
                seat_col_width = (7 * inch - 2 * station_col_width) / num_seat_types if num_seat_types > 0 else 0.5 * inch
                
                col_widths = [station_col_width, station_col_width] + [seat_col_width] * num_seat_types
                
                route_summary_table = Table(route_table_data, colWidths=col_widths, repeatRows=1)
                route_summary_table.setStyle(TableStyle([
                    ('BACKGROUND', (0, 0), (-1, 0), custom_green),
                    ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
                    ('FONTNAME', (0, 0), (-1, 0), bold_font),
                    ('FONTSIZE', (0, 0), (-1, 0), 9),
                    ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
                    ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                    ('BACKGROUND', (0, 1), (-1, -1), colors.white),
                    ('FONTSIZE', (0, 1), (-1, -1), 8),
                    ('GRID', (0, 0), (-1, -1), 0.5, custom_green),
                    ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, light_green]),
                    ('LEFTPADDING', (0, 0), (-1, -1), 4),
                    ('RIGHTPADDING', (0, 0), (-1, -1), 4),
                    ('TOPPADDING', (0, 0), (-1, -1), 6),
                    ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
                ]))
                
                story.append(route_summary_table)
            else:
                story.append(Paragraph("No routes with issued tickets found.", styles['Normal']))
        else:
            story.append(Paragraph("No issued tickets found for any seat type.", styles['Normal']))
        
        story.append(PageBreak())
        
        for seat_type in seat_types:
            has_issued_tickets = any(
                any(len(seats) > 0 for seats in from_routes.values())
                for from_routes in issued_matrices[seat_type].values()
            )
            
            if has_issued_tickets:
                matrix_title = f"ISSUED TICKETS — {seat_type}"
                story.append(Paragraph(matrix_title, matrix_title_style))
                story.append(Spacer(1, 15))
                
                table_data = []
                headers = ["From Station", "To Station", "Count", "Seat Numbers"]
                table_data.append([Paragraph(h, ParagraphStyle('HeaderStyle', 
                                                              parent=styles['Normal'],
                                                              fontName=bold_font,
                                                              fontSize=10,
                                                              alignment=1,
                                                              textColor=colors.white)) for h in headers])
                
                for from_city in stations:
                    for to_city in stations:
                        if from_city in issued_matrices[seat_type] and to_city in issued_matrices[seat_type][from_city]:
                            issued_seats = issued_matrices[seat_type][from_city][to_city]
                            if issued_seats:
                                seat_count = len(issued_seats)
                                seat_lines = format_seat_list(issued_seats, for_pdf=True)
                                
                                lines_per_page = 40
                                
                                for i in range(0, len(seat_lines), lines_per_page):
                                    chunk_lines = seat_lines[i:i + lines_per_page]
                                    seat_str = "\n".join(chunk_lines)
                                    
                                    from_para = Paragraph(
                                        from_city if i == 0 else f"{from_city} (cont.)",
                                        ParagraphStyle('CellStyle',
                                                       parent=styles['Normal'],
                                                       fontSize=9,
                                                       alignment=1,
                                                       fontName=regular_font)
                                    )
                                    to_para = Paragraph(
                                        to_city if i == 0 else f"{to_city} (cont.)",
                                        ParagraphStyle('CellStyle',
                                                       parent=styles['Normal'],
                                                       fontSize=9,
                                                       alignment=1,
                                                       fontName=regular_font)
                                    )
                                    count_para = Paragraph(
                                        str(seat_count) if i == 0 else "",
                                        ParagraphStyle('CellStyle',
                                                       parent=styles['Normal'],
                                                       fontSize=9,
                                                       alignment=1,
                                                       fontName=bold_font)
                                    )
                                    seats_para = Paragraph(
                                        seat_str,
                                        ParagraphStyle('SeatStyle',
                                                       parent=styles['Normal'],
                                                       fontSize=8,
                                                       alignment=0,
                                                       fontName=regular_font,
                                                       leading=10)
                                    )
                                    
                                    table_data.append([from_para, to_para, count_para, seats_para])
                
                if len(table_data) > 1:
                    col_widths = [1.3*inch, 1.3*inch, 0.7*inch, 3.7*inch]
                    pdf_table = Table(table_data, colWidths=col_widths, repeatRows=1, splitByRow=True)
                    pdf_table.setStyle(TableStyle([
                        ('BACKGROUND', (0, 0), (-1, 0), custom_green),
                        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
                        ('ALIGN', (0, 0), (2, -1), 'CENTER'),
                        ('ALIGN', (3, 0), (3, -1), 'LEFT'),
                        ('FONTNAME', (0, 0), (-1, 0), bold_font),
                        ('FONTSIZE', (0, 0), (-1, 0), 10),
                        ('BOTTOMPADDING', (0, 0), (-1, 0), 10),
                        ('TOPPADDING', (0, 0), (-1, 0), 10),
                        ('BACKGROUND', (0, 1), (-1, -1), colors.white),
                        ('GRID', (0, 0), (-1, -1), 1, custom_green),
                        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
                        ('LEFTPADDING', (0, 0), (-1, -1), 8),
                        ('RIGHTPADDING', (0, 0), (-1, -1), 8),
                        ('TOPPADDING', (0, 1), (-1, -1), 6),
                        ('BOTTOMPADDING', (0, 1), (-1, -1), 6),
                        ('FONTNAME', (0, 1), (-1, -1), regular_font),
                        ('FONTSIZE', (0, 1), (2, -1), 9),
                        ('FONTSIZE', (3, 1), (3, -1), 8),
                        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, light_green])
                    ]))
                    
                    story.append(pdf_table)
                    story.append(Spacer(1, 25))
                
                story.append(PageBreak())
        
        doc.build(story, canvasmaker=NumberedCanvas)
        print(f"{Fore.GREEN}PDF report generated successfully: {filename}")
        print(f"{Fore.CYAN}Report saved in current directory: {os.path.abspath(filename)}")
        
    except Exception as e:
        print(f"{Fore.RED}Error generating PDF: {str(e)}")
        print(f"{Fore.YELLOW}Please ensure you have reportlab installed: pip install reportlab")

def main():
    print(f"{Fore.CYAN}Bangladesh Railway Issued Tickets Report{Style.RESET_ALL}")
    print("=" * 60)
    
    print(f"{Fore.YELLOW}IMPORTANT: Use a fresh railway account to ensure accurate results.")
    print(f"{Fore.YELLOW}Accounts with existing bookings may show inconsistent or incomplete data.{Style.RESET_ALL}")
    print("=" * 60)
    
    try:
        CONFIG = load_config()
    except Exception as e:
        print(f"{Fore.RED}Configuration Error: {str(e)}")
        print(f"{Fore.YELLOW}Please check your .env file and ensure TRAIN_MODEL and DATE_OF_JOURNEY are set.")
        return
    
    train_model = CONFIG['train_model']
    journey_date = CONFIG['date_of_journey']
    
    print(f"{Fore.GREEN}Configuration:")
    print(f"Train Model: {train_model}")
    print(f"Journey Date: {journey_date}")
    
    try:
        date_obj = datetime.strptime(journey_date, "%d-%b-%Y")
        api_date_format = date_obj.strftime("%Y-%m-%d")
    except ValueError:
        print(f"{Fore.RED}Invalid date format in CONFIG. Please use DD-MMM-YYYY format.")
        return
    
    print(f"\n{Fore.CYAN}Fetching train route information...")
    train_data = fetch_train_data(train_model, api_date_format)
    
    if not train_data:
        print(f"{Fore.RED}No train data found for model {train_model}")
        return
    
    journey_day = date_obj.strftime("%a")
    running_days = train_data.get('days', [])
    
    if journey_day not in running_days:
        print(f"\n{Fore.RED}ERROR: Train {train_data['train_name']} does not run on {journey_day} ({journey_date})")
        print(f"{Fore.YELLOW}This train runs on: {', '.join(running_days)}")
        print(f"{Fore.YELLOW}Please try another date when the train is running.")
        return
    
    stations = [route['city'] for route in train_data['routes']]
    station_dates = {}
    current_date = date_obj
    previous_time = None
    
    print(f"\n{Fore.GREEN}Train: {train_data['train_name']}")
    print(f"Model: {train_model}")
    print(f"Running Days: {', '.join(train_data['days'])}")
    print(f"{Fore.GREEN}Train runs on {journey_day} ({journey_date})")
    
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
    
    print(f"\n{Fore.CYAN}Route Stations ({len(stations)} stations):")
    for i, station in enumerate(stations, 1):
        print(f"{i:2d}. {station} ({station_dates[station]})")
    
    seat_types = ["AC_B", "AC_S", "SNIGDHA", "F_BERTH", "F_SEAT", "F_CHAIR",
                  "S_CHAIR", "SHOVAN", "SHULOV", "AC_CHAIR"]
    
    print(f"\n{Fore.CYAN}Fetching issued tickets data for all route combinations...")
    print(f"Total combinations to check: {len(stations) * (len(stations) - 1) // 2}")
    
    issued_matrices = {seat_type: {} for seat_type in seat_types}
    fare_matrices = {seat_type: {} for seat_type in seat_types}

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = []
        
        for i, from_city in enumerate(stations):
            for j, to_city in enumerate(stations):
                if i < j:
                    route_date = station_dates[from_city]
                    future = executor.submit(get_route_availability, from_city, to_city, route_date, train_model)
                    futures.append((future, from_city, to_city))
        
        completed = 0
        total = len(futures)
        
        for future, from_city, to_city in futures:
            route_train_data = future.result()
            completed += 1
            
            if completed % 10 == 0 or completed == total:
                print(f"Progress: {completed}/{total} combinations processed...")
            
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
                            seat_type["trip_id"], seat_type["trip_route_id"]
                        )
                        
                        if not has_error and issued_info.get("count", 0) > 0:
                            if from_city not in issued_matrices[seat_type_name]:
                                issued_matrices[seat_type_name][from_city] = {}
                            issued_matrices[seat_type_name][from_city][to_city] = issued_info["issued_tickets"]
                        else:
                            if from_city not in issued_matrices[seat_type_name]:
                                issued_matrices[seat_type_name][from_city] = {}
                            issued_matrices[seat_type_name][from_city][to_city] = []
    
    print(f"\n{Fore.GREEN}Data collection completed!")
    
    for seat_type in seat_types:
        has_issued_tickets = any(
            any(len(seats) > 0 for seats in from_routes.values())
            for from_routes in issued_matrices[seat_type].values()
        )
        
        if has_issued_tickets:
            print(f"\n{Fore.MAGENTA}{'='*80}")
            print(f"ISSUED TICKETS MATRIX - SEAT TYPE: {seat_type}")
            print(f"{'='*80}{Style.RESET_ALL}")
            
            table_data = []
            headers = ["From Station", "To Station", "Issued Seats Count", "All Seat Numbers"]
            
            for from_city in stations:
                for to_city in stations:
                    if from_city in issued_matrices[seat_type] and to_city in issued_matrices[seat_type][from_city]:
                        issued_seats = issued_matrices[seat_type][from_city][to_city]
                        if issued_seats:
                            seat_count = len(issued_seats)
                            seat_display = format_seat_list(issued_seats)
                            table_data.append([from_city, to_city, seat_count, seat_display])
            
            if table_data:
                print(f"{Fore.CYAN}Total routes with issued tickets: {len(table_data)}")
                try:
                    print(tabulate(table_data, headers=headers, tablefmt="grid", maxcolwidths=[15, 15, 10, 60]))
                    print(f"{Fore.GREEN}Table completed successfully for {seat_type}")
                except Exception as e:
                    print(f"{Fore.RED}Error displaying table for {seat_type}: {str(e)}")
                    for row in table_data:
                        print(f"{row[0]} -> {row[1]}: {row[2]} seats")
                        print(f"  Seats: {row[3]}")
                        print()
            else:
                print(f"{Fore.YELLOW}No issued tickets found for seat type {seat_type}")
    
    print(f"\n{Fore.CYAN}{'='*80}")
    print(f"ROUTE-WISE ISSUED TICKET SUMMARY")
    print(f"Train: {train_data['train_name']}")
    print(f"Route: {stations[0]} → {stations[-1]}")
    print(f"{'='*80}{Style.RESET_ALL}")
    
    route_summary, seat_types_with_data = create_route_summary_data(issued_matrices, fare_matrices, stations)
    
    if seat_types_with_data:
        terminal_headers = ["From Station", "To Station"] + seat_types_with_data
        terminal_table_data = []
        
        for from_station in stations:
            for to_station in stations:
                if from_station != to_station and from_station in route_summary:
                    has_tickets = any(
                        route_summary[from_station][to_station][seat_type]["count"] > 0 
                        for seat_type in seat_types_with_data
                    )
                    
                    if has_tickets:
                        row = [from_station, to_station]
                        
                        for seat_type in seat_types_with_data:
                            count = route_summary[from_station][to_station][seat_type]["count"]
                            fare = route_summary[from_station][to_station][seat_type]["fare"]
                            if count > 0:
                                count_text = f"{count}\n\u09F3 {fare:.0f}"
                            else:
                                count_text = "—"
                            row.append(count_text)
                        
                        terminal_table_data.append(row)
        
        if terminal_table_data:
            try:
                max_station_width = max(15, max(len(row[0]) for row in terminal_table_data + [terminal_headers]))
                max_to_width = max(15, max(len(row[1]) for row in terminal_table_data + [terminal_headers]))
                seat_widths = [8] * len(seat_types_with_data)
                
                maxcolwidths = [max_station_width, max_to_width] + seat_widths
                
                print(tabulate(terminal_table_data, headers=terminal_headers, tablefmt="grid", maxcolwidths=maxcolwidths))
                print(f"{Fore.GREEN}Route summary completed successfully")
            except Exception as e:
                print(f"{Fore.RED}Error displaying route summary table: {str(e)}")
                for row in terminal_table_data:
                    print(f"{row[0]} → {row[1]}: {', '.join(f'{seat_types_with_data[i]}={row[i+2]}' for i in range(len(seat_types_with_data)) if row[i+2] != '—')}")
        else:
            print(f"{Fore.YELLOW}No routes with issued tickets found.")
    else:
        print(f"{Fore.YELLOW}No issued tickets found for any seat type.")
    
    print(f"\n{Fore.CYAN}{'='*80}")
    print(f"OVERALL SUMMARY")
    print(f"{'='*80}{Style.RESET_ALL}")
    
    summary_data = []
    summary_headers = ["Seat Type", "Routes with Issued Tickets", "Total Issued Tickets"]
    
    for seat_type in seat_types:
        routes_with_tickets = 0
        total_tickets = 0
        
        for from_routes in issued_matrices[seat_type].values():
            for seats in from_routes.values():
                if seats:
                    routes_with_tickets += 1
                    total_tickets += len(seats)
        
        if routes_with_tickets > 0:
            summary_data.append([seat_type, routes_with_tickets, total_tickets])
    
    if summary_data:
        print(tabulate(summary_data, headers=summary_headers, tablefmt="grid"))
    else:
        print(f"{Fore.YELLOW}No issued tickets found for any seat type on any route.")
    
    print(f"\n{Fore.GREEN}Issued tickets matrix analysis completed!")
    print(f"{Fore.CYAN}Total train stations: {len(stations)}")
    print(f"Total route combinations analyzed: {len(stations) * (len(stations) - 1) // 2}")
    
    generate_pdf_report(issued_matrices, fare_matrices, stations, train_data, CONFIG)

if __name__ == "__main__":
    main()