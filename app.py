import os
import base64
import json
import random
import string
import secrets
from datetime import datetime, timedelta, timezone
from functools import wraps
import time
from io import BytesIO

from flask import (
    Flask, render_template, request, jsonify, 
    redirect, url_for, session, flash, send_file,
    make_response
)
from flask_sqlalchemy import SQLAlchemy
from flask_login import (
    LoginManager, UserMixin, login_user, 
    logout_user, login_required, current_user
)
from flask_socketio import SocketIO, emit
from flask_cors import CORS
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
import qrcode
from PIL import Image, ImageDraw, ImageFont
import requests
import jwt
from sqlalchemy import func, and_, or_
import pytz

# Optional PDF generation (install weasyprint)
try:
    from weasyprint import HTML, CSS
    from weasyprint.text.fonts import FontConfiguration
    WEASYPRINT_AVAILABLE = True
except ImportError:
    WEASYPRINT_AVAILABLE = False
    print("⚠️ WeasyPrint not installed. PDF generation will fail.")

# Initialize app
app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-secret-key-change-in-production')
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///school.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['UPLOAD_FOLDER'] = 'static/uploads'
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max file size
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=7)  # for parent sessions

# SMS Gate Configuration
SMSGATE_BASE_URL = os.getenv("SMSGATE_BASE_URL", "https://api.sms-gate.app")
SMSGATE_USERNAME = os.getenv("SMSGATE_USERNAME", "your_username")
SMSGATE_PASSWORD = os.getenv("SMSGATE_PASSWORD", "your_password")
TOKEN_CACHE = {"token": None, "expires_at": None}

# Initialize extensions
db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'
socketio = SocketIO(app, cors_allowed_origins="*")
CORS(app)

# Create upload folder
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

# Timezone for Bangladesh
bd_tz = pytz.timezone('Asia/Dhaka')

# ==================== DATABASE MODELS ====================

class User(UserMixin, db.Model):
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    role = db.Column(db.String(20), nullable=False)  # 'admin' or 'teacher'
    phone = db.Column(db.String(20))
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Relationships
    teacher_assignments = db.relationship(
        'TeacherAssignment', 
        foreign_keys='TeacherAssignment.teacher_id', 
        backref='teacher_user', 
        lazy=True
    )
    assigned_assignments = db.relationship(
        'TeacherAssignment', 
        foreign_keys='TeacherAssignment.assigned_by', 
        backref='assigner_user', 
        lazy=True
    )
    sent_messages = db.relationship('MessageLog', backref='sender', lazy=True)


    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class Student(db.Model):
    __tablename__ = 'students'
    id = db.Column(db.Integer, primary_key=True)
    uid = db.Column(db.String(8), unique=True, nullable=False, index=True)
    name = db.Column(db.String(100), nullable=False)
    class_level = db.Column(db.Integer, nullable=False)
    roll = db.Column(db.Integer, nullable=False)
    father_name = db.Column(db.String(100), nullable=False)
    father_phone = db.Column(db.String(15), nullable=False)
    mother_name = db.Column(db.String(100))
    mother_phone = db.Column(db.String(15))
    birth_date = db.Column(db.Date)
    blood_group = db.Column(db.String(5))
    address = db.Column(db.Text)
    photo_filename = db.Column(db.String(255))
    qr_token = db.Column(db.String(64), unique=True, nullable=False)
    qr_expiry = db.Column(db.DateTime, nullable=False)
    id_card_token = db.Column(db.String(32), unique=True)
    id_card_cvv = db.Column(db.String(4))
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    attendance_records = db.relationship('Attendance', backref='student', lazy=True)
    sms_logs = db.relationship('SMSLog', backref='student', lazy=True)
    # --- FIXED: use back_populates instead of backref ---
    results = db.relationship('Result', back_populates='student', lazy=True)

    # Parent relationship (many-to-many)
    parents = db.relationship('Parent', secondary='parent_student', back_populates='children')


# Association table for Parent <-> Student
parent_student = db.Table('parent_student',
    db.Column('parent_id', db.Integer, db.ForeignKey('parents.id'), primary_key=True),
    db.Column('student_id', db.Integer, db.ForeignKey('students.id'), primary_key=True),
    db.Column('relationship', db.String(20))  # 'father', 'mother', 'guardian'
)


class Parent(db.Model):
    __tablename__ = 'parents'
    id = db.Column(db.Integer, primary_key=True)
    phone = db.Column(db.String(15), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(256), nullable=False)
    name = db.Column(db.String(100))
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_login = db.Column(db.DateTime)

    # Relationships
    children = db.relationship('Student', secondary=parent_student, back_populates='parents')

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class Attendance(db.Model):
    __tablename__ = 'attendance'
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey('students.id'), nullable=False)
    date = db.Column(db.Date, nullable=False)
    check_in_time = db.Column(db.DateTime, nullable=False)
    check_out_time = db.Column(db.DateTime)
    status = db.Column(db.String(20), default='present')
    method = db.Column(db.String(10))
    teacher_id = db.Column(db.Integer, db.ForeignKey('users.id'))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (db.UniqueConstraint('student_id', 'date', name='unique_student_date'),)
    teacher = db.relationship('User', foreign_keys=[teacher_id])


class SMSLog(db.Model):
    __tablename__ = 'sms_logs'
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey('students.id'))
    phone = db.Column(db.String(15), nullable=False)
    message = db.Column(db.Text, nullable=False)
    message_type = db.Column(db.String(20))
    status = db.Column(db.String(20))
    message_id = db.Column(db.String(100))
    sent_at = db.Column(db.DateTime, default=datetime.utcnow)


class MessageLog(db.Model):
    __tablename__ = 'message_logs'
    id = db.Column(db.Integer, primary_key=True)
    sent_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    target_type = db.Column(db.String(20), nullable=False)
    target_value = db.Column(db.String(50))
    message = db.Column(db.Text, nullable=False)
    character_count = db.Column(db.Integer)
    delivery_status = db.Column(db.String(20), default='pending')
    sent_at = db.Column(db.DateTime, default=datetime.utcnow)


class TeacherAssignment(db.Model):
    __tablename__ = 'teacher_assignments'
    id = db.Column(db.Integer, primary_key=True)
    teacher_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    class_level = db.Column(db.Integer, nullable=False)
    can_take_attendance = db.Column(db.Boolean, default=True)
    assigned_by = db.Column(db.Integer, db.ForeignKey('users.id'))
    assigned_at = db.Column(db.DateTime, default=datetime.utcnow)

    teacher = db.relationship('User', foreign_keys=[teacher_id], backref='assigned_classes')
    assigner = db.relationship('User', foreign_keys=[assigned_by], backref='created_assignments')


class SystemSettings(db.Model):
    __tablename__ = 'system_settings'
    id = db.Column(db.Integer, primary_key=True)
    setting_key = db.Column(db.String(50), unique=True, nullable=False)
    setting_value = db.Column(db.Text)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class AuditLog(db.Model):
    __tablename__ = 'audit_logs'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'))
    action = db.Column(db.String(100), nullable=False)
    details = db.Column(db.Text)
    ip_address = db.Column(db.String(45))
    user_agent = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship('User', backref='audit_logs')


# ---------- RESULT MANAGEMENT MODELS ----------
class Subject(db.Model):
    __tablename__ = 'subjects'
    id = db.Column(db.Integer, primary_key=True)
    class_level = db.Column(db.Integer, nullable=False)
    subject_name = db.Column(db.String(50), nullable=False)
    full_marks = db.Column(db.Integer, default=100)
    pass_marks = db.Column(db.Integer, default=33)
    order = db.Column(db.Integer, default=0)

    __table_args__ = (db.UniqueConstraint('class_level', 'subject_name', name='unique_class_subject'),)


class Exam(db.Model):
    __tablename__ = 'exams'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    year = db.Column(db.Integer, nullable=False)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (db.UniqueConstraint('name', 'year', name='unique_exam'),)


class Result(db.Model):
    __tablename__ = 'results'
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey('students.id'), nullable=False)
    exam_id = db.Column(db.Integer, db.ForeignKey('exams.id'), nullable=False)
    class_level = db.Column(db.Integer, nullable=False)
    roll = db.Column(db.Integer, nullable=False)
    subject_marks = db.Column(db.JSON, nullable=False, default={})
    total_marks = db.Column(db.Float)
    obtained_marks = db.Column(db.Float)
    percentage = db.Column(db.Float)
    grade = db.Column(db.String(5))
    position = db.Column(db.Integer)
    published = db.Column(db.Boolean, default=False)
    published_at = db.Column(db.DateTime)
    download_token = db.Column(db.String(64), unique=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # --- FIXED: use back_populates instead of backref ---
    student = db.relationship('Student', back_populates='results')
    exam = db.relationship('Exam', backref='results')
# -------------------------------------------------------

# ==================== HELPER FUNCTIONS ====================

def get_bangladesh_time():
    utc_now = datetime.now(timezone.utc)
    return utc_now.astimezone(bd_tz)

def generate_uid():
    while True:
        uid = ''.join(random.choices(string.digits, k=8))
        if not Student.query.filter_by(uid=uid).first():
            return uid

def generate_qr_token():
    return secrets.token_urlsafe(32)

def generate_cvv():
    return ''.join(random.choices(string.digits, k=4))

def format_phone_e164(phone):
    if not phone:
        return None
    phone = ''.join(filter(str.isdigit, phone))
    if phone.startswith("01") and len(phone) == 11:
        return f"+880{phone[1:]}"
    elif phone.startswith("880") and len(phone) == 13:
        return f"+{phone}"
    elif len(phone) == 10:
        return f"+880{phone}"
    return None

def get_jwt_token():
    global TOKEN_CACHE
    if TOKEN_CACHE["token"] and TOKEN_CACHE["expires_at"]:
        if datetime.now(timezone.utc) < TOKEN_CACHE["expires_at"]:
            return TOKEN_CACHE["token"], None
    auth_string = f"{SMSGATE_USERNAME}:{SMSGATE_PASSWORD}"
    auth_encoded = base64.b64encode(auth_string.encode()).decode()
    headers = {
        "Authorization": f"Basic {auth_encoded}",
        "Content-Type": "application/json"
    }
    payload = {
        "scopes": ["messages:send", "messages:read"],
        "ttl": 3600
    }
    try:
        response = requests.post(
            f"{SMSGATE_BASE_URL}/3rdparty/v1/auth/token",
            headers=headers,
            json=payload,
            timeout=20
        )
        if response.status_code in (200, 201):
            data = response.json()
            token = data.get("access_token")
            TOKEN_CACHE["token"] = token
            TOKEN_CACHE["expires_at"] = datetime.now(timezone.utc) + timedelta(seconds=3500)
            return token, None
        return None, f"HTTP {response.status_code}: {response.text}"
    except Exception as e:
        return None, str(e)

def send_sms_via_smsgate(phone_numbers, message, retry=1):
    token, error = get_jwt_token()
    if error:
        return {"success": False, "error": error, "results": []}
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json"
    }
    results = []
    for phone in phone_numbers:
        formatted_phone = format_phone_e164(phone)
        if not formatted_phone:
            results.append({
                "phone": phone,
                "success": False,
                "status": "Invalid",
                "error": "Invalid phone format"
            })
            continue
        payload = {
            "textMessage": {
                "text": message
            },
            "phoneNumbers": [formatted_phone]
        }
        try:
            response = requests.post(
                f"{SMSGATE_BASE_URL}/3rdparty/v1/messages",
                headers=headers,
                json=payload,
                timeout=30
            )
            if response.status_code in (200, 201, 202):
                status = "Processing" if response.status_code == 202 else "Sent"
                response_data = response.json()
                message_id = response_data.get("messageId") or response_data.get("id")
                results.append({
                    "phone": formatted_phone,
                    "success": True,
                    "status": status,
                    "response": response_data,
                    "message_id": message_id
                })
            else:
                if retry > 0 and response.status_code in [500, 502, 503, 504, 429]:
                    time.sleep(2)
                    retry_results = send_sms_via_smsgate([phone], message, retry-1)
                    results.extend(retry_results["results"])
                else:
                    results.append({
                        "phone": phone,
                        "success": False,
                        "status": "Failed",
                        "error": f"HTTP {response.status_code}"
                    })
        except requests.exceptions.RequestException as e:
            if retry > 0:
                time.sleep(2)
                retry_results = send_sms_via_smsgate([phone], message, retry-1)
                results.extend(retry_results["results"])
            else:
                results.append({
                    "phone": phone,
                    "success": False,
                    "status": "NetworkError",
                    "error": str(e)
                })
    return {"success": any(r["success"] for r in results), "results": results}

def generate_qr_code(student):
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=10,
        border=4,
    )
    qr_data = f"{request.host_url}a/{student.qr_token}"
    qr.add_data(qr_data)
    qr.make(fit=True)
    qr_img = qr.make_image(fill_color="black", back_color="white")
    img_io = BytesIO()
    qr_img.save(img_io, 'PNG')
    img_io.seek(0)
    return img_io

def get_sms_templates():
    return {
        'present': {
            'bn': 'উপস্থিতি নোটিশ: আপনার সন্তান {NAME}, শ্রেণি {CLASS}, আজ {TIME} টায় উপস্থিত রয়েছে। — ভরিলহাট নেছারিয়া দাখিল মাদ্রাসা',
            'en': 'Attendance Notice: Your child {NAME}, Class {CLASS}, is PRESENT today at {TIME}. — Bhorilhat Nesaria Dakhil Madrasha'
        },
        'absent': {
            'bn': 'উপস্থিতি নোটিশ: আপনার সন্তান {NAME}, শ্রেণি {CLASS}, আজ অনুপস্থিত। ভুল হলে মাদ্রাসায় যোগাযোগ করুন। — ভরিলহাট নেছারিয়া দাখিল মাদ্রাসা',
            'en': 'Attendance Notice: Your child {NAME}, Class {CLASS}, is ABSENT today. Please contact the madrasha if this is an error. — Bhorilhat Nesaria Dakhil Madrasha'
        },
        'id_card': {
            'bn': 'আইডি কার্ড প্রস্তুত: আপনার সন্তানের আইডি কার্ড প্রস্তুত। ডাউনলোড করুন: {LINK} — CVV: {CVV} — ভরিলহাট নেছারিয়া দাখিল মাদ্রাসা',
            'en': 'ID Card Ready: Your child\'s ID card is ready. Download: {LINK} — CVV: {CVV} — Bhorilhat Nesaria Dakhil Madrasha'
        }
    }

def can_take_attendance(teacher_id, class_level):
    assignment = TeacherAssignment.query.filter_by(
        teacher_id=teacher_id,
        class_level=class_level,
        can_take_attendance=True
    ).first()
    return assignment is not None

def is_attendance_system_active():
    setting = SystemSettings.query.filter_by(setting_key='attendance_system_active').first()
    return setting and setting.setting_value == 'true'

def log_audit(user_id, action, details=None):
    audit = AuditLog(
        user_id=user_id,
        action=action,
        details=details,
        ip_address=request.remote_addr,
        user_agent=request.user_agent.string if request.user_agent else None
    )
    db.session.add(audit)
    db.session.commit()

def should_show_memorial_popup():
    if 'memorial_shown' not in session:
        session['memorial_shown'] = True
        return True
    return False

# ---------- RESULT HELPERS ----------
def calculate_grade(percentage):
    if percentage >= 80:
        return 'A+'
    elif percentage >= 70:
        return 'A'
    elif percentage >= 60:
        return 'A-'
    elif percentage >= 50:
        return 'B'
    elif percentage >= 40:
        return 'C'
    elif percentage >= 33:
        return 'D'
    else:
        return 'F'

def generate_result_token():
    return secrets.token_urlsafe(32)

def generate_marksheet_pdf(student, result, exam):
    if not WEASYPRINT_AVAILABLE:
        return None
    html = render_template('marksheet_pdf.html',
                          student=student,
                          result=result,
                          exam=exam,
                          now=get_bangladesh_time())
    font_config = FontConfiguration()
    pdf = HTML(string=html).write_pdf(
        stylesheets=[CSS(string='@page { size: A4; margin: 1.5cm; }')],
        font_config=font_config
    )
    return pdf

# ---------- PARENT PORTAL HELPERS ----------
def create_or_update_parent(phone, student, relationship='father'):
    """Create a parent account if not exists, link to student, and send credentials."""
    if not phone:
        return None

    parent = Parent.query.filter_by(phone=phone).first()
    if not parent:
        # Generate a random 6-digit password
        temp_password = ''.join(random.choices(string.digits, k=6))
        parent = Parent(
            phone=phone,
            name=student.father_name if relationship == 'father' else student.mother_name
        )
        parent.set_password(temp_password)
        db.session.add(parent)
        db.session.flush()  # get parent.id

        # Send SMS with credentials
        message = f'''আপনার অভিভাবক অ্যাকাউন্ট তৈরি হয়েছে।
লগইন: {phone}
পাসওয়ার্ড: {temp_password}
লগইন লিংক: {request.host_url}parent/login
ধন্যবাদ - ভরিলহাট নেছারিয়া দাখিল মাদ্রাসা'''
        send_sms_via_smsgate([phone], message)

    # Link to student if not already linked
    if student not in parent.children:
        parent.children.append(student)
        # Optionally store the relationship
        stmt = parent_student.update().where(
            and_(
                parent_student.c.parent_id == parent.id,
                parent_student.c.student_id == student.id
            )
        ).values(relationship=relationship)
        db.session.execute(stmt)
        db.session.commit()

    return parent
# ------------------------------------------------------

# ==================== FLASK-LOGIN CONFIG ====================

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# ==================== CONTEXT PROCESSOR ====================
@app.context_processor
def utility_processor():
    """Make get_bangladesh_time and now available in all templates."""
    now = get_bangladesh_time()
    return dict(
        get_bangladesh_time=get_bangladesh_time,
        now=now,
        bd_time=now
    )

# ==================== DECORATORS ====================

def admin_required(f):
    @wraps(f)
    @login_required
    def decorated_function(*args, **kwargs):
        if current_user.role != 'admin':
            flash('Admin access required', 'error')
            return redirect(url_for('dashboard'))
        return f(*args, **kwargs)
    return decorated_function

def teacher_required(f):
    @wraps(f)
    @login_required
    def decorated_function(*args, **kwargs):
        if current_user.role != 'teacher':
            flash('Teacher access required', 'error')
            return redirect(url_for('dashboard'))
        return f(*args, **kwargs)
    return decorated_function

def parent_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'parent_id' not in session:
            flash('অভিভাবক হিসেবে লগইন করুন', 'error')
            return redirect(url_for('parent_login'))
        return f(*args, **kwargs)
    return decorated_function

# ==================== EXISTING ROUTES ====================

@app.route('/')
def index():
    show_memorial = should_show_memorial_popup()
    return render_template('index.html', show_memorial_popup=show_memorial)

@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')
        user = User.query.filter_by(email=email, is_active=True).first()
        if user and user.check_password(password):
            login_user(user)
            log_audit(user.id, 'login', f'User logged in from {request.remote_addr}')
            session['user_role'] = user.role
            session['user_name'] = user.name
            next_page = request.args.get('next')
            return redirect(next_page or url_for('dashboard'))
        flash('Invalid email or password', 'error')
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    log_audit(current_user.id, 'logout')
    logout_user()
    session.clear()
    return redirect(url_for('index'))

@app.route('/dashboard')
@login_required
def dashboard():
    today = get_bangladesh_time().date()
    if current_user.role == 'admin':
        total_students = Student.query.filter_by(is_active=True).count()
        total_teachers = User.query.filter_by(role='teacher', is_active=True).count()
        today_attendance = Attendance.query.filter_by(date=today).count()
        attendance_by_class = db.session.query(
            Student.class_level,
            func.count(Attendance.id).label('present_count')
        ).join(Attendance, Student.id == Attendance.student_id)\
         .filter(Attendance.date == today)\
         .group_by(Student.class_level).all()
        
        # Get total students per class for attendance percentage
        class_totals = {}
        for class_level in range(1,13):
            count = Student.query.filter_by(class_level=class_level, is_active=True).count()
            class_totals[class_level] = count
        
        active_exams_count = Exam.query.filter_by(is_active=True).count()
        
        return render_template('admin_dashboard.html',
                             total_students=total_students,
                             total_teachers=total_teachers,
                             today_attendance=today_attendance,
                             attendance_by_class=attendance_by_class,
                             class_totals=class_totals,
                             active_exams_count=active_exams_count)
    else:
        assignments = TeacherAssignment.query.filter_by(
            teacher_id=current_user.id,
            can_take_attendance=True
        ).all()
        assigned_classes = [a.class_level for a in assignments]
        today_attendance = Attendance.query.filter(
            Attendance.date == today,
            Attendance.teacher_id == current_user.id
        ).count()
        
        # Get student count per class for teacher dashboard
        class_students_count = {}
        for cls in assigned_classes:
            count = Student.query.filter_by(class_level=cls, is_active=True).count()
            class_students_count[cls] = count
        
        return render_template('teacher_dashboard.html',
                             assigned_classes=assigned_classes,
                             today_attendance=today_attendance,
                             class_students_count=class_students_count)

@app.route('/attendance/scan', methods=['GET', 'POST'])
@teacher_required
def attendance_scan():
    if not is_attendance_system_active():
        return render_template('attendance_scan.html', system_active=False)
    if request.method == 'POST':
        if 'qr_image' in request.files:
            file = request.files['qr_image']
            if file.filename != '':
                pass
        uid = request.form.get('uid')
        if uid:
            student = Student.query.filter_by(uid=uid, is_active=True).first()
            if student:
                if not can_take_attendance(current_user.id, student.class_level):
                    return jsonify({'success': False, 'error': 'Not authorized for this class'})
                today = get_bangladesh_time().date()
                existing = Attendance.query.filter_by(
                    student_id=student.id,
                    date=today
                ).first()
                if existing:
                    return jsonify({'success': False, 'error': 'Already marked today'})
                now = get_bangladesh_time()
                attendance = Attendance(
                    student_id=student.id,
                    date=today,
                    check_in_time=now,
                    method='manual',
                    teacher_id=current_user.id
                )
                db.session.add(attendance)
                db.session.commit()
                template = get_sms_templates()['present']['bn']
                message = template.format(
                    NAME=student.name,
                    CLASS=student.class_level,
                    TIME=now.strftime('%I:%M %p')
                )
                sms_result = send_sms_via_smsgate([student.father_phone], message)
                sms_log = SMSLog(
                    student_id=student.id,
                    phone=student.father_phone,
                    message=message,
                    message_type='present',
                    status='sent' if sms_result['success'] else 'failed'
                )
                db.session.add(sms_log)
                db.session.commit()
                log_audit(current_user.id, 'mark_attendance', 
                         f'Student: {student.name}, UID: {student.uid}')
                return jsonify({
                    'success': True,
                    'student': {
                        'name': student.name,
                        'class': student.class_level,
                        'roll': student.roll,
                        'time': now.strftime('%I:%M %p')
                    }
                })
            return jsonify({'success': False, 'error': 'Student not found'})
    return render_template('attendance_scan.html', system_active=True)

@app.route('/a/<token>')
def process_qr(token):
    student = Student.query.filter_by(qr_token=token, is_active=True).first()
    if not student:
        return jsonify({'error': 'Invalid QR code'}), 404
    if student.qr_expiry < datetime.utcnow():
        return jsonify({'error': 'QR code expired'}), 410
    if not is_attendance_system_active():
        return jsonify({'error': 'Attendance system is not active'}), 403
    teacher_id = request.args.get('teacher_id')
    if not teacher_id and current_user.is_authenticated and current_user.role == 'teacher':
        teacher_id = current_user.id
    if teacher_id:
        if not can_take_attendance(teacher_id, student.class_level):
            return jsonify({'error': 'Not authorized'}), 403
    today = get_bangladesh_time().date()
    existing = Attendance.query.filter_by(
        student_id=student.id,
        date=today
    ).first()
    if existing:
        return jsonify({'error': 'Already marked today'}), 409
    now = get_bangladesh_time()
    attendance = Attendance(
        student_id=student.id,
        date=today,
        check_in_time=now,
        method='qr',
        teacher_id=teacher_id
    )
    db.session.add(attendance)
    db.session.commit()
    template = get_sms_templates()['present']['bn']
    message = template.format(
        NAME=student.name,
        CLASS=student.class_level,
        TIME=now.strftime('%I:%M %p')
    )
    sms_result = send_sms_via_smsgate([student.father_phone], message)
    sms_log = SMSLog(
        student_id=student.id,
        phone=student.father_phone,
        message=message,
        message_type='present',
        status='sent' if sms_result['success'] else 'failed'
    )
    db.session.add(sms_log)
    db.session.commit()
    socketio.emit('attendance_update', {
        'student_id': student.id,
        'student_name': student.name,
        'class': student.class_level,
        'time': now.strftime('%I:%M %p')
    })
    return jsonify({
        'success': True,
        'student': {
            'name': student.name,
            'class': student.class_level,
            'roll': student.roll,
            'uid': student.uid
        },
        'time': now.strftime('%I:%M %p')
    })

@app.route('/attendance/check', methods=['GET', 'POST'])
def check_attendance():
    if request.method == 'POST':
        uid = request.form.get('uid')
        phone_last4 = request.form.get('phone_last4')
        student = Student.query.filter_by(uid=uid, is_active=True).first()
        if not student:
            flash('Student not found', 'error')
            return render_template('check_attendance.html')
        if phone_last4 and not student.father_phone.endswith(phone_last4):
            flash('Phone verification failed', 'error')
            return render_template('check_attendance.html')
        today = get_bangladesh_time().date()
        attendance = Attendance.query.filter_by(
            student_id=student.id,
            date=today
        ).first()
        now = get_bangladesh_time()
        status = 'pending'
        if attendance:
            status = attendance.status
        elif now.time() >= datetime.strptime('11:00', '%H:%M').time():
            status = 'absent'
        return render_template('check_attendance.html',
                             student=student,
                             attendance=attendance,
                             status=status,
                             show_result=True)
    return render_template('check_attendance.html')

@app.route('/admin/students', methods=['GET', 'POST'])
@admin_required
def manage_students():
    if request.method == 'POST':
        if 'add_student' in request.form:
            name = request.form.get('name')
            class_level = int(request.form.get('class_level'))
            roll = int(request.form.get('roll'))
            father_name = request.form.get('father_name')
            father_phone = request.form.get('father_phone')
            mother_name = request.form.get('mother_name')
            mother_phone = request.form.get('mother_phone')
            birth_date = request.form.get('birth_date')
            blood_group = request.form.get('blood_group')
            address = request.form.get('address')
            uid = generate_uid()
            qr_token = generate_qr_token()
            qr_expiry = datetime.utcnow() + timedelta(days=365)
            student = Student(
                uid=uid,
                name=name,
                class_level=class_level,
                roll=roll,
                father_name=father_name,
                father_phone=father_phone,
                mother_name=mother_name,
                mother_phone=mother_phone,
                birth_date=datetime.strptime(birth_date, '%Y-%m-%d') if birth_date else None,
                blood_group=blood_group,
                address=address,
                qr_token=qr_token,
                qr_expiry=qr_expiry
            )
            db.session.add(student)
            db.session.commit()

            # Create parent accounts for father and mother
            create_or_update_parent(father_phone, student, 'father')
            if mother_phone:
                create_or_update_parent(mother_phone, student, 'mother')

            flash('Student added successfully', 'success')
            log_audit(current_user.id, 'add_student', f'Student: {name}, UID: {uid}')
        elif 'delete_student' in request.form:
            student_id = request.form.get('student_id')
            student = Student.query.get(student_id)
            if student:
                student.is_active = False
                db.session.commit()
                flash('Student deactivated', 'success')
                log_audit(current_user.id, 'deactivate_student', f'Student ID: {student_id}')
    class_filter = request.args.get('class')
    query = Student.query.filter_by(is_active=True)
    if class_filter and class_filter != 'all':
        query = query.filter_by(class_level=int(class_filter))
    students = query.order_by(Student.class_level, Student.roll).all()
    return render_template('manage_students.html', students=students)

@app.route('/admin/student/<int:student_id>/generate-id-card', methods=['GET', 'POST'])
@admin_required
def generate_id_card(student_id):
    student = Student.query.get_or_404(student_id)
    if request.method == 'POST':
        if 'photo' in request.files:
            file = request.files['photo']
            if file.filename != '':
                filename = secure_filename(f"{student.uid}_{int(time.time())}.webp")
                filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
                img = Image.open(file)
                img = img.resize((300, 300), Image.Resampling.LANCZOS)
                img.save(filepath, 'WEBP', quality=85)
                student.photo_filename = filename
        if not student.id_card_token:
            student.id_card_token = secrets.token_urlsafe(16)
            student.id_card_cvv = generate_cvv()
        db.session.commit()

        # Ensure parent accounts exist and send ID card link
        template = get_sms_templates()['id_card']['bn']
        message = template.format(
            LINK=f"{request.host_url}idcard/dl/{student.id_card_token}",
            CVV=student.id_card_cvv
        )
        # Send to father
        if student.father_phone:
            sms_result = send_sms_via_smsgate([student.father_phone], message)
            sms_log = SMSLog(
                student_id=student.id,
                phone=student.father_phone,
                message=message,
                message_type='id_card',
                status='sent' if sms_result['success'] else 'failed'
            )
            db.session.add(sms_log)
        # Also send to mother if available
        if student.mother_phone:
            sms_result = send_sms_via_smsgate([student.mother_phone], message)
            sms_log = SMSLog(
                student_id=student.id,
                phone=student.mother_phone,
                message=message,
                message_type='id_card',
                status='sent' if sms_result['success'] else 'failed'
            )
            db.session.add(sms_log)
        db.session.commit()

        flash('ID card generated and SMS sent', 'success')
        log_audit(current_user.id, 'generate_id_card', f'Student ID: {student_id}')
    return render_template('generate_id_card.html', student=student)

@app.route('/idcard/dl/<token>')
def download_id_card(token):
    student = Student.query.filter_by(id_card_token=token, is_active=True).first()
    if not student:
        return 'ID card not found', 404
    qr_img = generate_qr_code(student)
    width, height = 1011, 638
    card = Image.new('RGB', (width, height), (255, 255, 255))
    draw = ImageDraw.Draw(card)
    for i in range(height):
        r = int(255 - (i / height * 100))
        g = int(240 - (i / height * 80))
        b = int(200 - (i / height * 60))
        draw.line([(0, i), (width, i)], fill=(r, g, b))
    try:
        title_font = ImageFont.truetype("arialbd.ttf", 28)
        text_font = ImageFont.truetype("arial.ttf", 20)
        small_font = ImageFont.truetype("arial.ttf", 16)
    except:
        title_font = ImageFont.load_default()
        text_font = ImageFont.load_default()
        small_font = ImageFont.load_default()
    draw.text((50, 30), "Bhorilhat Nesaria Dakhil Madrasha", font=title_font, fill=(0, 0, 0))
    draw.text((50, 80), "Dewra Faridpur, Dhaka", font=small_font, fill=(100, 100, 100))
    if student.photo_filename:
        photo_path = os.path.join(app.config['UPLOAD_FOLDER'], student.photo_filename)
        if os.path.exists(photo_path):
            photo = Image.open(photo_path)
            photo = photo.resize((150, 150))
            card.paste(photo, (50, 130))
    y_pos = 130
    draw.text((220, y_pos), f"Name: {student.name}", font=text_font, fill=(0, 0, 0))
    y_pos += 40
    draw.text((220, y_pos), f"Class: {student.class_level} Roll: {student.roll}", font=text_font, fill=(0, 0, 0))
    y_pos += 40
    draw.text((220, y_pos), f"UID: {student.uid}", font=text_font, fill=(0, 0, 0))
    y_pos += 40
    draw.text((220, y_pos), f"Father: {student.father_name}", font=small_font, fill=(0, 0, 0))
    y_pos += 30
    if student.birth_date:
        draw.text((220, y_pos), f"DOB: {student.birth_date.strftime('%d/%m/%Y')}", font=small_font, fill=(0, 0, 0))
        y_pos += 30
    if student.blood_group:
        draw.text((220, y_pos), f"Blood: {student.blood_group}", font=small_font, fill=(0, 0, 0))
    qr_size = 400
    qr = Image.open(qr_img)
    qr = qr.resize((qr_size, qr_size))
    card.paste(qr, (width - qr_size - 50, 130))
    if student.id_card_cvv:
        draw.text((width - 100, height - 50), f"CVV: {student.id_card_cvv}", font=small_font, fill=(255, 0, 0))
    img_io = BytesIO()
    card.save(img_io, 'PNG', quality=95)
    img_io.seek(0)
    return send_file(img_io, mimetype='image/png',
                     download_name=f"ID_Card_{student.uid}.png",
                     as_attachment=True)

@app.route('/id-card/verify', methods=['GET', 'POST'])
def verify_id_card():
    if request.method == 'POST':
        uid = request.form.get('uid')
        cvv = request.form.get('cvv')
        student = Student.query.filter_by(uid=uid, id_card_cvv=cvv, is_active=True).first()
        if student:
            return render_template('verify_id_card.html', 
                                 student=student,
                                 verified=True)
        else:
            flash('Invalid UID or CVV', 'error')
    return render_template('verify_id_card.html')

@app.route('/admin/messages', methods=['GET', 'POST'])
@admin_required
def send_messages():
    if request.method == 'POST':
        message = request.form.get('message')
        target_type = request.form.get('target_type')
        target_value = request.form.get('target_value')
        if not message or len(message.strip()) < 5:
            flash('Message is too short', 'error')
            return redirect(url_for('send_messages'))
        recipients = []
        if target_type == 'individual':
            student = Student.query.get(target_value)
            if student:
                recipients.append({
                    'phone': student.father_phone,
                    'student_id': student.id
                })
        elif target_type == 'class':
            students = Student.query.filter_by(
                class_level=int(target_value),
                is_active=True
            ).all()
            for student in students:
                recipients.append({
                    'phone': student.father_phone,
                    'student_id': student.id
                })
        elif target_type == 'institute':
            students = Student.query.filter_by(is_active=True).all()
            for student in students:
                recipients.append({
                    'phone': student.father_phone,
                    'student_id': student.id
                })
        phones = [r['phone'] for r in recipients]
        sms_result = send_sms_via_smsgate(phones, message)
        msg_log = MessageLog(
            sent_by=current_user.id,
            target_type=target_type,
            target_value=target_value,
            message=message,
            character_count=len(message),
            delivery_status='sent' if sms_result['success'] else 'failed'
        )
        db.session.add(msg_log)
        for recipient in recipients:
            sms_log = SMSLog(
                student_id=recipient.get('student_id'),
                phone=recipient['phone'],
                message=message,
                message_type='custom',
                status='sent' if sms_result['success'] else 'failed'
            )
            db.session.add(sms_log)
        db.session.commit()
        flash(f'Message sent to {len(recipients)} recipients', 'success')
        log_audit(current_user.id, 'send_message', 
                 f'Type: {target_type}, Count: {len(recipients)}')
    students = Student.query.filter_by(is_active=True).order_by(Student.class_level, Student.name).all()
    return render_template('send_messages.html', students=students)

@app.route('/admin/reports/attendance')
@admin_required
def attendance_reports():
    now = get_bangladesh_time()
    today = now.date()
    report_date = request.args.get('date')
    if report_date:
        report_date = datetime.strptime(report_date, '%Y-%m-%d').date()
    else:
        report_date = today
    if report_date == today and now.time() < datetime.strptime('12:00', '%H:%M').time():
        return render_template('reports_not_available.html')
    class_filter = request.args.get('class')
    query = db.session.query(
        Student, Attendance
    ).join(Attendance, Student.id == Attendance.student_id)\
     .filter(Attendance.date == report_date)\
     .filter(Student.is_active == True)
    if class_filter and class_filter != 'all':
        query = query.filter(Student.class_level == int(class_filter))
    attendance_records = query.order_by(Student.class_level, Student.roll).all()
    present_student_ids = [r[0].id for r in attendance_records]
    absent_query = Student.query.filter(Student.is_active == True)
    if class_filter and class_filter != 'all':
        absent_query = absent_query.filter(Student.class_level == int(class_filter))
    absent_students = absent_query.filter(~Student.id.in_(present_student_ids)).all()
    return render_template('attendance_reports.html',
                         attendance_records=attendance_records,
                         absent_students=absent_students,
                         report_date=report_date,
                         class_filter=class_filter)

@app.route('/admin/teachers', methods=['GET', 'POST'])
@admin_required
def manage_teachers():
    if request.method == 'POST':
        if 'add_teacher' in request.form:
            name = request.form.get('name')
            email = request.form.get('email')
            phone = request.form.get('phone')
            password = request.form.get('password')
            if User.query.filter_by(email=email).first():
                flash('Email already exists', 'error')
                return redirect(url_for('manage_teachers'))
            teacher = User(
                email=email,
                name=name,
                phone=phone,
                role='teacher'
            )
            teacher.set_password(password)
            db.session.add(teacher)
            db.session.commit()
            classes = request.form.getlist('classes[]')
            for class_level in classes:
                if class_level:
                    assignment = TeacherAssignment(
                        teacher_id=teacher.id,
                        class_level=int(class_level),
                        can_take_attendance=True,
                        assigned_by=current_user.id
                    )
                    db.session.add(assignment)
            db.session.commit()
            flash('Teacher added successfully', 'success')
            log_audit(current_user.id, 'add_teacher', f'Teacher: {name}')
        elif 'update_assignment' in request.form:
            teacher_id = request.form.get('teacher_id')
            classes = request.form.getlist('assigned_classes[]')
            TeacherAssignment.query.filter_by(teacher_id=teacher_id).delete()
            for class_level in classes:
                if class_level:
                    assignment = TeacherAssignment(
                        teacher_id=teacher_id,
                        class_level=int(class_level),
                        can_take_attendance=True,
                        assigned_by=current_user.id
                    )
                    db.session.add(assignment)
            db.session.commit()
            flash('Assignments updated', 'success')
            log_audit(current_user.id, 'update_assignments', f'Teacher ID: {teacher_id}')
    teachers = User.query.filter_by(role='teacher', is_active=True).all()
    return render_template('manage_teachers.html', teachers=teachers)

@app.route('/admin/settings', methods=['GET', 'POST'])
@admin_required
def system_settings():
    if request.method == 'POST':
        if 'attendance_system' in request.form:
            action = request.form.get('attendance_system')
            setting = SystemSettings.query.filter_by(setting_key='attendance_system_active').first()
            if not setting:
                setting = SystemSettings(setting_key='attendance_system_active')
                db.session.add(setting)
            setting.setting_value = 'true' if action == 'start' else 'false'
            db.session.commit()
            status = 'started' if action == 'start' else 'stopped'
            flash(f'Attendance system {status}', 'success')
            log_audit(current_user.id, f'attendance_system_{action}')
    attendance_active = is_attendance_system_active()
    return render_template('system_settings.html', attendance_active=attendance_active)

@app.route('/admin/audit-logs')
@admin_required
def audit_logs():
    page = request.args.get('page', 1, type=int)
    per_page = 50
    logs = AuditLog.query.order_by(AuditLog.created_at.desc())\
                         .paginate(page=page, per_page=per_page)
    return render_template('audit_logs.html', logs=logs)

# ---------- RESULT MANAGEMENT ROUTES ----------
@app.route('/admin/exams', methods=['GET', 'POST'])
@admin_required
def manage_exams():
    if request.method == 'POST':
        name = request.form.get('name')
        year = request.form.get('year', get_bangladesh_time().year)
        exam = Exam(name=name, year=year)
        db.session.add(exam)
        db.session.commit()
        flash('পরীক্ষা যোগ করা হয়েছে', 'success')
        log_audit(current_user.id, 'add_exam', f'Exam: {name} {year}')
        return redirect(url_for('manage_exams'))
    exams = Exam.query.order_by(Exam.year.desc(), Exam.name).all()
    return render_template('manage_exams.html', exams=exams)

@app.route('/admin/results/entry', methods=['GET', 'POST'])
@admin_required
def result_entry():
    if request.method == 'POST':
        class_level = int(request.form.get('class_level'))
        exam_id = int(request.form.get('exam_id'))
        return redirect(url_for('enter_marks', class_level=class_level, exam_id=exam_id))
    exams = Exam.query.filter_by(is_active=True).all()
    return render_template('result_entry_select.html', exams=exams)

@app.route('/admin/results/enter/<int:class_level>/<int:exam_id>', methods=['GET', 'POST'])
@admin_required
def enter_marks(class_level, exam_id):
    exam = Exam.query.get_or_404(exam_id)
    students = Student.query.filter_by(class_level=class_level, is_active=True).order_by(Student.roll).all()
    subjects = Subject.query.filter_by(class_level=class_level).order_by(Subject.order).all()
    if not subjects:
        default_subjects = ['বাংলা', 'ইংরেজি', 'গণিত', 'বিজ্ঞান', 'সমাজ', 'ধর্ম']
        subjects = [Subject(subject_name=s, full_marks=100, pass_marks=33) for s in default_subjects]

    # Build result_map for each student
    result_map = {}
    for student in students:
        result = Result.query.filter_by(student_id=student.id, exam_id=exam_id).first()
        if result:
            result_map[student.id] = result.subject_marks
        else:
            result_map[student.id] = {}

    if request.method == 'POST':
        for student in students:
            result = Result.query.filter_by(student_id=student.id, exam_id=exam_id).first()
            if not result:
                result = Result(
                    student_id=student.id,
                    exam_id=exam_id,
                    class_level=class_level,
                    roll=student.roll,
                    download_token=generate_result_token()
                )
                db.session.add(result)

            marks = {}
            total_obtained = 0
            total_full = 0
            for subj in subjects:
                key = f'marks_{student.id}_{subj.subject_name}'
                if key in request.form and request.form[key].strip():
                    try:
                        m = float(request.form[key])
                        marks[subj.subject_name] = m
                        total_obtained += m
                        total_full += subj.full_marks
                    except ValueError:
                        pass
            result.subject_marks = marks
            result.obtained_marks = total_obtained
            result.total_marks = total_full
            result.percentage = (total_obtained / total_full * 100) if total_full > 0 else 0
            result.grade = calculate_grade(result.percentage)

        db.session.commit()
        flash('মার্কস সংরক্ষিত হয়েছে', 'success')
        log_audit(current_user.id, 'enter_marks', f'Class {class_level}, Exam: {exam.name}')
        return redirect(url_for('result_entry'))

    return render_template('result_entry_grid.html',
                         class_level=class_level,
                         exam=exam,
                         students=students,
                         subjects=subjects,
                         result_map=result_map)

@app.route('/admin/results/publish', methods=['POST'])
@admin_required
def publish_results():
    class_level = int(request.form.get('class_level'))
    exam_id = int(request.form.get('exam_id'))
    exam = Exam.query.get_or_404(exam_id)

    results = Result.query.filter_by(class_level=class_level, exam_id=exam_id).all()
    count = 0
    for res in results:
        if not res.published:
            res.published = True
            res.published_at = get_bangladesh_time()
            if not res.download_token:
                res.download_token = generate_result_token()
            count += 1

            student = res.student
            # Send SMS to father and mother
            for phone in [student.father_phone, student.mother_phone]:
                if phone:
                    link = url_for('view_marksheet', token=res.download_token, _external=True)
                    message = f'''আপনার সন্তানের ফলাফল প্রকাশিত হয়েছে।
ডাউনলোড লিংক: {link}
ধন্যবাদ - ভরিলহাট নেছারিয়া দাখিল মাদ্রাসা'''
                    send_sms_via_smsgate([phone], message)

    db.session.commit()
    flash(f'{count} জন শিক্ষার্থীর ফলাফল প্রকাশিত হয়েছে এবং SMS পাঠানো হয়েছে', 'success')
    log_audit(current_user.id, 'publish_results', f'Class {class_level}, Exam: {exam.name}')
    return redirect(url_for('result_entry'))

@app.route('/result/check', methods=['GET', 'POST'])
def check_result():
    if request.method == 'POST':
        class_level = int(request.form.get('class_level'))
        roll = int(request.form.get('roll'))
        exam_id = request.form.get('exam_id')

        student = Student.query.filter_by(class_level=class_level, roll=roll, is_active=True).first()
        if not student:
            flash('শিক্ষার্থী পাওয়া যায়নি', 'error')
            return redirect(url_for('check_result'))

        query = Result.query.filter_by(student_id=student.id, published=True)
        if exam_id:
            query = query.filter_by(exam_id=exam_id)
        result = query.order_by(Result.published_at.desc()).first()

        if not result:
            flash('এই শিক্ষার্থীর ফলাফল এখনও প্রকাশিত হয়নি', 'error')
            return redirect(url_for('check_result'))

        return redirect(url_for('view_marksheet', token=result.download_token))

    exams = Exam.query.filter_by(is_active=True).all()
    return render_template('result_check.html', exams=exams)

@app.route('/result/<token>')
def view_marksheet(token):
    result = Result.query.filter_by(download_token=token, published=True).first_or_404()
    student = result.student
    exam = result.exam

    if 'download' in request.args:
        pdf = generate_marksheet_pdf(student, result, exam)
        if pdf is None:
            flash('PDF generation is not available. Please try again later.', 'error')
            return redirect(url_for('view_marksheet', token=token))
        response = make_response(pdf)
        response.headers['Content-Type'] = 'application/pdf'
        response.headers['Content-Disposition'] = f'attachment; filename=marksheet_{student.uid}_{exam.id}.pdf'
        return response
    else:
        return render_template('view_marksheet.html',
                             student=student,
                             result=result,
                             exam=exam)

# ==================== PARENT PORTAL ROUTES ====================

@app.route('/parent/login', methods=['GET', 'POST'])
def parent_login():
    if 'parent_id' in session:
        return redirect(url_for('parent_dashboard'))
    if request.method == 'POST':
        phone = request.form.get('phone')
        password = request.form.get('password')
        parent = Parent.query.filter_by(phone=phone, is_active=True).first()
        if parent and parent.check_password(password):
            session.permanent = True
            session['parent_id'] = parent.id
            parent.last_login = datetime.utcnow()
            db.session.commit()
            flash('সফলভাবে লগইন করেছেন', 'success')
            return redirect(url_for('parent_dashboard'))
        flash('ভুল ফোন নম্বর বা পাসওয়ার্ড', 'error')
    return render_template('parent_login.html')

@app.route('/parent/logout')
def parent_logout():
    session.pop('parent_id', None)
    flash('লগআউট সম্পন্ন হয়েছে', 'info')
    return redirect(url_for('parent_login'))

@app.route('/parent/dashboard')
@parent_required
def parent_dashboard():
    parent = Parent.query.get(session['parent_id'])
    return render_template('parent_dashboard.html', parent=parent)

@app.route('/parent/child/<int:student_id>')
@parent_required
def parent_child_view(student_id):
    parent = Parent.query.get(session['parent_id'])
    student = Student.query.get_or_404(student_id)

    # Authorization: check if student belongs to parent
    if student not in parent.children:
        flash('আপনি এই শিক্ষার্থীর তথ্য দেখতে অনুমোদিত নন', 'error')
        return redirect(url_for('parent_dashboard'))

    # Get attendance records
    attendance = Attendance.query.filter_by(student_id=student.id).order_by(Attendance.date.desc()).all()
    # Get published results
    results = Result.query.filter_by(student_id=student.id, published=True).order_by(Result.published_at.desc()).all()

    return render_template('parent_child_view.html',
                         student=student,
                         attendance=attendance,
                         results=results)

@app.route('/parent/forgot-password', methods=['GET', 'POST'])
def parent_forgot_password():
    if request.method == 'POST':
        phone = request.form.get('phone')
        parent = Parent.query.filter_by(phone=phone).first()
        if parent:
            otp = ''.join(random.choices(string.digits, k=6))
            session['reset_phone'] = phone
            session['reset_otp'] = otp
            session['reset_expiry'] = (datetime.now() + timedelta(minutes=10)).timestamp()
            message = f'আপনার পাসওয়ার্ড রিসেট OTP: {otp}\nএই কোডটি ১০ মিনিটের জন্য বৈধ।'
            send_sms_via_smsgate([phone], message)
            flash('ওটিপি আপনার মোবাইলে পাঠানো হয়েছে', 'info')
            return redirect(url_for('parent_reset_password'))
        else:
            flash('এই নম্বরটি নিবন্ধিত নয়', 'error')
    return render_template('parent_forgot_password.html')

@app.route('/parent/reset-password', methods=['GET', 'POST'])
def parent_reset_password():
    if request.method == 'POST':
        otp = request.form.get('otp')
        new_pass = request.form.get('new_password')
        if otp == session.get('reset_otp') and datetime.now().timestamp() < session.get('reset_expiry', 0):
            phone = session.get('reset_phone')
            parent = Parent.query.filter_by(phone=phone).first()
            if parent:
                parent.set_password(new_pass)
                db.session.commit()
                session.pop('reset_otp', None)
                session.pop('reset_phone', None)
                session.pop('reset_expiry', None)
                flash('পাসওয়ার্ড পরিবর্তন হয়েছে। এখন লগইন করুন।', 'success')
                return redirect(url_for('parent_login'))
        flash('OTP ভুল বা মেয়াদোত্তীর্ণ', 'error')
    return render_template('parent_reset_password.html')

@app.route('/parent/change-password', methods=['GET', 'POST'])
@parent_required
def parent_change_password():
    parent = Parent.query.get(session['parent_id'])
    if request.method == 'POST':
        current = request.form.get('current_password')
        new = request.form.get('new_password')
        if parent and parent.check_password(current):
            parent.set_password(new)
            db.session.commit()
            flash('পাসওয়ার্ড পরিবর্তন হয়েছে', 'success')
            return redirect(url_for('parent_dashboard'))
        flash('বর্তমান পাসওয়ার্ড ভুল', 'error')
    return render_template('parent_change_password.html')

# ==================== API ROUTES ====================

@app.route('/api/student/<uid>/qr')
def get_student_qr(uid):
    student = Student.query.filter_by(uid=uid, is_active=True).first()
    if not student:
        return jsonify({'error': 'Student not found'}), 404
    qr_img = generate_qr_code(student)
    return send_file(qr_img, mimetype='image/png')

@app.route('/api/attendance/today')
@login_required
def today_attendance_api():
    today = get_bangladesh_time().date()
    if current_user.role == 'admin':
        attendance = Attendance.query.filter_by(date=today)\
                                     .order_by(Attendance.check_in_time.desc())\
                                     .limit(100).all()
    else:
        attendance = Attendance.query.filter_by(
            date=today,
            teacher_id=current_user.id
        ).order_by(Attendance.check_in_time.desc()).all()
    data = []
    for record in attendance:
        data.append({
            'student_name': record.student.name,
            'class': record.student.class_level,
            'roll': record.student.roll,
            'time': record.check_in_time.strftime('%I:%M %p'),
            'method': record.method
        })
    return jsonify(data)

@app.route('/memorial')
def memorial():
    return render_template('memorial_popup.html')

@socketio.on('connect')
def handle_connect():
    if current_user.is_authenticated:
        emit('connected', {'user': current_user.name})

# ==================== INITIALIZATION ====================

@app.before_request
def before_request():
    session['bd_time'] = get_bangladesh_time().isoformat()

def create_default_admin():
    if not User.query.filter_by(email='admin@madrasha.edu').first():
        admin = User(
            email='admin@madrasha.edu',
            name='System Administrator',
            role='admin'
        )
        admin.set_password('admin123')
        db.session.add(admin)
        db.session.commit()
        print("Default admin created: admin@madrasha.edu / admin123")

def init_app():
    with app.app_context():
        db.create_all()
        create_default_admin()
        if not SystemSettings.query.filter_by(setting_key='attendance_system_active').first():
            setting = SystemSettings(
                setting_key='attendance_system_active',
                setting_value='false'
            )
            db.session.add(setting)
            db.session.commit()

init_app()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
