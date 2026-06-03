from flask import Flask, render_template, request, redirect, url_for, session, jsonify, send_file
import sqlite3
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import date, datetime, timedelta
import random
import string
import qrcode
import io
import base64
import json
import os
import csv
from functools import wraps

app = Flask(__name__)
app.secret_key = 'smart_attendance_secret_key_2026'

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DB_PATH = os.path.join(BASE_DIR, 'attendance.db')

class DictCursor:
    def __init__(self, conn):
        self.cur = conn.cursor()
    def execute(self, query, params=()):
        return self.cur.execute(query, params)
    def fetchone(self):
        row = self.cur.fetchone()
        return dict(row) if row is not None else None
    def fetchall(self):
        return [dict(r) for r in self.cur.fetchall()]

class SQLiteCompat:
    def __init__(self, path):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute('PRAGMA foreign_keys = ON')
    def cursor(self, dictionary=False):
        return DictCursor(self.conn) if dictionary else self.conn.cursor()
    def commit(self):
        self.conn.commit()
    def close(self):
        self.conn.close()

def get_db():
    return SQLiteCompat(DB_PATH)

def ensure_column(cur, table, column, definition):
    cur.execute(f"PRAGMA table_info({table})")
    cols = [r[1] for r in cur.fetchall()]
    if column not in cols:
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

def init_db_if_needed():
    conn = sqlite3.connect(DB_PATH)
    conn.execute('PRAGMA foreign_keys = ON')
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL,
        full_name TEXT NOT NULL,
        email TEXT,
        role TEXT NOT NULL CHECK(role IN ('admin','teacher','student')),
        roll_no TEXT,
        gr_no TEXT,
        department TEXT,
        mobile TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")
    cur.execute("""
    CREATE TABLE IF NOT EXISTS subjects (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        code TEXT UNIQUE NOT NULL,
        department TEXT,
        teacher_id INTEGER,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (teacher_id) REFERENCES users(id) ON DELETE SET NULL
    )""")
    cur.execute("""
    CREATE TABLE IF NOT EXISTS enrollments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        student_id INTEGER NOT NULL,
        subject_id INTEGER NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(student_id, subject_id),
        FOREIGN KEY (student_id) REFERENCES users(id) ON DELETE CASCADE,
        FOREIGN KEY (subject_id) REFERENCES subjects(id) ON DELETE CASCADE
    )""")
    cur.execute("""
    CREATE TABLE IF NOT EXISTS attendance (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        student_id INTEGER NOT NULL,
        subject_id INTEGER NOT NULL,
        date DATE NOT NULL,
        status TEXT DEFAULT 'absent' CHECK(status IN ('present','absent')),
        marked_by INTEGER,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE (student_id, subject_id, date),
        FOREIGN KEY (student_id) REFERENCES users(id) ON DELETE CASCADE,
        FOREIGN KEY (subject_id) REFERENCES subjects(id) ON DELETE CASCADE
    )""")
    cur.execute("""
    CREATE TABLE IF NOT EXISTS attendance_sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        subject_id INTEGER NOT NULL,
        teacher_id INTEGER NOT NULL,
        otp TEXT NOT NULL,
        token TEXT UNIQUE NOT NULL,
        session_date DATE NOT NULL,
        status TEXT DEFAULT 'active' CHECK(status IN ('active','closed')),
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        expires_at TIMESTAMP NOT NULL,
        FOREIGN KEY (subject_id) REFERENCES subjects(id) ON DELETE CASCADE,
        FOREIGN KEY (teacher_id) REFERENCES users(id) ON DELETE CASCADE
    )""")
    cur.execute("""
    CREATE TABLE IF NOT EXISTS teacher_attendance_sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        otp TEXT NOT NULL,
        token TEXT UNIQUE NOT NULL,
        status TEXT DEFAULT 'active' CHECK(status IN ('active','closed')),
        created_by INTEGER,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        expires_at TIMESTAMP NOT NULL,
        FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE SET NULL
    )""")
    cur.execute("""
    CREATE TABLE IF NOT EXISTS teacher_attendance (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        teacher_id INTEGER NOT NULL,
        session_id INTEGER NOT NULL,
        date DATE NOT NULL,
        status TEXT DEFAULT 'present' CHECK(status IN ('present','absent','leave')),
        method TEXT,
        marked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(teacher_id, date),
        FOREIGN KEY (teacher_id) REFERENCES users(id) ON DELETE CASCADE,
        FOREIGN KEY (session_id) REFERENCES teacher_attendance_sessions(id) ON DELETE CASCADE
    )""")
    # migrate old DB if needed
    ensure_column(cur, 'users', 'gr_no', 'TEXT')
    ensure_column(cur, 'users', 'mobile', 'TEXT')
    cur.execute("DELETE FROM users WHERE role IN ('teacher','student') AND username IN ('teacher1','student1')")
    cur.execute("INSERT OR IGNORE INTO users (username,password,full_name,email,role,department) VALUES (?,?,?,?,?,?)",
                ('admin', generate_password_hash('admin123'), 'System Admin', 'admin@smartattendance.local', 'admin', 'Administration'))
    conn.commit()
    conn.close()

def generate_otp(length=6):
    return ''.join(random.choices(string.digits, k=length))

def generate_token(length=32):
    alphabet = string.ascii_letters + string.digits
    return ''.join(random.choices(alphabet, k=length))

def make_qr_base64(url):
    qr = qrcode.QRCode(version=1, box_size=8, border=4)
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    buf.seek(0)
    return base64.b64encode(buf.getvalue()).decode()

def login_required(role=None):
    def outer(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if 'user_id' not in session:
                return redirect(url_for('login'))
            if role and session.get('role') != role:
                return redirect(url_for('index'))
            return f(*args, **kwargs)
        return decorated
    return outer

def role_home(role):
    if role == 'admin':
        return 'admin_dashboard'
    if role == 'teacher':
        return 'teacher_dashboard'
    return 'student_dashboard'

@app.route('/')
def index():
    if 'user_id' in session:
        return redirect(url_for(role_home(session.get('role'))))
    return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        username = request.form['username'].strip()
        password = request.form['password']
        role = request.form['role']
        db = get_db(); cur = db.cursor(dictionary=True)
        cur.execute("SELECT * FROM users WHERE username=? AND role=?", (username, role))
        user = cur.fetchone(); db.close()
        if user and check_password_hash(user['password'], password):
            session.clear()
            session['user_id'] = user['id']
            session['username'] = user['username']
            session['role'] = user['role']
            session['full_name'] = user['full_name']
            return redirect(url_for(role_home(user['role'])))
        error = 'Invalid username, password, or role.'
    return render_template('login.html', error=error)

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

# ADMIN
@app.route('/admin/dashboard')
@login_required('admin')
def admin_dashboard():
    db = get_db(); cur = db.cursor(dictionary=True)
    stats = {}
    for key, q in {
        'teachers': "SELECT COUNT(*) cnt FROM users WHERE role='teacher'",
        'students': "SELECT COUNT(*) cnt FROM users WHERE role='student'",
        'classes': "SELECT COUNT(*) cnt FROM subjects",
        'enrollments': "SELECT COUNT(*) cnt FROM enrollments",
        'teacher_today': "SELECT COUNT(*) cnt FROM teacher_attendance WHERE date=DATE('now') AND status='present'",
    }.items():
        cur.execute(q); stats[key] = cur.fetchone()['cnt']
    cur.execute("SELECT s.*, u.full_name teacher_name FROM subjects s LEFT JOIN users u ON s.teacher_id=u.id ORDER BY s.id DESC LIMIT 6")
    recent_classes = cur.fetchall()
    db.close()
    return render_template('admin_dashboard.html', stats=stats, recent_classes=recent_classes)

@app.route('/admin/teachers')
@login_required('admin')
def admin_teachers():
    db = get_db(); cur = db.cursor(dictionary=True)
    cur.execute("SELECT * FROM users WHERE role='teacher' ORDER BY full_name")
    teachers = cur.fetchall(); db.close()
    return render_template('admin_teachers.html', teachers=teachers)

@app.route('/admin/teacher/add', methods=['POST'])
@login_required('admin')
def admin_add_teacher():
    data = request.form
    db = get_db(); cur = db.cursor()
    cur.execute("INSERT INTO users (username,password,full_name,email,role,department,mobile) VALUES (?,?,?,?,?,?,?)",
                (data['username'].strip(), generate_password_hash(data['password']), data['full_name'].strip(), data.get('email','').strip(), 'teacher', data.get('department','').strip(), data.get('mobile','').strip()))
    db.commit(); db.close()
    return redirect(url_for('admin_teachers'))

@app.route('/admin/teacher/delete/<int:tid>', methods=['POST'])
@login_required('admin')
def admin_delete_teacher(tid):
    db = get_db(); cur = db.cursor()
    cur.execute("UPDATE subjects SET teacher_id=NULL WHERE teacher_id=?", (tid,))
    cur.execute("DELETE FROM users WHERE id=? AND role='teacher'", (tid,))
    db.commit(); db.close()
    return redirect(url_for('admin_teachers'))

@app.route('/admin/students')
@login_required('admin')
def admin_students():
    db = get_db(); cur = db.cursor(dictionary=True)
    cur.execute("SELECT * FROM users WHERE role='student' ORDER BY full_name")
    students = cur.fetchall(); db.close()
    return render_template('admin_students.html', students=students)

@app.route('/admin/student/add', methods=['POST'])
@login_required('admin')
def admin_add_student():
    data = request.form
    db = get_db(); cur = db.cursor()
    cur.execute("INSERT INTO users (username,password,full_name,email,role,roll_no,gr_no,department,mobile) VALUES (?,?,?,?,?,?,?,?,?)",
                (data['username'].strip(), generate_password_hash(data['password']), data['full_name'].strip(), data.get('email','').strip(), 'student', data.get('roll_no','').strip(), data.get('gr_no','').strip(), data.get('department','').strip(), data.get('mobile','').strip()))
    db.commit(); db.close()
    return redirect(url_for('admin_students'))

@app.route('/admin/student/delete/<int:sid>', methods=['POST'])
@login_required('admin')
def admin_delete_student(sid):
    db = get_db(); cur = db.cursor()
    cur.execute("DELETE FROM users WHERE id=? AND role='student'", (sid,))
    db.commit(); db.close()
    return redirect(url_for('admin_students'))

@app.route('/admin/classes')
@login_required('admin')
def admin_classes():
    dept = request.args.get('department','')
    db = get_db(); cur = db.cursor(dictionary=True)
    cur.execute("SELECT * FROM users WHERE role='teacher' ORDER BY full_name")
    teachers = cur.fetchall()
    cur.execute("SELECT DISTINCT department FROM users WHERE role='student' AND department IS NOT NULL AND department<>'' ORDER BY department")
    departments = [r['department'] for r in cur.fetchall()]
    cur.execute("""
        SELECT s.*, u.full_name teacher_name, COUNT(e.id) enrolled_count
        FROM subjects s
        LEFT JOIN users u ON s.teacher_id=u.id
        LEFT JOIN enrollments e ON e.subject_id=s.id
        GROUP BY s.id ORDER BY s.id DESC
    """)
    classes = cur.fetchall()
    if dept:
        cur.execute("SELECT * FROM users WHERE role='student' AND department=? ORDER BY full_name", (dept,))
    else:
        cur.execute("SELECT * FROM users WHERE role='student' ORDER BY full_name")
    students = cur.fetchall()
    db.close()
    return render_template('admin_classes.html', classes=classes, teachers=teachers, students=students, departments=departments, selected_dept=dept)

@app.route('/admin/class/add', methods=['POST'])
@login_required('admin')
def admin_add_class():
    data = request.form
    teacher_id = data.get('teacher_id') or None
    db = get_db(); cur = db.cursor()
    cur.execute("INSERT INTO subjects (name,code,department,teacher_id) VALUES (?,?,?,?)",
                (data['name'].strip(), data['code'].strip(), data.get('department','').strip(), teacher_id))
    db.commit(); db.close()
    return redirect(url_for('admin_classes'))

@app.route('/admin/class/delete/<int:cid>', methods=['POST'])
@login_required('admin')
def admin_delete_class(cid):
    db = get_db(); cur = db.cursor()
    cur.execute("DELETE FROM subjects WHERE id=?", (cid,))
    db.commit(); db.close()
    return redirect(url_for('admin_classes'))

@app.route('/admin/class/enroll', methods=['POST'])
@login_required('admin')
def admin_enroll_students():
    subject_id = request.form.get('subject_id')
    student_ids = request.form.getlist('student_ids')
    db = get_db(); cur = db.cursor()
    for sid in student_ids:
        cur.execute("INSERT OR IGNORE INTO enrollments (student_id, subject_id) VALUES (?,?)", (sid, subject_id))
    db.commit(); db.close()
    return redirect(url_for('admin_classes', department=request.form.get('department_filter','')))

@app.route('/admin/class/<int:cid>/students')
@login_required('admin')
def admin_class_students(cid):
    db = get_db(); cur = db.cursor(dictionary=True)
    cur.execute("SELECT * FROM subjects WHERE id=?", (cid,)); cls = cur.fetchone()
    cur.execute("SELECT u.* FROM enrollments e JOIN users u ON e.student_id=u.id WHERE e.subject_id=? ORDER BY u.full_name", (cid,))
    students = cur.fetchall(); db.close()
    return render_template('admin_class_students.html', cls=cls, students=students)

@app.route('/admin/teacher-attendance', methods=['GET', 'POST'])
@login_required('admin')
def admin_teacher_attendance():
    db = get_db(); cur = db.cursor(dictionary=True)
    message = None
    if request.method == 'POST':
        expires_minutes = int(request.form.get('expires_minutes', 30) or 30)
        otp = generate_otp()
        token = generate_token()
        expires_at = (datetime.now() + timedelta(minutes=expires_minutes)).strftime('%Y-%m-%d %H:%M:%S')
        raw = db.cursor()
        raw.execute("INSERT INTO teacher_attendance_sessions (otp, token, created_by, expires_at) VALUES (?,?,?,?)",
                    (otp, token, session['user_id'], expires_at))
        db.commit()
        message = 'Teacher attendance QR/OTP session generated successfully.'

    cur.execute("SELECT * FROM users WHERE role='teacher' ORDER BY full_name")
    teachers = cur.fetchall()
    cur.execute("""
        SELECT tas.*, u.full_name admin_name,
        (SELECT COUNT(*) FROM teacher_attendance ta WHERE ta.session_id=tas.id) marked_count
        FROM teacher_attendance_sessions tas
        LEFT JOIN users u ON tas.created_by=u.id
        ORDER BY tas.id DESC LIMIT 8
    """)
    sessions = cur.fetchall()
    latest = sessions[0] if sessions else None
    qr_image = None
    mark_url = None
    if latest and latest['status'] == 'active':
        mark_url = request.host_url.rstrip('/') + url_for('teacher_attendance_mark') + '?token=' + latest['token']
        qr_image = make_qr_base64(mark_url)
    cur.execute("""
        SELECT ta.*, u.full_name, u.username
        FROM teacher_attendance ta
        JOIN users u ON ta.teacher_id=u.id
        ORDER BY ta.marked_at DESC LIMIT 30
    """)
    records = cur.fetchall(); db.close()
    return render_template('admin_teacher_attendance.html', teachers=teachers, sessions=sessions, latest=latest,
                           qr_image=qr_image, mark_url=mark_url, records=records, message=message)

@app.route('/admin/teacher-attendance/close/<int:sid>', methods=['POST'])
@login_required('admin')
def admin_close_teacher_attendance(sid):
    db=get_db(); cur=db.cursor()
    cur.execute("UPDATE teacher_attendance_sessions SET status='closed' WHERE id=?", (sid,))
    db.commit(); db.close()
    return redirect(url_for('admin_teacher_attendance'))

@app.route('/teacher/attendance', methods=['GET', 'POST'])
@login_required('teacher')
def teacher_attendance_mark():
    # Teacher self-attendance is disabled. Admin will handle teacher attendance.
    return redirect(url_for('teacher_dashboard'))

@app.route('/teacher/dashboard')
@login_required('teacher')
def teacher_dashboard():
    db = get_db(); cur = db.cursor(dictionary=True)
    tid = session['user_id']
    cur.execute("SELECT * FROM users WHERE id=?", (tid,)); profile = cur.fetchone()
    cur.execute("SELECT * FROM subjects WHERE teacher_id=? ORDER BY name", (tid,)); my_classes = cur.fetchall()
    class_ids = [c['id'] for c in my_classes]
    stats = {'my_classes': len(my_classes), 'students': 0, 'today_records': 0, 'recent': [], 'trend_labels': '[]', 'trend_data': '[]'}
    if class_ids:
        ph = ','.join('?' * len(class_ids))
        cur.execute(f"SELECT COUNT(DISTINCT student_id) cnt FROM enrollments WHERE subject_id IN ({ph})", class_ids); stats['students'] = cur.fetchone()['cnt']
        cur.execute(f"SELECT COUNT(*) cnt FROM attendance WHERE date=DATE('now') AND subject_id IN ({ph})", class_ids); stats['today_records'] = cur.fetchone()['cnt']
        cur.execute(f"""
            SELECT u.full_name, s.name subject, a.status, a.date
            FROM attendance a JOIN users u ON a.student_id=u.id JOIN subjects s ON a.subject_id=s.id
            WHERE a.subject_id IN ({ph}) ORDER BY a.created_at DESC LIMIT 10
        """, class_ids); stats['recent'] = cur.fetchall()
        cur.execute(f"""
            SELECT date, COUNT(*) present FROM attendance WHERE status='present' AND date >= DATE('now','-7 day')
            AND subject_id IN ({ph}) GROUP BY date ORDER BY date
        """, class_ids); trend = cur.fetchall()
        stats['trend_labels'] = json.dumps([str(r['date']) for r in trend]); stats['trend_data'] = json.dumps([r['present'] for r in trend])
    db.close()
    return render_template('teacher_dashboard.html', stats=stats, profile=profile, my_classes=my_classes)

@app.route('/teacher/mark_attendance', methods=['GET','POST'])
@login_required('teacher')
def mark_attendance():
    db = get_db(); cur = db.cursor(dictionary=True)
    cur.execute("SELECT * FROM subjects WHERE teacher_id=? ORDER BY name", (session['user_id'],)); subjects = cur.fetchall()
    selected_subject = request.form.get('subject_id') or request.args.get('subject_id') or (str(subjects[0]['id']) if subjects else '')
    students = []
    if selected_subject:
        cur.execute("""
            SELECT u.*, IFNULL(a.status,'') status FROM enrollments e
            JOIN users u ON e.student_id=u.id
            LEFT JOIN attendance a ON a.student_id=u.id AND a.subject_id=e.subject_id AND a.date=?
            WHERE e.subject_id=? ORDER BY u.full_name
        """, (request.form.get('date', date.today().isoformat()), selected_subject))
        students = cur.fetchall()
    message = None
    if request.method == 'POST' and selected_subject:
        student_ids = request.form.getlist('student_ids'); present_ids = request.form.getlist('present_ids'); date_val = request.form['date']
        raw_cur = db.cursor()
        for sid in student_ids:
            status = 'present' if sid in present_ids else 'absent'
            raw_cur.execute("INSERT INTO attendance (student_id,subject_id,date,status,marked_by) VALUES (?,?,?,?,?) ON CONFLICT(student_id,subject_id,date) DO UPDATE SET status=excluded.status, marked_by=excluded.marked_by",
                            (sid, selected_subject, date_val, status, session['user_id']))
        db.commit(); message = f"Attendance saved for {len(student_ids)} students."
    db.close()
    return render_template('mark_attendance.html', subjects=subjects, students=students, message=message, today=date.today().isoformat(), selected_subject=str(selected_subject))

@app.route('/teacher/reports')
@login_required('teacher')
def reports():
    db = get_db(); cur = db.cursor(dictionary=True); data = {}
    cur.execute("SELECT * FROM subjects WHERE teacher_id=? ORDER BY name", (session['user_id'],)); data['subjects'] = cur.fetchall()
    subject_id = request.args.get('subject_id'); month = request.args.get('month', date.today().strftime('%Y-%m'))
    data['selected_subject'] = subject_id; data['selected_month'] = month; data['report'] = []
    if subject_id:
        cur.execute("""
            SELECT u.full_name,u.roll_no,u.gr_no,
            SUM(CASE WHEN a.status='present' THEN 1 ELSE 0 END) present,
            SUM(CASE WHEN a.status='absent' THEN 1 ELSE 0 END) absent,
            COUNT(a.id) total,
            ROUND(IFNULL(SUM(CASE WHEN a.status='present' THEN 1 ELSE 0 END)*100.0/NULLIF(COUNT(a.id),0),0),1) pct
            FROM enrollments e JOIN users u ON e.student_id=u.id
            LEFT JOIN attendance a ON a.student_id=u.id AND a.subject_id=e.subject_id AND strftime('%Y-%m', a.date)=?
            WHERE e.subject_id=? GROUP BY u.id ORDER BY u.full_name
        """, (month, subject_id)); data['report'] = cur.fetchall()
    db.close(); return render_template('reports.html', data=data)

@app.route('/teacher/generate_qr/<int:subject_id>')
@login_required('teacher')
def generate_qr(subject_id):
    """Teacher generates a QR + OTP session for student attendance."""
    db = get_db(); cur = db.cursor(dictionary=True)
    cur.execute("SELECT * FROM subjects WHERE id=? AND teacher_id=?", (subject_id, session['user_id']))
    subject = cur.fetchone()
    if not subject:
        db.close()
        return jsonify({'error': 'This class is not assigned to you.'}), 403

    otp = generate_otp()
    token = generate_token(36)
    today = date.today().isoformat()

    # Teacher can set OTP/QR validity time from Mark Attendance page.
    # Keep it between 1 and 180 minutes to avoid invalid/too-long sessions.
    try:
        expires_minutes = int(request.args.get('expires_minutes', 30) or 30)
    except ValueError:
        expires_minutes = 30
    expires_minutes = max(1, min(expires_minutes, 180))

    expires_at = (datetime.now() + timedelta(minutes=expires_minutes)).strftime('%Y-%m-%d %H:%M:%S')
    raw = db.cursor()
    # close old active sessions for same subject/day, then create a fresh one
    raw.execute("UPDATE attendance_sessions SET status='closed' WHERE subject_id=? AND session_date=? AND status='active'", (subject_id, today))
    raw.execute("""
        INSERT INTO attendance_sessions (subject_id, teacher_id, otp, token, session_date, expires_at)
        VALUES (?,?,?,?,?,?)
    """, (subject_id, session['user_id'], otp, token, today, expires_at))
    db.commit()
    mark_url = request.host_url.rstrip('/') + url_for('qr_attend') + '?token=' + token
    qr_image = make_qr_base64(mark_url)
    db.close()
    return jsonify({
        'qr_image': qr_image,
        'otp': otp,
        'token': token,
        'mark_url': mark_url,
        'expires_at': expires_at,
        'expires_minutes': expires_minutes
    })

@app.route('/teacher/attendance-session/close/<token>', methods=['POST'])
@login_required('teacher')
def close_student_attendance_session(token):
    db = get_db(); cur = db.cursor()
    cur.execute("UPDATE attendance_sessions SET status='closed' WHERE token=? AND teacher_id=?", (token, session['user_id']))
    db.commit(); db.close()
    return redirect(url_for('teacher_dashboard'))


@app.route('/api/students_for_subject/<int:subject_id>')
@login_required('teacher')
def students_for_subject(subject_id):
    db = get_db(); cur = db.cursor(dictionary=True)
    target_date = request.args.get('date', date.today().isoformat())
    cur.execute("""
        SELECT u.id, u.full_name, u.roll_no, u.gr_no, IFNULL(a.status,'') status
        FROM enrollments e
        JOIN users u ON e.student_id=u.id
        LEFT JOIN attendance a ON a.student_id=u.id AND a.subject_id=e.subject_id AND a.date=?
        WHERE e.subject_id=? ORDER BY u.full_name
    """, (target_date, subject_id))
    students = cur.fetchall(); db.close()
    return jsonify(students)

# STUDENT
@app.route('/student/dashboard')
@login_required('student')
def student_dashboard():
    db = get_db(); cur = db.cursor(dictionary=True); sid = session['user_id']
    cur.execute("SELECT * FROM users WHERE id=?", (sid,)); profile = cur.fetchone()
    cur.execute("""
        SELECT s.name,s.code,u.full_name teacher_name,
        SUM(CASE WHEN a.status='present' THEN 1 ELSE 0 END) present,
        COUNT(a.id) total,
        ROUND(IFNULL(SUM(CASE WHEN a.status='present' THEN 1 ELSE 0 END)*100.0/NULLIF(COUNT(a.id),0),0),1) pct
        FROM enrollments e JOIN subjects s ON e.subject_id=s.id
        LEFT JOIN users u ON s.teacher_id=u.id
        LEFT JOIN attendance a ON a.subject_id=s.id AND a.student_id=e.student_id
        WHERE e.student_id=? GROUP BY s.id ORDER BY s.name
    """, (sid,)); subjects = cur.fetchall()
    cur.execute("SELECT a.date,s.name subject,a.status FROM attendance a JOIN subjects s ON a.subject_id=s.id WHERE a.student_id=? ORDER BY a.date DESC LIMIT 15", (sid,)); recent = cur.fetchall()
    cur.execute("SELECT SUM(CASE WHEN status='present' THEN 1 ELSE 0 END) p, SUM(CASE WHEN status='absent' THEN 1 ELSE 0 END) a FROM attendance WHERE student_id=?", (sid,)); pie = cur.fetchone()
    cur.execute("SELECT ROUND(IFNULL(SUM(CASE WHEN status='present' THEN 1 ELSE 0 END)*100.0/NULLIF(COUNT(*),0),0),1) overall FROM attendance WHERE student_id=?", (sid,)); overall = cur.fetchone()['overall'] or 0
    db.close()
    return render_template('student_dashboard.html', data={'subjects':subjects,'recent':recent,'overall':overall,'pie_present':int(pie['p'] or 0),'pie_absent':int(pie['a'] or 0)}, profile=profile)

@app.route('/student/qr_attend', methods=['GET', 'POST'])
def qr_attend():
    token = request.args.get('token','').strip()
    message = None
    success = False

    db = get_db()
    cur = db.cursor(dictionary=True)

    cur.execute("SELECT * FROM attendance_sessions WHERE token=?", (token,))
    att_session = cur.fetchone()

    if not att_session:
        db.close()
        return render_template('qr_result.html', success=False, message='Invalid QR code.')

    if att_session['status'] != 'active':
        db.close()
        return render_template('qr_result.html', success=False, message='This attendance session is closed.')

    if datetime.now() > datetime.strptime(att_session['expires_at'], '%Y-%m-%d %H:%M:%S'):
        db.close()
        return render_template('qr_result.html', success=False, message='QR code expired.')

    if request.method == 'POST':
        gr_no = request.form.get('gr_no','').strip()

        cur.execute("SELECT * FROM users WHERE gr_no=? AND role='student'", (gr_no,))
        student = cur.fetchone()

        if not student:
            message = 'Invalid GR number.'
        else:
            cur.execute(
                "SELECT id FROM enrollments WHERE student_id=? AND subject_id=?",
                (student['id'], att_session['subject_id'])
            )

            if not cur.fetchone():
                message = 'This GR number is not enrolled in this class.'
            else:
                raw = db.cursor()
                raw.execute("""
                    INSERT INTO attendance (student_id,subject_id,date,status,marked_by)
                    VALUES (?,?,?,'present',?)
                    ON CONFLICT(student_id,subject_id,date)
                    DO UPDATE SET status='present', marked_by=excluded.marked_by
                """, (student['id'], att_session['subject_id'], att_session['session_date'], att_session['teacher_id']))

                db.commit()
                success = True
                message = 'Attendance marked successfully by QR.'

        db.close()
        return render_template('qr_result.html', success=success, message=message)

    db.close()
    return render_template('qr_gr_form.html', token=token)


@app.route('/student/otp_attend', methods=['GET','POST'])
@login_required('student')
def otp_attend():
    message = None
    success = False

    if request.method == 'POST':
        otp = request.form.get('otp','').strip()
        db = get_db()
        cur = db.cursor(dictionary=True)

        cur.execute("SELECT * FROM attendance_sessions WHERE otp=? ORDER BY id DESC LIMIT 1", (otp,))
        att_session = cur.fetchone()

        if not att_session:
            message = 'Invalid OTP.'
        elif att_session['status'] != 'active':
            message = 'This attendance session is closed.'
        elif datetime.now() > datetime.strptime(att_session['expires_at'], '%Y-%m-%d %H:%M:%S'):
            message = 'OTP expired.'
        else:
            cur.execute(
                "SELECT id FROM enrollments WHERE student_id=? AND subject_id=?",
                (session['user_id'], att_session['subject_id'])
            )
            if not cur.fetchone():
                message = 'You are not enrolled in this class.'
            else:
                raw = db.cursor()
                raw.execute("""
                    INSERT INTO attendance (student_id,subject_id,date,status,marked_by)
                    VALUES (?,?,?,'present',?)
                    ON CONFLICT(student_id,subject_id,date)
                    DO UPDATE SET status='present', marked_by=excluded.marked_by
                """, (session['user_id'], att_session['subject_id'], att_session['session_date'], att_session['teacher_id']))
                db.commit()
                success = True
                message = 'Attendance marked successfully by OTP.'

        db.close()

    return render_template('student_otp_attend.html', message=message, success=success)


@app.context_processor
def inject_globals():
    return {'now': date.today().strftime('%d %B %Y')}

if __name__ == '__main__':
    init_db_if_needed()

    port = int(os.environ.get("PORT", 5000))

    app.run(
        host="0.0.0.0",
        port=port,
        debug=False
    )
