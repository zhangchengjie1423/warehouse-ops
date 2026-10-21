"""Warehouse Ops: CSV validation, SQLite persistence and operational metrics."""
from __future__ import annotations
import csv
import hashlib
import io
import math
import sqlite3
from collections import defaultdict
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

TZ = timezone(timedelta(hours=8))
ORDER_FIELDS = ['order_id','station','picker_id','created_at','promised_at','pick_started_at','pick_finished_at','packed_at','delivered_at','items','status','issue']
SHIFT_FIELDS = ['shift_id','employee_id','station','start_at','end_at','break_minutes','role']
FIELDS = {'orders': ORDER_FIELDS, 'shifts': SHIFT_FIELDS}
ISSUES = {'none':'无','stockout':'缺货','picking_error':'拣货差错','damaged':'货损','delivery_delay':'配送延误','other':'其他'}
STATUSES = {'delivered':'已送达','in_progress':'履约中','cancelled':'已取消'}

class Problem(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status

def clock():
    return datetime.now(TZ).replace(microsecond=0)

def plain(value, label, maximum=80, optional=False):
    if not isinstance(value,str):
        raise Problem(f'{label}必须为文本')
    value = value.strip()
    if (not value and not optional) or len(value)>maximum or any(ord(c)<32 for c in value):
        raise Problem(f'{label}不能为空、不能有控制字符且最多 {maximum} 字符')
    return value

def integer(value, label, lower=0, upper=100000):
    try:
        raw = str(value).strip()
        if not raw or not raw.isascii() or not raw.isdecimal():
            raise ValueError()
        n = int(raw)
        if not lower <= n <= upper:
            raise ValueError()
        return n
    except (ValueError,TypeError):
        raise Problem(f'{label}须为 {lower} 至 {upper} 的整数') from None

def decimal_number(value, label, low, high):
    try:
        n = Decimal(str(value))
        if not n.is_finite() or not Decimal(str(low)) <= n <= Decimal(str(high)):
            raise ValueError()
        return float(n)
    except (ValueError,TypeError,InvalidOperation):
        raise Problem(f'{label}须在 {low} 至 {high} 之间') from None

def day(value):
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat()!=value:
            raise ValueError()
        return parsed
    except (ValueError,TypeError):
        raise Problem('日期须为 YYYY-MM-DD') from None

def moment(value):
    try:
        if len(value)<16 or value[10] not in ('T',' '):
            raise ValueError()
        parsed = datetime.fromisoformat(value)
        if parsed.microsecond:
            raise ValueError()
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=TZ)
        return parsed.astimezone(TZ)
    except (ValueError,TypeError,OverflowError):
        raise Problem('时间须含日期和时分，格式 YYYY-MM-DD HH:MM[:SS]') from None

def iso(value):
    return value.isoformat(timespec='seconds')

def normalize(kind, row, now):
    if kind=='orders':
        optional = ('picker_id','pick_started_at','pick_finished_at','packed_at','delivered_at')
        r = {k:plain(row[k],k,80,k in optional) for k in ORDER_FIELDS}
        r['items'] = integer(r['items'],'商品件数',1,100000)
        if r['status'] not in STATUSES or r['issue'] not in ISSUES:
            raise Problem('status 或 issue 枚举值无效，请参考字段说明')
        stamps = {}
        for key in ('created_at','promised_at','pick_started_at','pick_finished_at','packed_at','delivered_at'):
            if r[key]:
                stamps[key] = moment(r[key])
                r[key] = iso(stamps[key])
                if key!='promised_at' and stamps[key]>now:
                    raise Problem(f'{key}不能晚于当前时间')
        if stamps['promised_at']<stamps['created_at']:
            raise Problem('承诺送达时间不能早于下单时间')
        previous, gap = stamps['created_at'], False
        for key in ('pick_started_at','pick_finished_at','packed_at','delivered_at'):
            if not r[key]:
                gap = True
            else:
                if gap or stamps[key]<previous:
                    raise Problem('作业时间须连续且按开拣、拣完、出仓、送达顺序递增')
                previous = stamps[key]
        if r['pick_started_at'] and not r['picker_id']:
            raise Problem('已有拣货时间的订单必须填写 picker_id')
        if (r['status']=='delivered')!=bool(r['delivered_at']):
            raise Problem('已送达状态必须有送达时间，其他状态不能有送达时间')
        return r
    r = {k:plain(row[k],k,80) for k in SHIFT_FIELDS}
    start,end = moment(r['start_at']),moment(r['end_at'])
    rest = integer(r['break_minutes'],'休息分钟',0,960)
    if end<=start or end>now or start.date()!=end.date():
        raise Problem('工时须在同一天结束、结束晚于开始且不能记录未来工时；跨日班次请拆分')
    minutes = (end-start).total_seconds()/60
    if minutes>960 or rest>=minutes:
        raise Problem('单条工时最多 16 小时，休息必须小于班次长度')
    if r['role'] not in ('picker','packer'):
        raise Problem('role 只能为 picker（拣货）或 packer（包装）')
    r.update(start_at=iso(start),end_at=iso(end),break_minutes=rest)
    return r

def read_csv(kind, content, now=None):
    if not isinstance(kind,str) or kind not in FIELDS:
        raise Problem('仅支持 orders 或 shifts')
    if not isinstance(content,str) or len(content.encode('utf-8'))>4_000_000:
        raise Problem('CSV 文件须小于 4 MB')
    reader = csv.DictReader(io.StringIO(content.lstrip(chr(65279)),newline=''),strict=True)
    try:
        headers = reader.fieldnames
        if headers is None or len(headers)!=len(set(headers)) or set(headers)!=set(FIELDS[kind]):
            raise Problem('表头不匹配，应包含：'+', '.join(FIELDS[kind]))
        rows,errors = [],[]
        for line,row in enumerate(reader,2):
            if line>10001:
                raise Problem('单次最多导入 10000 行')
            try:
                if None in row or any(v is None for v in row.values()):
                    raise Problem('列数与表头不一致')
                rows.append((line,normalize(kind,row,now or clock())))
            except Problem as exc:
                errors.append({'line':line,'message':str(exc)})
        if not rows and not errors:
            raise Problem('CSV 只有表头，没有数据')
        return rows,errors
    except csv.Error as exc:
        raise Problem('CSV 格式错误：'+str(exc)) from None

def csv_bytes(fields, rows):
    stream = io.StringIO(newline='')
    writer = csv.writer(stream)
    writer.writerow(fields)
    for row in rows:
        values = []
        for key in fields:
            value = row.get(key,'')
            if isinstance(value,str) and (value.lstrip().startswith(('=','+','-','@')) or value.startswith((chr(9),chr(13),chr(10)))):
                value = "'"+value
            values.append(value)
        writer.writerow(values)
    return stream.getvalue().encode('utf-8-sig')

def ratio(a,b):
    return round(a/b*100,2) if b else None

def average(values):
    return round(sum(values)/len(values),2) if values else None

def p90(values):
    return round(sorted(values)[math.ceil(len(values)*.9)-1],2) if values else None

class Store:
    def __init__(self,path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with self.connect() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                INSERT OR IGNORE INTO meta VALUES ('mode','empty');
                CREATE TABLE IF NOT EXISTS orders(
                    order_id TEXT PRIMARY KEY,station TEXT NOT NULL,picker_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,promised_at TEXT NOT NULL,pick_started_at TEXT NOT NULL,
                    pick_finished_at TEXT NOT NULL,packed_at TEXT NOT NULL,delivered_at TEXT NOT NULL,
                    items INTEGER NOT NULL CHECK(items>0),status TEXT NOT NULL,issue TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS order_day ON orders(created_at,station);
                CREATE TABLE IF NOT EXISTS shifts(
                    shift_id TEXT PRIMARY KEY,employee_id TEXT NOT NULL,station TEXT NOT NULL,
                    start_at TEXT NOT NULL,end_at TEXT NOT NULL,break_minutes INTEGER NOT NULL,role TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS imports(
                    id INTEGER PRIMARY KEY,kind TEXT NOT NULL,filename TEXT NOT NULL,
                    inserted INTEGER NOT NULL,skipped INTEGER NOT NULL,created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS followups(
                    order_id TEXT PRIMARY KEY REFERENCES orders(order_id),state TEXT NOT NULL,
                    owner TEXT NOT NULL,note TEXT NOT NULL,version INTEGER NOT NULL,updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS actions(
                    id INTEGER PRIMARY KEY,order_id TEXT NOT NULL REFERENCES orders(order_id),state TEXT NOT NULL,
                    owner TEXT NOT NULL,note TEXT NOT NULL,version INTEGER NOT NULL,created_at TEXT NOT NULL);
            ''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path,timeout=15)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def ingest(self,kind,content,filename,commit=False,now=None):
        rows,errors = read_csv(kind,content,now)
        filename = plain(filename.replace(chr(92),'/').split('/')[-1],'文件名',160)
        key = 'order_id' if kind=='orders' else 'shift_id'
        with self.connect() as db:
            if commit:
                db.execute('BEGIN IMMEDIATE')
            existing = {r[key]:dict(r) for r in db.execute(f'SELECT * FROM {kind}')}
            intervals = defaultdict(list)
            if kind=='shifts':
                for row in existing.values():
                    intervals[row['employee_id']].append(row)
            new,skipped = [],0
            for line,row in rows:
                if row[key] in existing:
                    if existing[row[key]]==row:
                        skipped += 1
                    else:
                        errors.append({'line':line,'message':f'{row[key]} 已存在且内容不同，不能覆盖'})
                    continue
                if kind=='shifts' and any(row['start_at']<x['end_at'] and row['end_at']>x['start_at'] for x in intervals[row['employee_id']]):
                    errors.append({'line':line,'message':f"{row['employee_id']} 工时重叠（含跨站点和岗位）"})
                    continue
                existing[row[key]] = row
                if kind=='shifts':
                    intervals[row['employee_id']].append(row)
                new.append(row)
            result = {'valid':not errors,'inserted':len(new),'skipped':skipped,
                      'errors':sorted(errors,key=lambda x:x['line'])[:100],'error_count':len(errors),
                      'sample':new[:5],'committed':False,'fingerprint':hashlib.sha256(content.encode()).hexdigest()}
            if commit and not errors:
                fields = FIELDS[kind]
                db.executemany(f"INSERT INTO {kind} ({','.join(fields)}) VALUES ({','.join('?' for _ in fields)})",[[r[k] for k in fields] for r in new])
                db.execute('INSERT INTO imports(kind,filename,inserted,skipped,created_at) VALUES (?,?,?,?,?)',(kind,filename,len(new),skipped,iso(now or clock())))
                db.execute("UPDATE meta SET value=CASE WHEN value IN ('demo','mixed') THEN 'mixed' ELSE 'imported' END WHERE key='mode'")
                result['committed'] = True
            return result

    def metadata(self):
        with self.connect() as db:
            dates = db.execute('SELECT min(substr(created_at,1,10)),max(substr(created_at,1,10)) FROM orders').fetchone()
            return {'mode':db.execute("SELECT value FROM meta WHERE key='mode'").fetchone()[0],
                    'stations':[r[0] for r in db.execute('SELECT station FROM orders UNION SELECT station FROM shifts ORDER BY station')],
                    'first_day':dates[0],'last_day':dates[1],'today':clock().date().isoformat(),
                    'orders_count':db.execute('SELECT count(*) FROM orders').fetchone()[0],
                    'shifts_count':db.execute('SELECT count(*) FROM shifts').fetchone()[0],
                    'imports':[dict(r) for r in db.execute('SELECT * FROM imports ORDER BY id DESC LIMIT 20')]}

    @staticmethod
    def reasons(row,now):
        result = []
        if row['status']=='cancelled':
            return result
        if row['delivered_at'] and row['delivered_at']>row['promised_at']:
            result.append('超时送达')
        elif not row['delivered_at'] and moment(row['promised_at'])<now:
            result.append('待履约超时')
        if row['issue']!='none':
            result.append(ISSUES[row['issue']])
        return list(dict.fromkeys(result))

    def report(self,start,end,station='',now=None):
        a,b = day(start),day(end)
        if b<a or (b-a).days>365:
            raise Problem('日期范围须为 1 至 366 天，结束日期不能早于开始日期')
        current = now or clock()
        with self.connect() as db:
            orders = [dict(r) for r in db.execute("SELECT * FROM orders WHERE substr(created_at,1,10) BETWEEN ? AND ? AND (?='' OR station=?) ORDER BY created_at DESC",(start,end,station,station))]
            picked = [dict(r) for r in db.execute("SELECT * FROM orders WHERE substr(pick_finished_at,1,10) BETWEEN ? AND ? AND status!='cancelled' AND (?='' OR station=?)",(start,end,station,station))]
            shifts = [dict(r) for r in db.execute("SELECT * FROM shifts WHERE substr(start_at,1,10) BETWEEN ? AND ? AND role='picker' AND (?='' OR station=?)",(start,end,station,station))]
            followups = {r['order_id']:dict(r) for r in db.execute('SELECT * FROM followups')}
        active = [r for r in orders if r['status']!='cancelled']
        delivered = [r for r in active if r['status']=='delivered']
        on_time = [r for r in delivered if r['delivered_at']<=r['promised_at']]
        exceptions = []
        for row in orders:
            row['reasons'] = self.reasons(row,current)
            row['late_minutes'] = max(0,round(((moment(row['delivered_at']) if row['delivered_at'] else current)-moment(row['promised_at'])).total_seconds()/60,1)) if row['status']!='cancelled' else 0
            if row['reasons']:
                follow = followups.get(row['order_id'],{'state':'open','owner':'','note':'','version':0,'updated_at':''})
                exceptions.append({**row,'followup':follow})
        workers,workdays = {},defaultdict(float)
        def worker(s,emp):
            return workers.setdefault((s,emp),{'station':s,'employee_id':emp,'hours':0,'orders':0,'items':0,'unmatched_items':0,'unmatched_orders':0})
        for row in shifts:
            hours = ((moment(row['end_at'])-moment(row['start_at'])).total_seconds()/60-row['break_minutes'])/60
            worker(row['station'],row['employee_id'])['hours'] += hours
            workdays[(row['station'],row['employee_id'],row['start_at'][:10])] += hours
        for row in picked:
            w = worker(row['station'],row['picker_id'])
            if workdays[(row['station'],row['picker_id'],row['pick_finished_at'][:10])]>0:
                w['orders'] += 1
                w['items'] += row['items']
            else:
                w['unmatched_items'] += row['items']
                w['unmatched_orders'] += 1
        total_hours = sum(w['hours'] for w in workers.values())
        matched_items = sum(w['items'] for w in workers.values())
        for w in workers.values():
            w['efficiency'] = round(w['items']/w['hours'],2) if w['hours'] else None
            w['hours'] = round(w['hours'],2)
        days = []
        for i in range((b-a).days+1):
            d = (a+timedelta(days=i)).isoformat()
            subset = [r for r in active if r['created_at'][:10]==d]
            done = [r for r in subset if r['status']=='delivered']
            good = sum(r['delivered_at']<=r['promised_at'] for r in done)
            days.append({'date':d,'orders':len(subset),'delivered':len(done),'on_time_rate':ratio(good,len(done))})
        stages = []
        for label,begin,finish in [('等待开拣','created_at','pick_started_at'),('拣货','pick_started_at','pick_finished_at'),('复核包装 / 待出仓','pick_finished_at','packed_at'),('配送','packed_at','delivered_at')]:
            values = [(moment(r[finish])-moment(r[begin])).total_seconds()/60 for r in active if r[begin] and r[finish]]
            stages.append({'label':label,'mean':average(values),'p90':p90(values),'samples':len(values)})
        hourly = [{'hour':h,'orders':sum(moment(r['created_at']).hour==h for r in active),'items':sum(r['items'] for r in active if moment(r['created_at']).hour==h)} for h in range(24)]
        return {'start':start,'end':end,'station':station,'as_of':iso(current),
                'kpis':{'total':len(orders),'active':len(active),'cancelled':len(orders)-len(active),'delivered':len(delivered),
                        'on_time':len(on_time),'on_time_rate':ratio(len(on_time),len(delivered)),
                        'completion_rate':ratio(len(delivered),len(active)),
                        'overdue':sum('待履约超时' in r['reasons'] for r in exceptions),
                        'exceptions':len(exceptions),'open_exceptions':sum(r['followup']['state']!='resolved' for r in exceptions),
                        'hours':round(total_hours,2),'matched_items':matched_items,
                        'efficiency':round(matched_items/total_hours,2) if total_hours else None,
                        'unmatched_orders':sum(w['unmatched_orders'] for w in workers.values())},
                'trend':days,'stages':stages,'hourly':hourly,
                'workers':sorted(workers.values(),key=lambda w:(-(w['efficiency'] or 0),w['employee_id'])),
                'orders':orders,'exceptions':sorted(exceptions,key=lambda r:-r['late_minutes'])}

    def followup(self,data):
        order_id = plain(data.get('order_id'),'订单编号')
        state = data.get('state')
        if state not in ('open','progress','resolved'):
            raise Problem('异常状态无效')
        owner = plain(data.get('owner',''),'跟进人',40,state=='open')
        note = plain(data.get('note',''),'处理记录',500,state!='resolved')
        version = integer(data.get('version'),'版本号')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            order = db.execute('SELECT * FROM orders WHERE order_id=?',(order_id,)).fetchone()
            if not order or not self.reasons(dict(order),clock()):
                raise Problem('订单不存在或当前无异常')
            old = db.execute('SELECT version FROM followups WHERE order_id=?',(order_id,)).fetchone()
            if (old['version'] if old else 0)!=version:
                raise Problem('记录已由其他页面更新，请刷新后重试',409)
            stamp = iso(clock())
            db.execute('INSERT INTO followups VALUES (?,?,?,?,?,?) ON CONFLICT(order_id) DO UPDATE SET state=excluded.state,owner=excluded.owner,note=excluded.note,version=excluded.version,updated_at=excluded.updated_at',(order_id,state,owner,note,version+1,stamp))
            db.execute('INSERT INTO actions(order_id,state,owner,note,version,created_at) VALUES (?,?,?,?,?,?)',(order_id,state,owner,note,version+1,stamp))
        return {'ok':True,'version':version+1}

    def history(self,order_id):
        with self.connect() as db:
            return [dict(r) for r in db.execute('SELECT * FROM actions WHERE order_id=? ORDER BY id DESC',(order_id,))]

    def staffing(self,data):
        target = day(data.get('date'))
        station = plain(data.get('station',''),'站点',80,True)
        growth = decimal_number(data.get('growth',0),'业务增长率',-80,300)
        productivity = decimal_number(data.get('productivity',60),'标准拣货效率',.1,10000)
        utilization = decimal_number(data.get('utilization',85),'有效作业比例',10,100)/100
        available = integer(data.get('available',3),'每小时可用人数',0,1000)
        a,b = target-timedelta(days=28),target-timedelta(days=1)
        with self.connect() as db:
            rows = [dict(r) for r in db.execute("SELECT * FROM orders WHERE substr(created_at,1,10) BETWEEN ? AND ? AND status!='cancelled' AND (?='' OR station=?)",(a.isoformat(),b.isoformat(),station,station))]
        days = sorted(set(r['created_at'][:10] for r in rows))
        weekdays = [d for d in days if day(d).weekday()==target.weekday()]
        selected = weekdays if len(weekdays)>=2 else days
        if not selected:
            raise Problem('计划日前 28 天无有效订单，先导入历史数据或选择有历史数据的计划日期')
        subset = [r for r in rows if r['created_at'][:10] in selected]
        hourly = []
        for h in range(24):
            baseline = sum(r['items'] for r in subset if moment(r['created_at']).hour==h)/len(selected)
            forecast = baseline*(1+growth/100)
            required = math.ceil(forecast/(productivity*utilization))
            hourly.append({'hour':h,'baseline_items':round(baseline,2),'forecast_items':round(forecast,2),'required':required,'available':available,'gap':max(0,required-available)})
        return {'date':target.isoformat(),'station':station,'growth':growth,'productivity':productivity,'utilization':utilization*100,'available':available,'sample_days':selected,
                'method':'同星期有单日均值' if len(weekdays)>=2 else '历史有单日均值',
                'peak_required':max(r['required'] for r in hourly),'required_hours':sum(r['required'] for r in hourly),
                'gap_hours':sum(r['gap'] for r in hourly),'hourly':hourly}

    def export(self,kind,start,end,station=''):
        report = self.report(start,end,station)
        if kind=='orders':
            return csv_bytes(ORDER_FIELDS,report['orders'])
        if kind=='workers':
            return csv_bytes(['station','employee_id','hours','orders','items','efficiency','unmatched_orders'],report['workers'])
        if kind=='exceptions':
            rows = [{**r,'reasons':' / '.join(r['reasons']),**{k:r['followup'][k] for k in ('state','owner','note')}} for r in report['exceptions']]
            return csv_bytes(['order_id','station','reasons','late_minutes','state','owner','note'],rows)
        if kind=='trend':
            return csv_bytes(['date','orders','delivered','on_time_rate'],report['trend'])
        raise Problem('不支持的导出类型')
