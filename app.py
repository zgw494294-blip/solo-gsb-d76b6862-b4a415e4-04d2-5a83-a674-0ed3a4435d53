"""SQLite 事务隔离实验室 —— Flask 后端。

每个实验对应一个独立的 WAL 数据库（单行整数值 100）。
页面左右两栏（甲 / 乙）各绑定一条长期独立连接，事务完全由显式 SQL 控制；
第三条连接只读已提交值。状态刷新不执行任何 SELECT，
避免在 DEFERRED 事务中提前建立读快照。

写操作同样遵守这一约束：事务尚未建立读快照时，「加上」的边界检查
基于 observer 连接看到的已提交值在 Python 中完成，UPDATE 本身再以
checked_add 函数做最终精确判定，绝不先在本连接上 SELECT —— 否则
首次写锁冲突（SQLITE_BUSY）后本事务会留下读快照，对方提交释放写锁后
重试即被误报为 SQLITE_BUSY_SNAPSHOT。

WAL 检查点（PASSIVE / TRUNCATE）不由甲、乙两栏的连接执行，而是每次
用一条全新的独立连接真实执行 PRAGMA wal_checkpoint，busy_timeout=0：
读事务阻止回收时立即返回 busy=1，不等待。各写连接均已关闭自动检查点
（PRAGMA wal_autocheckpoint=0），检查点时机完全由手动按钮 / HTTP 控制。
检查点绝不代替两栏提交、回滚，也不替换连接；状态刷新只读取内存中
最近一次检查点结果的缓存，不执行任何 SQL、不触发检查点。
"""

import os
import re
import shutil
import sqlite3
import threading
import time
import uuid

from flask import Flask, jsonify, render_template, request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("LAB_DATA_DIR", os.path.join(BASE_DIR, "data"))
MAX_LOG_ENTRIES = 200
MAX_ADD_ABS = 2 ** 62  # 单次增量上限
INT64_MIN = -(2 ** 63)  # SQLite 整数下限，累加结果不得越界（含负向）
INT64_MAX = 2 ** 63 - 1  # SQLite 整数上限，越界会被 SQLite 静默转成 REAL
OVERFLOW_MESSAGE = "累加结果超出有符号 64 位整数范围，本次未修改"
JS_SAFE_INT_MAX = 2 ** 53 - 1  # Number.MAX_SAFE_INTEGER，超出则用十进制字符串传输

INT_PATTERN = re.compile(r"[+-]?[0-9]+")

VALID_OPS = {"begin", "read", "add", "commit", "rollback"}
OP_LABELS = {
    "begin": "BEGIN DEFERRED",
    "read": "读值",
    "commit": "COMMIT",
    "rollback": "ROLLBACK",
}

# 允许手动触发的检查点模式：SQL 片段直接拼入 PRAGMA，必须走白名单
VALID_CHECKPOINT_MODES = {"PASSIVE", "TRUNCATE"}
WAL_SUFFIX = "-wal"

app = Flask(__name__)


def json_int(value):
    """JSON 无损编码整数：安全整数范围内返回数字，超出则用十进制字符串。"""
    if value is None:
        return None
    if -JS_SAFE_INT_MAX <= value <= JS_SAFE_INT_MAX:
        return value
    return str(value)


def parse_int(raw):
    """解析增量：接受 JSON 整数或十进制字符串（大整数以字符串传输），否则返回 None。"""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str) and INT_PATTERN.fullmatch(raw.strip()):
        return int(raw.strip())
    return None


class PanelState:
    """某一栏的展示状态。last_read / log 记录在内存中，刷新状态不触碰 SQLite。"""

    def __init__(self):
        self.last_read = None
        self.log = []
        # 当前事务是否已建立读快照：显式读值后为 True，
        # 成功写入后也为 True；BEGIN 与 COMMIT/ROLLBACK 后重置。
        self.tx_has_snapshot = False

    def add_log(self, text):
        self.log.append("%s  %s" % (time.strftime("%H:%M:%S"), text))
        if len(self.log) > MAX_LOG_ENTRIES:
            del self.log[: len(self.log) - MAX_LOG_ENTRIES]


class Experiment:
    def __init__(self, exp_id):
        self.id = exp_id
        self.db_path = os.path.join(DATA_DIR, "exp_%s.db" % exp_id)
        self._init_db()
        # 两栏各自的长期连接：isolation_level=None 进入自动提交模式，
        # 事务完全由显式 BEGIN/COMMIT/ROLLBACK 控制；
        # busy_timeout=0 让写锁冲突立即报错而不是等待。
        # 两个写连接均关闭自动检查点（wal_autocheckpoint=0），
        # 使 WAL 回收只由手动 PASSIVE / TRUNCATE 检查点触发。
        # overflow_flags 供连接上的 checked_add 函数在判定 64 位溢出时置位，
        # 以便把溢出与普通 SQLite 错误区分开，沿用既有的拒绝响应。
        self.overflow_flags = {"a": [False], "b": [False]}
        self.conns = {
            "a": self._open_conn(self.overflow_flags["a"], autockpt=False),
            "b": self._open_conn(self.overflow_flags["b"], autockpt=False),
        }
        self.conn_locks = {"a": threading.Lock(), "b": threading.Lock()}
        # 第三条连接：只读已提交值
        self.observer = self._open_conn()
        self.observer_lock = threading.Lock()
        self.panels = {"a": PanelState(), "b": PanelState()}
        # 检查点状态只保存在内存：状态刷新只读这份缓存，不触发检查点
        self.checkpoint_lock = threading.Lock()
        self.checkpoint_last = None
        self.checkpoint_log = []

    def _init_db(self):
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                "CREATE TABLE kv (id INTEGER PRIMARY KEY CHECK (id = 1),"
                " val INTEGER NOT NULL)"
            )
            conn.execute("INSERT INTO kv (id, val) VALUES (1, 100)")
            conn.commit()
        finally:
            conn.close()

    def _open_conn(self, overflow_flag=None, autockpt=True):
        conn = sqlite3.connect(
            self.db_path, timeout=0, isolation_level=None, check_same_thread=False
        )
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=0")
        if not autockpt:
            # 关闭本连接提交时的自动检查点：检查点时机完全交由
            # 手动 PASSIVE / TRUNCATE 按钮（HTTP）控制。
            conn.execute("PRAGMA wal_autocheckpoint=0")
        if overflow_flag is not None:
            # checked_add 在 UPDATE 内部做最终的 64 位边界判定。
            # SQLite 对整数溢出不报错而会静默存成 REAL，必须在写入前拦下：
            # 溢出时置位并抛异常终止本次语句，行不会被修改，事务保持开启。
            # 函数只做纯计算，不执行任何读取，不会影响快照与加锁顺序。
            def checked_add(current, delta):
                result = current + delta
                if not INT64_MIN <= result <= INT64_MAX:
                    overflow_flag[0] = True
                    raise OverflowError("累加结果超出有符号 64 位整数范围")
                return result

            conn.create_function("checked_add", 2, checked_add)
        return conn

    def panel_state(self, side):
        # 只读取内存状态与连接标志位，绝不执行 SELECT，
        # 避免为 DEFERRED 事务提前建立读快照。
        panel = self.panels[side]
        return {
            "in_transaction": self.conns[side].in_transaction,
            "last_read": json_int(panel.last_read),
            "log": list(panel.log),
        }

    def committed_value(self):
        with self.observer_lock:
            row = self.observer.execute(
                "SELECT val FROM kv WHERE id = 1"
            ).fetchone()
            return row[0]

    def checkpoint_state(self):
        # 只读内存缓存：不执行任何 SQL、不触发检查点
        return {"last": self.checkpoint_last, "log": list(self.checkpoint_log)}

    def add_checkpoint_log(self, text):
        self.checkpoint_log.append("%s  %s" % (time.strftime("%H:%M:%S"), text))
        if len(self.checkpoint_log) > MAX_LOG_ENTRIES:
            del self.checkpoint_log[: len(self.checkpoint_log) - MAX_LOG_ENTRIES]

    def wal_size(self):
        # 操作后读取 WAL 文件字节数；文件不存在时记 0
        try:
            return os.path.getsize(self.db_path + WAL_SUFFIX)
        except OSError:
            return 0


def run_op(exp, side, op, value):
    """在指定栏的长期连接上真实执行一个操作，返回 {ok, error, state}。"""
    panel = exp.panels[side]
    conn = exp.conns[side]
    label = "加上 %s" % value if op == "add" else OP_LABELS.get(op, op)

    def reject(message):
        panel.add_log("%s → 拒绝：%s" % (label, message))
        return {
            "ok": False,
            "error": {"name": None, "code": None, "message": message},
            "state": exp.panel_state(side),
        }

    with exp.conn_locks[side]:
        try:
            if op == "begin":
                if conn.in_transaction:
                    return reject("事务已开启，不能重复 BEGIN")
                conn.execute("BEGIN DEFERRED")
                panel.tx_has_snapshot = False
                panel.add_log("BEGIN DEFERRED → 成功，事务已开启")
            elif op == "read":
                row = conn.execute("SELECT val FROM kv WHERE id = 1").fetchone()
                panel.last_read = row[0]
                panel.tx_has_snapshot = conn.in_transaction
                scope = "事务内" if conn.in_transaction else "事务外（自动提交）"
                panel.add_log("读值（%s）→ %s" % (scope, row[0]))
            elif op == "add":
                if not conn.in_transaction:
                    return reject("禁止事务外修改：请先 BEGIN DEFERRED")
                overflow_message = OVERFLOW_MESSAGE
                if panel.tx_has_snapshot:
                    # 本事务已有读快照（显式读值或此前写过）：
                    # 直接在事务内读当前值做精确边界检查，行为与单事务内
                    # 可见性完全一致。快照过时导致的写冲突仍由 SQLite
                    # 如实报 SQLITE_BUSY_SNAPSHOT，不做任何自动处理。
                    current = conn.execute(
                        "SELECT val FROM kv WHERE id = 1"
                    ).fetchone()[0]
                    if not INT64_MIN <= current + value <= INT64_MAX:
                        return reject(overflow_message)
                else:
                    # 关键路径：事务尚无读快照（BEGIN 后第一次操作就是写）。
                    # 绝不能先在本连接 SELECT —— 那会建立读快照：若对方正
                    # 持有写锁，本次 UPDATE 拿到 SQLITE_BUSY 后快照已留下，
                    # 对方提交后重试就会被误报为 SQLITE_BUSY_SNAPSHOT。
                    # 这里用 observer 看到的已提交值做无锁预判：仅当
                    # 「已提交值 + 增量」本身就越界时直接拒绝，从而保持
                    # 溢出拒绝不获取写锁、不影响另一栏的原有行为。
                    committed = exp.committed_value()
                    if not INT64_MIN <= committed + value <= INT64_MAX:
                        return reject(overflow_message)
                # 最终精确判定放在 UPDATE 内的 checked_add：
                # 无快照预判与真实写入之间若对方恰好提交了别的值，
                # 仍以语句内计算为准，保证永远不会静默写成 REAL。
                # 纯写语句在写锁被占时返回 SQLITE_BUSY 且不建立读快照，
                # 因此对方提交/回滚释放写锁后可在同一事务直接重试成功。
                exp.overflow_flags[side][0] = False
                conn.execute(
                    "UPDATE kv SET val = checked_add(val, ?) WHERE id = 1",
                    (value,),
                )
                panel.tx_has_snapshot = True
                panel.add_log("加上 %s → 成功（尚未提交）" % value)
            elif op == "commit":
                if not conn.in_transaction:
                    return reject("当前没有开启的事务")
                conn.execute("COMMIT")
                panel.tx_has_snapshot = False
                panel.add_log("COMMIT → 成功，事务已提交")
            elif op == "rollback":
                if not conn.in_transaction:
                    return reject("当前没有开启的事务")
                conn.execute("ROLLBACK")
                panel.tx_has_snapshot = False
                panel.add_log("ROLLBACK → 成功，事务已回滚")
            else:
                return reject("未知操作：%s" % op)
        except sqlite3.Error as exc:
            # checked_add 置位溢出标志时，这不是锁冲突而是越界拒绝：
            # 语句已终止、行未修改、事务保持开启，沿用既有拒绝响应。
            if exp.overflow_flags[side][0]:
                exp.overflow_flags[side][0] = False
                return reject(overflow_message)
            # 写锁冲突等 SQLite 错误：如实上报错误名与错误码，
            # 不做任何自动回滚，失败的事务保持开启。
            name = getattr(exc, "sqlite_errorname", None) or "SQLITE_ERROR"
            code = getattr(exc, "sqlite_errorcode", None)
            panel.add_log("%s → 失败 [%s (%s)] %s" % (label, name, code, exc))
            return {
                "ok": False,
                "error": {"name": name, "code": code, "message": str(exc)},
                "state": exp.panel_state(side),
            }
    return {"ok": True, "error": None, "state": exp.panel_state(side)}


def run_checkpoint(exp, mode):
    """用全新的独立连接真实执行 PRAGMA wal_checkpoint(<mode>)。

    busy_timeout=0：读事务阻止回收时立即返回 busy=1，绝不等待。
    检查点不替代两栏提交 / 回滚，也不触碰甲、乙的长期连接。
    """
    mode = (mode or "").upper()
    if mode not in VALID_CHECKPOINT_MODES:
        return {
            "ok": False,
            "error": {"name": None, "code": None, "message": "unknown checkpoint mode"},
            "state": exp.checkpoint_state(),
        }
    with exp.checkpoint_lock:
        # 每次一条独立连接：不用甲、乙或 observer，避免把检查点和
        # 两栏的事务状态、读快照绑定在一起。该连接不是最后一个连接，
        # 关闭时不会触发默认检查点；busy_timeout=0 使 busy 立即返回。
        ckpt_conn = sqlite3.connect(exp.db_path, timeout=0, isolation_level=None)
        try:
            ckpt_conn.execute("PRAGMA busy_timeout=0")
            try:
                row = ckpt_conn.execute(
                    "PRAGMA wal_checkpoint(%s)" % mode
                ).fetchone()
            except sqlite3.Error as exc:
                name = getattr(exc, "sqlite_errorname", None) or "SQLITE_ERROR"
                code = getattr(exc, "sqlite_errorcode", None)
                result = {
                    "mode": mode,
                    "busy": None,
                    "log_frames": None,
                    "checkpointed": None,
                    "wal_size": exp.wal_size(),
                }
                exp.checkpoint_last = result
                exp.add_checkpoint_log(
                    "%s → 失败 [%s (%s)] %s" % (mode, name, code, exc)
                )
                return {
                    "ok": False,
                    "error": {"name": name, "code": code, "message": str(exc)},
                    "state": exp.checkpoint_state(),
                }
        finally:
            ckpt_conn.close()
        # SQLite 原样返回三项：busy、WAL 总帧数 log、已回写帧数 checkpointed
        busy, log_frames, checkpointed = row[0], row[1], row[2]
        result = {
            "mode": mode,
            "busy": busy,
            "log_frames": log_frames,
            "checkpointed": checkpointed,
            "wal_size": exp.wal_size(),
        }
        exp.checkpoint_last = result
        if busy:
            tail = "检查点被并发读事务阻止（busy=1）"
        elif checkpointed < log_frames:
            tail = "回写未完成（%d/%d 帧）" % (checkpointed, log_frames)
        else:
            tail = "回写完成（%d/%d 帧）" % (checkpointed, log_frames)
        exp.add_checkpoint_log(
            "%s → busy=%d，WAL 总帧数=%d，已回写帧数=%d，WAL=%d 字节，%s"
            % (mode, busy, log_frames, checkpointed, result["wal_size"], tail)
        )
    return {"ok": True, "error": None, "state": exp.checkpoint_state(), **result}


# 实验注册表：仅保存在内存中，无需跨重启保留
experiments = {}
experiments_lock = threading.Lock()


def get_experiment(exp_id):
    with experiments_lock:
        return experiments.get(exp_id)


@app.get("/")
def index():
    return render_template("index.html")


@app.post("/api/experiments")
def create_experiment():
    exp_id = uuid.uuid4().hex[:12]
    exp = Experiment(exp_id)
    with experiments_lock:
        experiments[exp_id] = exp
    return jsonify({"id": exp_id})


@app.get("/api/experiments/<exp_id>/status")
def experiment_status(exp_id):
    exp = get_experiment(exp_id)
    if exp is None:
        return jsonify({"error": "experiment not found"}), 404
    # 两栏状态与检查点状态均来自内存缓存，不执行任何 SQL、不触发检查点
    return jsonify(
        {
            "id": exp.id,
            "a": exp.panel_state("a"),
            "b": exp.panel_state("b"),
            "checkpoint": exp.checkpoint_state(),
        }
    )


@app.get("/api/experiments/<exp_id>/committed")
def experiment_committed(exp_id):
    exp = get_experiment(exp_id)
    if exp is None:
        return jsonify({"error": "experiment not found"}), 404
    return jsonify({"committed": json_int(exp.committed_value())})


@app.post("/api/experiments/<exp_id>/checkpoint")
@app.post("/api/experiments/<exp_id>/checkpoint/<mode>")
def experiment_checkpoint(exp_id, mode=None):
    exp = get_experiment(exp_id)
    if exp is None:
        return jsonify({"error": "experiment not found"}), 404
    if mode is None:
        data = request.get_json(silent=True) or {}
        mode = data.get("mode")
    mode = (mode or "").upper()
    if mode not in VALID_CHECKPOINT_MODES:
        return jsonify({"error": "mode must be PASSIVE or TRUNCATE"}), 400
    return jsonify(run_checkpoint(exp, mode))


@app.post("/api/experiments/<exp_id>/conn/<side>/op")
def experiment_op(exp_id, side):
    exp = get_experiment(exp_id)
    if exp is None:
        return jsonify({"error": "experiment not found"}), 404
    if side not in ("a", "b"):
        return jsonify({"error": "unknown side"}), 400
    data = request.get_json(silent=True) or {}
    op = data.get("op")
    if op not in VALID_OPS:
        return jsonify({"error": "unknown op"}), 400
    value = None
    if op == "add":
        value = parse_int(data.get("value"))
        if value is None:
            return jsonify({"error": "value must be an integer"}), 400
        if abs(value) > MAX_ADD_ABS:
            return jsonify({"error": "value too large"}), 400
    return jsonify(run_op(exp, side, op, value))


def prepare_data_dir():
    # 实验无需跨重启保留：启动时清空旧数据库文件
    if os.path.isdir(DATA_DIR):
        shutil.rmtree(DATA_DIR, ignore_errors=True)
    os.makedirs(DATA_DIR, exist_ok=True)


prepare_data_dir()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, threaded=True)
