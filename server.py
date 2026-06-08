import json
import os
from datetime import datetime, timezone
from functools import wraps
from io import BytesIO

from flask import Flask, request, jsonify, session, send_from_directory, send_file
from flask_sqlalchemy import SQLAlchemy
from openpyxl import Workbook
from openpyxl.styles import Font, Alignment
from sqlalchemy import func, event
from sqlalchemy.exc import IntegrityError, OperationalError

app = Flask(__name__, static_folder='static')
app.secret_key = 'your-secret-key-change-in-production'  # 生产环境请更换

# ---------- PostgreSQL 数据库配置 ----------
DATABASE_URL = os.environ.get('DATABASE_URL')
if not DATABASE_URL:
    DATABASE_URL = 'postgresql://postgres:postgres@localhost:5432/annotations_db'

app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
# 设置全局隔离级别为 SERIALIZABLE – 最强的可串行化隔离，避免幻读、写偏序
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    'isolation_level': 'SERIALIZABLE',
    'pool_pre_ping': True,          # 连接前检测，避免使用失效连接
    'pool_recycle': 3600,
}

db = SQLAlchemy(app)

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

# ================== 数据模型（新增乐观锁、审计日志、幂等键） ==================
class Annotation(db.Model):
    __tablename__ = 'annotations'
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    username = db.Column(db.String(80), nullable=False)
    task_name = db.Column(db.String(500), nullable=False)
    duration_ms = db.Column(db.Integer, nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    # 乐观锁版本号
    version = db.Column(db.Integer, default=1, nullable=False)

    # 索引优化
    __table_args__ = (
        db.Index('idx_username_created', 'username', 'created_at'),
    )

class AuditLog(db.Model):
    """审计日志表 – 记录所有对 Annotation 的修改操作"""
    __tablename__ = 'audit_logs'
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    username = db.Column(db.String(80), nullable=False)          # 操作人
    action = db.Column(db.String(10), nullable=False)            # CREATE / UPDATE / DELETE
    annotation_id = db.Column(db.Integer, nullable=True)         # 关联的标注记录ID
    old_data = db.Column(db.JSON, nullable=True)                 # 修改前的数据（JSON）
    new_data = db.Column(db.JSON, nullable=True)                 # 修改后的数据（JSON）
    ip_address = db.Column(db.String(45), nullable=True)         # 客户端IP
    user_agent = db.Column(db.String(256), nullable=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

class IdempotencyKey(db.Model):
    """幂等键表 – 防止重复提交"""
    __tablename__ = 'idempotency_keys'
    key = db.Column(db.String(128), primary_key=True)            # 客户端提供的幂等键
    response = db.Column(db.JSON, nullable=False)                # 已保存的响应结果
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    expires_at = db.Column(db.DateTime, nullable=False)          # 过期时间（比如24小时后）

# ---------- 辅助函数 ----------
def get_client_ip():
    """获取真实客户端IP（考虑代理）"""
    if request.headers.get('X-Forwarded-For'):
        return request.headers.get('X-Forwarded-For').split(',')[0].strip()
    return request.remote_addr or '0.0.0.0'

def record_audit(action, annotation_id=None, old_data=None, new_data=None):
    """记录审计日志（异步写入，不阻塞主事务）"""
    try:
        audit = AuditLog(
            username=session.get('username', 'unknown'),
            action=action,
            annotation_id=annotation_id,
            old_data=old_data,
            new_data=new_data,
            ip_address=get_client_ip(),
            user_agent=request.headers.get('User-Agent', '')[:256]
        )
        db.session.add(audit)
        # 注意：不立即 commit，由外层事务统一提交
    except Exception as e:
        # 审计日志失败不应影响主业务，仅打印警告
        app.logger.warning(f"Failed to record audit log: {e}")

def validate_duration_seconds(duration_seconds):
    """强校验时长：正数，最大支持 24 小时（86400秒）"""
    try:
        value = float(duration_seconds)
        if value <= 0:
            raise ValueError("时长必须 > 0")
        if value > 86400:   # 24小时限制
            raise ValueError("时长不能超过 24 小时")
        return value
    except (TypeError, ValueError) as e:
        raise ValueError(f"视频时长格式错误或超出范围: {e}")

# ---------- 幂等键装饰器 ----------
def idempotent(expire_seconds=86400):
    """
    幂等键装饰器 – 要求客户端在请求头中提供 Idempotency-Key。
    如果相同的 Key 在有效期内再次请求，直接返回上次缓存的响应。
    """
    def decorator(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            # 只对 POST /api/annotations 生效（创建操作）
            if request.method != 'POST' or request.path != '/api/annotations':
                return f(*args, **kwargs)
            idem_key = request.headers.get('Idempotency-Key')
            if not idem_key:
                return jsonify({'error': 'Missing Idempotency-Key header for POST request'}), 400

            # 查询是否已存在
            existing = IdempotencyKey.query.get(idem_key)
            if existing and existing.expires_at > datetime.now(timezone.utc):
                # 返回缓存的响应
                app.logger.info(f"Idempotent hit: key={idem_key}")
                return jsonify(existing.response), 200
            elif existing:
                # 已过期，删除旧记录
                db.session.delete(existing)
                db.session.commit()

            # 执行业务逻辑
            resp = f(*args, **kwargs)
            # 仅当成功响应（2xx）且为 JSON 时才缓存
            if resp.status_code == 201 and resp.is_json:
                response_data = resp.get_json()
                # 存入幂等键表（有效期24小时）
                new_idem = IdempotencyKey(
                    key=idem_key,
                    response=response_data,
                    expires_at=datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=expire_seconds)
                )
                db.session.add(new_idem)
                db.session.commit()
            return resp
        return wrapped
    return decorator

# 引入 timedelta
from datetime import timedelta

# ---------- 装饰器：登录验证、管理员验证 ----------
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

# ---------- 页面路由 ----------
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
        'created_at': r.created_at.strftime('%Y-%m-%d %H:%M:%S'),
        'version': r.version   # 返回版本号供客户端后续更新使用
    } for r in records])

@app.route('/api/annotations', methods=['POST'])
@login_required
@idempotent(expire_seconds=86400)   # 幂等保护
def add_annotation():
    data = request.get_json()
    task_name = data.get('task_name', '').strip()
    duration_seconds = data.get('duration_seconds')

    if not task_name:
        return jsonify({'error': '标注任务名不能为空'}), 400
    try:
        duration_seconds = validate_duration_seconds(duration_seconds)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400

    duration_ms = int(round(duration_seconds * 1000))
    record = Annotation(
        username=session['username'],
        task_name=task_name,
        duration_ms=duration_ms
    )
    db.session.add(record)
    # 记录审计（会在提交前生成 annotation_id，但此时还没id，可以先flush）
    try:
        db.session.flush()  # 获取自增id
        record_audit('CREATE', annotation_id=record.id, new_data={
            'task_name': task_name,
            'duration_seconds': duration_seconds
        })
    except Exception:
        pass

    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Add annotation failed: {e}")
        return jsonify({'error': '数据库写入失败，请重试'}), 500

    return jsonify({'message': '添加成功', 'id': record.id, 'version': record.version}), 201

@app.route('/api/annotations/<int:record_id>', methods=['PUT'])
@login_required
def update_annotation(record_id):
    # 乐观锁更新：要求客户端提供当前版本号
    data = request.get_json()
    task_name = data.get('task_name', '').strip()
    duration_seconds = data.get('duration_seconds')
    client_version = data.get('version')   # 客户端必须传递期望的版本号

    if client_version is None:
        return jsonify({'error': '缺少版本号，请刷新后重试'}), 400

    # 查询记录（使用行锁 + 版本条件）
    record = Annotation.query.filter_by(id=record_id, username=session['username']).first()
    if not record:
        return jsonify({'error': '记录不存在或无权修改'}), 404

    # 记录旧数据
    old_data = {
        'task_name': record.task_name,
        'duration_seconds': round(record.duration_ms / 1000, 3)
    }

    if record.version != client_version:
        return jsonify({'error': '数据已被他人修改，请刷新后重新编辑'}), 409   # Conflict

    if not task_name:
        return jsonify({'error': '标注任务名不能为空'}), 400
    try:
        duration_seconds = validate_duration_seconds(duration_seconds)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400

    # 更新字段
    record.task_name = task_name
    record.duration_ms = int(round(duration_seconds * 1000))
    record.version += 1   # 版本号自增

    record_audit('UPDATE', annotation_id=record.id, old_data=old_data, new_data={
        'task_name': task_name,
        'duration_seconds': duration_seconds
    })

    try:
        db.session.commit()
    except OperationalError as e:   # SERIALIZABLE 隔离级别可能引发序列化失败
        db.session.rollback()
        app.logger.warning(f"Serialization conflict: {e}")
        return jsonify({'error': '并发冲突，请重试'}), 409
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Update failed: {e}")
        return jsonify({'error': '更新失败，请重试'}), 500

    return jsonify({'message': '更新成功', 'version': record.version})

@app.route('/api/annotations/<int:record_id>', methods=['DELETE'])
@login_required
def delete_annotation(record_id):
    record = Annotation.query.filter_by(id=record_id, username=session['username']).first()
    if not record:
        return jsonify({'error': '记录不存在或无权删除'}), 404

    old_data = {
        'task_name': record.task_name,
        'duration_seconds': round(record.duration_ms / 1000, 3)
    }
    record_audit('DELETE', annotation_id=record.id, old_data=old_data)

    db.session.delete(record)
    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Delete failed: {e}")
        return jsonify({'error': '删除失败，请重试'}), 500

    return jsonify({'message': '删除成功'})

@app.route('/api/mycount', methods=['GET'])
@login_required
def my_count():
    count = Annotation.query.filter_by(username=session['username']).count()
    return jsonify({'count': count})

# ---------- 管理员 API（增强统计与一致性检查） ----------
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
        'created_at': r.created_at.strftime('%Y-%m-%d %H:%M:%S'),
        'version': r.version
    } for r in records])

@app.route('/api/admin/users', methods=['GET'])
@login_required
@admin_required
def admin_users():
    stats = db.session.query(
        Annotation.username,
        func.count(Annotation.id).label('count'),
        func.sum(Annotation.duration_ms).label('total_duration_ms')
    ).group_by(Annotation.username).all()
    
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

@app.route('/api/admin/summary', methods=['GET'])
@login_required
@admin_required
def admin_summary():
    total_records = Annotation.query.count()
    total_duration_ms = db.session.query(func.sum(Annotation.duration_ms)).scalar() or 0
    total_duration_sec = round(total_duration_ms / 1000, 3)
    return jsonify({
        'total_records': total_records,
        'total_duration_seconds': total_duration_sec
    })

@app.route('/api/admin/consistency_check', methods=['GET'])
@login_required
@admin_required
def consistency_check():
    """
    检查每个用户的明细记录总和 与 统计表中的聚合值是否一致。
    返回不一致的列表（理论上应该一致，若因潜在 bug 导致不一致可被检出）
    """
    from collections import defaultdict
    details = db.session.query(Annotation.username, Annotation.duration_ms).all()
    agg = defaultdict(lambda: {'count': 0, 'total_ms': 0})
    for u, dur in details:
        agg[u]['count'] += 1
        agg[u]['total_ms'] += dur

    # 与通过 group by 查询的结果对比
    group_stats = db.session.query(
        Annotation.username,
        func.count(Annotation.id),
        func.sum(Annotation.duration_ms)
    ).group_by(Annotation.username).all()

    issues = []
    for username, cnt, total_ms in group_stats:
        expected_cnt = agg[username]['count']
        expected_ms = agg[username]['total_ms']
        if cnt != expected_cnt or total_ms != expected_ms:
            issues.append({
                'username': username,
                'group_count': cnt,
                'detail_count': expected_cnt,
                'group_total_ms': total_ms,
                'detail_total_ms': expected_ms
            })
    if not issues:
        return jsonify({'status': 'consistent', 'message': '所有数据一致性检查通过'})
    else:
        return jsonify({'status': 'inconsistent', 'issues': issues}), 409

# ---------- 导出 Excel ----------
@app.route('/excel', methods=['GET'])
@login_required
@admin_required   # 只允许管理员导出，避免数据泄露
def export_excel():
    records = Annotation.query.order_by(Annotation.created_at.asc()).all()

    stats_query = db.session.query(
        Annotation.username,
        func.count(Annotation.id).label('count'),
        func.sum(Annotation.duration_ms).label('total_ms')
    ).group_by(Annotation.username).all()
    stat_map = {s.username: (s.count, s.total_ms) for s in stats_query}

    wb = Workbook()
    ws_details = wb.active
    ws_details.title = "标注记录总表"

    headers = ['序号', '姓名', '任务名', '时长(秒)']
    ws_details.append(headers)
    for cell in ws_details[1]:
        cell.font = Font(bold=True)
        cell.alignment = Alignment(horizontal='center')

    for idx, record in enumerate(records, start=1):
        duration_sec = round(record.duration_ms / 1000, 3)
        ws_details.append([idx, record.username, record.task_name, duration_sec])

    ws_details.column_dimensions['A'].width = 8
    ws_details.column_dimensions['B'].width = 15
    ws_details.column_dimensions['C'].width = 50
    ws_details.column_dimensions['D'].width = 12

    ws_stats = wb.create_sheet("个人统计")
    ws_stats.append(['姓名', '完成条数', '完成时长(秒)'])
    for cell in ws_stats[1]:
        cell.font = Font(bold=True)
        cell.alignment = Alignment(horizontal='center')

    for username, info in USERS.items():
        count, total_ms = stat_map.get(username, (0, 0))
        total_seconds = round(total_ms / 1000, 3) if total_ms else 0
        ws_stats.append([username, count, total_seconds])

    ws_stats.column_dimensions['A'].width = 15
    ws_stats.column_dimensions['B'].width = 12
    ws_stats.column_dimensions['C'].width = 15

    output = BytesIO()
    wb.save(output)
    output.seek(0)

    # 记录导出审计
    record_audit('EXPORT', old_data={'export_time': datetime.now().isoformat()})

    return send_file(
        output,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        as_attachment=True,
        download_name='标注记录总表.xlsx'
    )

# ---------- 清理过期幂等键 ----------
@app.route('/api/admin/cleanup_idempotency_keys', methods=['POST'])
@login_required
@admin_required
def cleanup_idempotency_keys():
    """清理过期的幂等键，可由 cron 或管理员手动触发"""
    deleted = IdempotencyKey.query.filter(IdempotencyKey.expires_at < datetime.now(timezone.utc)).delete()
    db.session.commit()
    return jsonify({'deleted_count': deleted})

# ---------- 启动 ----------
if __name__ == '__main__':
    with app.app_context():
        db.create_all()   # 自动创建新表（审计日志、幂等键、带版本号的 annotation）
    # 开发模式仍使用内置服务器（但生产环境请使用 gunicorn，并确保 worker 数量与数据库连接池匹配）
    app.run(host='0.0.0.0', port=5000, debug=True, threaded=True)