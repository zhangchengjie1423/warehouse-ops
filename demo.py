"""Explicitly synthetic examples, generated relative to the first launch date."""
import random
from datetime import datetime, time, timedelta
from domain import TZ, FIELDS, clock, iso, csv_bytes

def demo_rows(today=None):
    rng = random.Random(261021)
    today = today or clock().date()
    orders, shifts = [], []
    for offset in range(28,0,-1):
        d = today-timedelta(days=offset)
        for code,station in [('E','演示·东湖站'),('W','演示·西城站')]:
            workers = [f'{code}{i:02d}' for i in range(1,7)]
            for i,emp in enumerate(workers):
                start = datetime.combine(d,time(8 if i<3 else 14),TZ)
                shifts.append(dict(zip(FIELDS['shifts'],[f'DEMO-{d}-{emp}',emp,station,iso(start),iso(start+timedelta(hours=8)),30,'picker'])))
            for i in range(rng.randint(90,135)):
                hour = rng.choices(list(range(8,21)),[4,6,13,17,11,5,6,8,10,16,19,9,4])[0]
                created = datetime.combine(d,time(hour,rng.randrange(60)),TZ)
                picker = rng.choice(workers[:3] if hour<14 else workers[3:])
                start = created+timedelta(minutes=rng.randint(1,9))
                finish = start+timedelta(minutes=rng.randint(3,11))
                packed = finish+timedelta(minutes=rng.randint(1,7))
                delivered = packed+timedelta(minutes=rng.randint(9,24))
                status = rng.choices(['delivered','cancelled','in_progress'],[94,4,2])[0]
                if offset>1 and status=='in_progress':
                    status='delivered'
                issue = rng.choices(['none','stockout','picking_error','damaged','delivery_delay'],[93,2,1,1,3])[0]
                stamps = [iso(start),iso(finish),iso(packed),iso(delivered)]
                if status=='cancelled':
                    stamps=['','','','']
                    picker=''
                elif status=='in_progress':
                    stamps[-1]=''
                orders.append(dict(zip(FIELDS['orders'],[f'DEMO-{d.strftime("%m%d")}-{code}-{i+1:04d}',station,picker,iso(created),iso(created+timedelta(minutes=40)),*stamps,rng.randint(2,14),status,issue])))
    return orders,shifts

def seed_demo(store):
    meta=store.metadata()
    if meta['orders_count'] or meta['shifts_count']:
        return
    orders,shifts=demo_rows()
    for kind,rows in [('orders',orders),('shifts',shifts)]:
        result=store.ingest(kind,csv_bytes(FIELDS[kind],rows).decode('utf-8-sig'),'generated-demo.csv',True)
        if not result['valid']:
            raise RuntimeError(result['errors'])
    with store.connect() as db:
        db.execute("UPDATE meta SET value='demo' WHERE key='mode'")

def sample_rows():
    d=clock().date()-timedelta(days=1)
    base=datetime.combine(d,time(10),TZ)
    order=dict(zip(FIELDS['orders'],['SAMPLE-001','样例站','SAMPLE-P01',iso(base),iso(base+timedelta(minutes=40)),iso(base+timedelta(minutes=4)),iso(base+timedelta(minutes=10)),iso(base+timedelta(minutes=14)),iso(base+timedelta(minutes=32)),12,'delivered','none']))
    shift=dict(zip(FIELDS['shifts'],['SAMPLE-S01','SAMPLE-P01','样例站',iso(base-timedelta(hours=2)),iso(base+timedelta(hours=6)),30,'picker']))
    return {'orders':[order],'shifts':[shift]}
