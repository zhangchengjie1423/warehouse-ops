import concurrent.futures
import csv
import http.client
import io
import json
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime,timedelta
from pathlib import Path
from app import make_server
from domain import Store,Problem,TZ,FIELDS,csv_bytes,iso,read_csv,p90
from demo import seed_demo

NOW=datetime(2026,10,21,12,tzinfo=TZ)
BASE=datetime(2026,10,20,10,tzinfo=TZ)

def order(identifier='O1',**changes):
    row=dict(zip(FIELDS['orders'],[identifier,'A','P1',iso(BASE),iso(BASE+timedelta(minutes=40)),iso(BASE+timedelta(minutes=5)),iso(BASE+timedelta(minutes=10)),iso(BASE+timedelta(minutes=15)),iso(BASE+timedelta(minutes=30)),12,'delivered','none']))
    row.update(changes)
    return row

def shift(identifier='S1',**changes):
    row=dict(zip(FIELDS['shifts'],[identifier,'P1','A',iso(BASE-timedelta(hours=2)),iso(BASE+timedelta(hours=6)),30,'picker']))
    row.update(changes)
    return row

def content(kind,rows):
    return csv_bytes(FIELDS[kind],rows).decode('utf-8-sig')

class DomainTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.store=Store(Path(self.tmp.name)/'test.db')

    def tearDown(self):
        self.tmp.cleanup()

    def add(self,kind,rows):
        return self.store.ingest(kind,content(kind,rows),'test.csv',True,NOW)

    def report(self):
        return self.store.report('2026-10-20','2026-10-20',now=NOW)

    def test_preview_does_not_write(self):
        r=self.store.ingest('orders',content('orders',[order()]),'orders.csv',False,NOW)
        self.assertTrue(r['valid']);self.assertEqual(self.store.metadata()['orders_count'],0)

    def test_commit_persists(self):
        self.assertTrue(self.add('orders',[order()])['committed'])
        self.assertEqual(Store(self.store.path).metadata()['orders_count'],1)

    def test_utf8_bom(self):
        r=self.store.ingest('orders',csv_bytes(FIELDS['orders'],[order()]).decode('utf-8'),'bom.csv',True,NOW)
        self.assertTrue(r['committed'])

    def test_atomic_invalid_batch(self):
        r=self.add('orders',[order(),order('O2',items='1.2')])
        self.assertEqual(r['error_count'],1);self.assertFalse(r['committed'])
        self.assertEqual(self.store.metadata()['orders_count'],0)

    def test_duplicate_idempotency(self):
        rows=[order(),order()]
        r=self.add('orders',rows)
        self.assertEqual((r['inserted'],r['skipped']),(1,1))
        self.assertEqual(self.add('orders',rows)['skipped'],2)

    def test_conflicting_key_blocks_other_rows(self):
        self.add('orders',[order()])
        r=self.add('orders',[order(items=99),order('O2')])
        self.assertFalse(r['valid']);self.assertEqual(self.store.metadata()['orders_count'],1)

    def test_concurrent_imports_are_idempotent(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures=[pool.submit(self.add,'orders',[order()]) for _ in range(2)]
            result=[f.result() for f in futures]
        self.assertEqual(sum(r['inserted'] for r in result),1)

    def test_headers_and_column_counts(self):
        with self.assertRaises(Problem):read_csv('orders','order_id,order_id\na,b',NOW)
        valid=content('orders',[order()]);bad=valid.rstrip() + ',extra\n'
        self.assertTrue(read_csv('orders',bad,NOW)[1])

    def test_invalid_numbers(self):
        for value in ('0','-1','NaN','1e3','True'):
            with self.subTest(value=value):self.assertFalse(self.add('orders',[order(items=value)])['valid'])

    def test_stage_sequence(self):
        for changes in ({'pick_started_at':''},{'pick_finished_at':iso(BASE)},{'delivered_at':''}):
            with self.subTest(changes=changes):self.assertFalse(self.add('orders',[order(**changes)])['valid'])

    def test_future_event_rejected(self):
        self.assertFalse(self.add('orders',[order(created_at=iso(NOW+timedelta(days=1)))])['valid'])

    def test_timezone_normalization(self):
        r=order(created_at='2026-10-20T02:00:00+00:00')
        self.add('orders',[r])
        self.assertEqual(self.report()['orders'][0]['created_at'],iso(BASE))

    def test_overlap_across_stations_and_roles(self):
        self.add('shifts',[shift()])
        r=self.add('shifts',[shift('S2',station='B',role='packer')])
        self.assertFalse(r['valid'])

    def test_back_to_back_shifts_allowed(self):
        r=self.add('shifts',[shift(end_at=iso(BASE)),shift('S2',start_at=iso(BASE))])
        self.assertTrue(r['valid'])

    def test_breaks_and_overnight(self):
        self.assertFalse(self.add('shifts',[shift(break_minutes=480)])['valid'])
        self.assertFalse(self.add('shifts',[shift(end_at=iso(BASE+timedelta(hours=16)))])['valid'])

    def test_rate_denominators_and_cancelled(self):
        self.add('orders',[order(),order('O2',delivered_at=iso(BASE+timedelta(minutes=50))),order('O3',status='in_progress',delivered_at=''),order('O4',status='cancelled',picker_id='',pick_started_at='',pick_finished_at='',packed_at='',delivered_at='')])
        r=self.report()['kpis']
        self.assertEqual((r['total'],r['active'],r['delivered'],r['on_time_rate']),(4,3,2,50))
        self.assertEqual(r['completion_rate'],66.67)
        self.assertEqual(r['overdue'],1)

    def test_exact_promise_is_on_time(self):
        self.add('orders',[order(delivered_at=iso(BASE+timedelta(minutes=40)))])
        self.assertEqual(self.report()['kpis']['on_time_rate'],100)

    def test_empty_metrics_are_null(self):
        k=self.report()['kpis']
        self.assertIsNone(k['on_time_rate']);self.assertIsNone(k['efficiency'])

    def test_weighted_productivity_and_idle_time(self):
        self.add('orders',[order(items=30),order('O2',picker_id='P2',items=20)])
        self.add('shifts',[shift(end_at=iso(BASE),break_minutes=0),shift('S2',employee_id='P2',end_at=iso(BASE-timedelta(hours=1)),break_minutes=0),shift('S3',employee_id='P3',end_at=iso(BASE-timedelta(hours=1)),break_minutes=0)])
        k=self.report()['kpis']
        self.assertEqual(k['hours'],4);self.assertEqual(k['efficiency'],12.5)

    def test_unmatched_orders_excluded(self):
        self.add('orders',[order(),order('O2',picker_id='P2')])
        self.add('shifts',[shift()])
        k=self.report()['kpis']
        self.assertEqual((k['matched_items'],k['unmatched_orders']),(12,1))
        self.assertEqual(k['efficiency'],1.6)

    def test_packer_hours_not_in_denominator(self):
        self.add('orders',[order()]);self.add('shifts',[shift(role='packer')])
        self.assertIsNone(self.report()['kpis']['efficiency'])

    def test_station_filter(self):
        self.add('orders',[order(),order('O2',station='B')])
        self.assertEqual(self.store.report('2026-10-20','2026-10-20','B',NOW)['kpis']['total'],1)

    def test_invalid_date_range(self):
        for start,end in [('2026-10-22','2026-10-20'),('2024-01-01','2026-10-20'),('invalid','2026-10-20')]:
            with self.assertRaises(Problem):self.store.report(start,end)

    def test_stage_metrics_and_p90(self):
        self.add('orders',[order()]);stages=self.report()['stages']
        self.assertEqual([s['mean'] for s in stages],[5,5,5,15]);self.assertEqual(p90(list(range(1,11))),9)

    def test_followup_persistence_and_version(self):
        self.add('orders',[order(issue='stockout')])
        data={'order_id':'O1','state':'resolved','owner':'演示组长','note':'已补货核对','version':0}
        self.store.followup(data)
        with self.assertRaises(Problem) as ctx:self.store.followup(data)
        self.assertEqual(ctx.exception.status,409)
        self.assertEqual(self.report()['kpis']['open_exceptions'],0)
        self.assertEqual(len(self.store.history('O1')),1)

    def test_resolution_needs_note_and_owner(self):
        self.add('orders',[order(issue='stockout')])
        for owner,note in [('','完成'),('A','')]:
            with self.assertRaises(Problem):self.store.followup({'order_id':'O1','state':'resolved','owner':owner,'note':note,'version':0})

    def test_normal_order_cannot_be_exception(self):
        self.add('orders',[order()])
        with self.assertRaises(Problem):self.store.followup({'order_id':'O1','state':'open','version':0})

    def test_forecast_formula_and_no_future_leakage(self):
        self.add('orders',[order(items=100)])
        result=self.store.staffing({'date':'2026-10-21','growth':20,'productivity':40,'utilization':75,'available':3})
        peak=result['hourly'][10]
        self.assertEqual((peak['forecast_items'],peak['required'],peak['gap']),(120,4,1))
        with self.assertRaises(Problem):self.store.staffing({'date':'2026-10-20'})

    def test_same_weekday_baseline(self):
        rows=[]
        for index,(days,items) in enumerate([(0,100),(7,200),(8,999)]):
            r=order(str(index),items=items)
            for key in ['created_at','promised_at','pick_started_at','pick_finished_at','packed_at','delivered_at']:
                r[key]=iso(datetime.fromisoformat(r[key])-timedelta(days=days))
            rows.append(r)
        self.add('orders',rows)
        r=self.store.staffing({'date':'2026-10-27','growth':0})
        self.assertEqual(r['method'],'同星期有单日均值')
        self.assertEqual(r['hourly'][10]['baseline_items'],150)

    def test_zero_capacity_parameter_rejected(self):
        for key in ['productivity','utilization']:
            with self.assertRaises(Problem):self.store.staffing({'date':'2026-10-21',key:0})

    def test_csv_injection_and_bom(self):
        raw=csv_bytes(['v'],[{'v':'=1+1'},{'v':'\t=1+1'},{'v':'normal'}])
        self.assertTrue(raw.startswith(bytes([239,187,191])))
        lines=list(csv.reader(io.StringIO(raw.decode('utf-8-sig'))))
        self.assertEqual(lines[1][0],"'=1+1");self.assertTrue(lines[2][0].startswith("'"))

    def test_demo_is_repeatable_without_overwrite(self):
        seed_demo(self.store);count=self.store.metadata()['orders_count']
        seed_demo(self.store)
        self.assertGreater(count,5000);self.assertEqual(self.store.metadata()['orders_count'],count)
        self.assertEqual(self.store.metadata()['mode'],'demo')

class HttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.store=Store(Path(self.tmp.name)/'test.db')
        self.server=make_server(self.store,0)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join();self.tmp.cleanup()

    def request(self,path,method='GET',data=None,headers=None):
        conn=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=5)
        body=json.dumps(data) if data is not None else None
        final={'Content-Type':'application/json','X-App-Token':self.server.token}
        final.update(headers or {})
        conn.request(method,path,body=body,headers=final)
        response=conn.getresponse();payload=response.read();status=response.status;head=dict(response.getheaders());conn.close()
        return status,payload,head

    def test_metadata_and_invalid_host(self):
        status,body,headers=self.request('/api/meta')
        self.assertEqual(status,200);self.assertIn('token',json.loads(body));self.assertIn('Content-Security-Policy',headers)
        self.assertEqual(self.request('/api/meta',headers={'Host':'evil.example'})[0],403)

    def test_write_token_and_origin(self):
        payload={'date':'2026-10-21'}
        self.assertEqual(self.request('/api/staffing','POST',payload,{'X-App-Token':'bad'})[0],403)
        self.assertEqual(self.request('/api/staffing','POST',payload,{'Origin':'https://evil.example'})[0],403)

    def test_malformed_object_returns_error(self):
        self.assertEqual(self.request('/api/import/preview','POST',{'kind':[],'content':'','filename':'x.csv'})[0],400)

    def test_import_through_api(self):
        payload={'kind':'orders','content':content('orders',[order()]),'filename':'orders.csv'}
        self.assertEqual(self.request('/api/import/preview','POST',payload)[0],200)
        self.assertEqual(self.store.metadata()['orders_count'],0)
        self.assertTrue(json.loads(self.request('/api/import/commit','POST',payload)[1])['committed'])
        self.assertEqual(self.store.metadata()['orders_count'],1)

    def test_backup_is_valid_sqlite(self):
        status,body,_=self.request('/api/backup')
        self.assertEqual(status,200)
        path=Path(self.tmp.name)/'backup.db';path.write_bytes(body)
        db=sqlite3.connect(path)
        try:
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0],'ok')
        finally:
            db.close()

    def test_template_and_path_traversal(self):
        status,body,_=self.request('/api/template?kind=orders')
        self.assertEqual(status,200);self.assertTrue(body.decode('utf-8-sig').startswith('order_id,'))
        self.assertEqual(self.request('/../domain.py')[0],404)

if __name__=='__main__':
    unittest.main(verbosity=2)
