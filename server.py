import json
from datetime import datetime, timezone
from flask import Flask, request, jsonify, session, send_from_directory
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import event
from functools import wraps

app = Flask(__name__, static_folder='static')
app.secret_key = 'your-secret-key-change-in-production'  # 生产环境请更换
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///annotations.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

# ---------- 启用 SQLite WAL 模式，提高并发 ----------
def set_sqlite_pragma(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute('PRAGMA journal_mode=WAL;')
    cursor.close()

# ---------- 加载账号 ----------
def load_accounts():
    with open('accounts.json', 'r', encoding='utf-8') as f:
        data = json.load(f)
    users = {}
    for u in data['users']:
        users[u['username']] = {
            'password': u['password'],
            'role': u['role']
        }
    return users

USERS = load_accounts()

# ---------- 数据库模型 ----------
class Annotation(db.Model):
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    username = db.Column(db.String(80), nullable=False)
    task_name = db.Column(db.String(500), nullable=False)   # 允许粘贴超链接
    duration_ms = db.Column(db.Integer, nullable=False)     # 视频时长（毫秒）
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

# ---------- 装饰器：登录验证 ----------
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'username' not in session:
            return jsonify({'error': '未登录'}), 401
        return f(*args, **kwargs)
    return decorated

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if session.get('role') != 'admin':
            return jsonify({'error': '需要管理员权限'}), 403
        return f(*args, **kwargs)
    return decorated

# ---------- 路由：页面 ----------
@app.route('/')
def index():
    return send_from_directory('static', 'login.html')

@app.route('/<path:filename>')
def static_files(filename):
    return send_from_directory('static', filename)

# ---------- 认证 API ----------
@app.route('/api/login', methods=['POST'])
def login():
    data = request.get_json()
    if not data:
        return jsonify({'error': '请求格式错误'}), 400
    username = data.get('username')
    password = data.get('password')
    user = USERS.get(username)
    if not user or user['password'] != password:
        return jsonify({'error': '用户名或密码错误'}), 401
    session['username'] = username
    session['role'] = user['role']
    return jsonify({'message': '登录成功', 'role': user['role']})

@app.route('/api/logout', methods=['POST'])
def logout():
    session.clear()
    return jsonify({'message': '已登出'})

@app.route('/api/session', methods=['GET'])
def get_session():
    if 'username' in session:
        return jsonify({'username': session['username'], 'role': session['role']})
    return jsonify({'username': None, 'role': None}), 401

# ---------- 标注员 API ----------
@app.route('/api/annotations', methods=['GET'])
@login_required
def get_my_annotations():
    records = Annotation.query.filter_by(username=session['username']).order_by(Annotation.created_at.desc()).all()
    return jsonify([{
        'id': r.id,
        'task_name': r.task_name,
        'duration_seconds': round(r.duration_ms / 1000, 3),
        'created_at': r.created_at.strftime('%Y-%m-%d %H:%M:%S')
    } for r in records])

@app.route('/api/annotations', methods=['POST'])
@login_required
def add_annotation():
    data = request.get_json()
    task_name = data.get('task_name', '').strip()
    duration_seconds = data.get('duration_seconds')

    if not task_name:
        return jsonify({'error': '标注任务名不能为空'}), 400
    try:
        duration_seconds = float(duration_seconds)
        if duration_seconds <= 0:
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({'error': '视频时长必须是正数'}), 400

    duration_ms = int(round(duration_seconds * 1000))
    record = Annotation(
        username=session['username'],
        task_name=task_name,
        duration_ms=duration_ms
    )
    db.session.add(record)
    db.session.commit()
    return jsonify({'message': '添加成功', 'id': record.id}), 201

@app.route('/api/annotations/<int:record_id>', methods=['PUT'])
@login_required
def update_annotation(record_id):
    record = Annotation.query.get(record_id)
    if not record:
        return jsonify({'error': '记录不存在'}), 404
    if record.username != session['username']:
        return jsonify({'error': '无权修改他人记录'}), 403

    data = request.get_json()
    task_name = data.get('task_name', '').strip()
    duration_seconds = data.get('duration_seconds')

    if not task_name:
        return jsonify({'error': '标注任务名不能为空'}), 400
    try:
        duration_seconds = float(duration_seconds)
        if duration_seconds <= 0:
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({'error': '视频时长必须是正数'}), 400

    record.task_name = task_name
    record.duration_ms = int(round(duration_seconds * 1000))
    db.session.commit()
    return jsonify({'message': '更新成功'})

@app.route('/api/annotations/<int:record_id>', methods=['DELETE'])
@login_required
def delete_annotation(record_id):
    record = Annotation.query.get(record_id)
    if not record:
        return jsonify({'error': '记录不存在'}), 404
    if record.username != session['username']:
        return jsonify({'error': '无权删除他人记录'}), 403
    db.session.delete(record)
    db.session.commit()
    return jsonify({'message': '删除成功'})

@app.route('/api/mycount', methods=['GET'])
@login_required
def my_count():
    count = Annotation.query.filter_by(username=session['username']).count()
    return jsonify({'count': count})

# ---------- 管理员 API ----------
@app.route('/api/admin/annotations', methods=['GET'])
@login_required
@admin_required
def admin_all_annotations():
    records = Annotation.query.order_by(Annotation.created_at.desc()).all()
    return jsonify([{
        'id': r.id,
        'username': r.username,
        'task_name': r.task_name,
        'duration_seconds': round(r.duration_ms / 1000, 3),
        'created_at': r.created_at.strftime('%Y-%m-%d %H:%M:%S')
    } for r in records])

@app.route('/api/admin/users', methods=['GET'])
@login_required
@admin_required
def admin_users():
    # 从数据库统计每个人的标注数量
    from sqlalchemy import func
    # 同时统计数量与总时长（毫秒）
    stats = db.session.query(
        Annotation.username,
        func.count(Annotation.id).label('count'),
        func.sum(Annotation.duration_ms).label('total_duration_ms')
    ).group_by(Annotation.username).all()
    
    # 构建用户统计映射
    stat_map = {s.username: (s.count, s.total_duration_ms) for s in stats}
    
    result = []
    for username, info in USERS.items():
        count, total_ms = stat_map.get(username, (0, 0))
        total_sec = round(total_ms / 1000, 3) if total_ms else 0
        result.append({
            'username': username,
            'role': info['role'],
            'annotation_count': count,
            'total_duration_seconds': total_sec
        })
    return jsonify(result)

# 管理员汇总接口 返回全局总记录数和总时长
@app.route('/api/admin/summary', methods=['GET'])
@login_required
@admin_required
def admin_summary():
    from sqlalchemy import func
    total_records = Annotation.query.count()
    total_duration_ms = db.session.query(func.sum(Annotation.duration_ms)).scalar() or 0
    total_duration_sec = round(total_duration_ms / 1000, 3)
    return jsonify({
        'total_records': total_records,
        'total_duration_seconds': total_duration_sec
    })

# ---------- 启动 ----------
if __name__ == '__main__':
    with app.app_context():
        # 注册 SQLite WAL 模式事件（需要在 engine 可用之后）
        @event.listens_for(db.engine, 'connect')
        def set_sqlite_pragma(dbapi_connection, connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute('PRAGMA journal_mode=WAL;')
            cursor.close()
        
        db.create_all()
    # 使用多线程模式，适合开发和小规模部署
    app.run(host='0.0.0.0', port=5000, debug=True, threaded=True)