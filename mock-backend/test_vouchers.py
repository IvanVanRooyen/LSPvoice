"""Authenticated synthetic voucher lifecycle regression cases."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient
import main
from vouchers import VoucherPolicy

class VoucherTests(unittest.TestCase):
    def setUp(self):
        auth=patch.object(main,'BEARER','voucher-test');auth.start();self.addCleanup(auth.stop)
        self.now=datetime(2026,11,23,10,tzinfo=main.BRISBANE)
        clock=patch.object(main,'_brisbane_now',side_effect=lambda:self.now);clock.start();self.addCleanup(clock.stop)
        self.client=TestClient(main.app);self.headers={'Authorization':'Bearer voucher-test'}
        self.store=main.app.state.vouchers
        for d in (self.store.records,self.store.claims,self.store.requests):d.clear()
        self.store.policy=VoucherPolicy()
    def details(self,**kw):
        body=dict(hospital='GH',stay_reference='mock-stay-001',bed_id='A101',route='A',
                  eligibility_reason='no_wallet',reason='My wallet is at home',contact_name='Synthetic Caller',
                  address='1 Example Street, Brisbane 4000',email='caller@example.invalid',ward='A',
                  mobile='0400000123',details_confirmed=True,terms_agreed=True,request_id='issue-1')
        return dict(body,**kw)
    def post(self,path,body):return self.client.post(path,json=body,headers=self.headers)
    def issue(self,**kw):
        r=self.post('/mock/vouchers/issue',self.details(**kw));self.assertEqual(r.status_code,200,r.text);return r.json()
    def status(self,vid):return self.client.get('/mock/vouchers/'+vid,headers=self.headers).json()
    def event(self,vid,name,success=True):return self.post(f'/mock/vouchers/{vid}/{name}',{'success':success}).json()
    def sms(self,vid,rid='sms-1',**kw):
        return self.post(f'/mock/vouchers/{vid}/sms',dict(dict(mobile='0400000123',mobile_confirmed=True,deadline_terms_confirmed=True,confirmed_pay_by=self.status(vid)['pay_by'],request_id=rid),**kw))
    def test_auth_all_endpoints(self):
        for path,body in [('/mock/vouchers/issue',self.details()),('/mock/vouchers/x/sms',dict(mobile='0400000123',mobile_confirmed=True,request_id='s')),('/mock/vouchers/x/redeem',{}),('/mock/vouchers/x/payment',{})]:
            self.assertEqual(self.client.post(path,json=body).status_code,401)
            self.assertEqual(self.client.post(path,json=body,headers={'Authorization':'Bearer wrong'}).status_code,401)
        self.assertEqual(self.client.get('/mock/vouchers/x').status_code,401)
    def test_stay_resolver_does_not_guess_ambiguous_bed(self):
        def resolve(**kw):return self.post('/mock/stays/resolve',dict(hospital='GH',**kw)).json()
        self.assertFalse(resolve(bed_id='A101')['stay_resolved'])
        self.assertFalse(resolve(bed_id='A999')['stay_resolved'])
        self.assertFalse(resolve(bed_id='A102',device_id='other')['stay_resolved'])
        self.assertEqual(resolve(bed_id='a102',device_id='bed-a102')['stay_reference'],'mock-stay-001')
        self.assertEqual(resolve(bed_id='A101',device_id='mock-screen-002')['stay_reference'],'mock-stay-002')
        self.assertEqual(resolve(device_id='mock-screen-003')['bed_id'],'A103')
        self.assertEqual(self.client.post('/mock/stays/resolve',json={'hospital':'GH','bed_id':'A102'}).status_code,401)
    def test_sms_requires_full_terms_for_current_issued_deadline(self):
        vid=self.issue()['voucher_id']
        self.assertEqual(self.sms(vid,deadline_terms_confirmed=False).status_code,422)
        self.assertEqual(self.sms(vid,confirmed_pay_by='2099-01-01T00:00:00+10:00').status_code,422)
        self.assertEqual(self.sms(vid,'{{call_id}}:sms').status_code,422)
        self.assertEqual(self.status(vid)['sms_attempts'],0)
        self.assertTrue(self.sms(vid).json()['sms_accepted'])
    def test_issue_not_activation_or_sms(self):
        r=self.issue();self.assertTrue(r['approved']);self.assertFalse(r['service_active']);self.assertIsNone(r['ends_at'])
        self.assertFalse(r['sms_accepted']);self.assertEqual(r['sms_attempts'],0)
        self.assertEqual(r['pay_by'],(self.now+timedelta(hours=24)).isoformat())
    def test_redemption_starts_full_day_and_keeps_due(self):
        r=self.issue();self.now+=timedelta(hours=2);a=self.event(r['voucher_id'],'redeem')
        self.assertTrue(a['service_active']);self.assertEqual(a['pay_by'],r['pay_by'])
        self.assertEqual(a['activated_at'],self.now.isoformat());self.assertEqual(a['ends_at'],(self.now+timedelta(hours=24)).isoformat())
    def test_same_patient_stay_after_bed_move(self):
        self.issue();self.assertEqual(self.issue(bed_id='A102',request_id='other')['reason'],'patient_stay_already_used')
        self.assertEqual(len(self.store.records),1)
    def test_different_patient_reuses_bed(self):
        self.issue();self.assertTrue(self.issue(stay_reference='mock-stay-002',request_id='new')['approved'])
    def test_readmission(self):
        self.issue();self.assertTrue(self.issue(stay_reference='mock-stay-003',bed_id='A103',request_id='readmit')['approved'])
    def test_unknown_mismatched_identity(self):
        for kw in [dict(stay_reference='unknown'),dict(bed_id='bad'),dict(hospital='Mercy')]:self.assertFalse(self.issue(**kw)['approved'])
        self.assertFalse(self.store.records)
        self.assertEqual(self.post('/mock/vouchers/issue',self.details(patient_id='invented')).status_code,422)
    def test_required_details_and_consent(self):
        for key in ['contact_name','address','email','ward','reason','request_id']:
            self.assertEqual(self.post('/mock/vouchers/issue',self.details(**{key:'  '})).status_code,422,key)
        for kw in [dict(terms_agreed=False),dict(details_confirmed=False),dict(email='bad'),dict(mobile='bad')]:self.assertFalse(self.issue(**kw)['approved'])
        self.assertFalse(self.store.records)
    def test_reason_and_route_validation(self):
        for kw in [dict(eligibility_reason='other'),dict(route='C'),dict(request_id='{{call_id}}:voucher-issue')]:self.assertEqual(self.post('/mock/vouchers/issue',self.details(**kw)).status_code,422)
    def test_route_a_needs_patient_mobile(self):self.assertFalse(self.issue(mobile='')['approved'])
    def test_route_b_without_patient_mobile_paid_before_activation(self):
        self.assertFalse(self.issue(route='B',mobile='')['approved'])
        r=self.issue(route='B',mobile='',payer_name='Synthetic Payer',payer_mobile='0400000456')
        self.assertTrue(r['approved']);self.assertFalse(self.event(r['voucher_id'],'redeem')['service_active'])
        self.assertTrue(self.event(r['voucher_id'],'payment')['service_active'])
    def test_route_a_payment_not_redemption(self):
        r=self.event(self.issue()['voucher_id'],'payment');self.assertFalse(r['service_active']);self.assertIsNone(r['redeemed_at'])
    def test_idempotent_issue_keeps_original_deadline(self):
        a=self.issue();self.now+=timedelta(hours=1);b=self.issue()
        self.assertEqual(a['voucher_id'],b['voucher_id']);self.assertEqual(a['pay_by'],b['pay_by']);self.assertEqual(len(self.store.records),1)
    def test_issue_key_payload_conflict(self):
        self.issue();self.assertEqual(self.post('/mock/vouchers/issue',self.details(contact_name='Other')).status_code,409)
    def test_concurrent_independent_calls_only_one_benefit(self):
        with ThreadPoolExecutor(max_workers=6) as pool:
            results=list(pool.map(lambda i:self.post('/mock/vouchers/issue',self.details(request_id=f'call-{i}')).json(),range(12)))
        self.assertEqual(sum(r['approved'] for r in results),1)
    def test_concurrent_retries_share_same_voucher(self):
        with ThreadPoolExecutor(max_workers=6) as pool:
            results=list(pool.map(lambda _:self.post('/mock/vouchers/issue',self.details()).json(),range(12)))
        self.assertTrue(all(r['approved'] for r in results))
        self.assertEqual(len({r['voucher_id'] for r in results}),1)
        self.assertEqual(len(self.store.records),1)
    def test_initial_sms_uses_confirmed_issue_recipient(self):
        vid=self.issue()['voucher_id']
        self.assertEqual(self.sms(vid,mobile='0400000456').status_code,422)
        self.assertEqual(self.status(vid)['sms_attempts'],0)
        other=self.issue(stay_reference='mock-stay-002',request_id='family',route='B',eligibility_reason='family_agreed',mobile='',payer_name='Synthetic Payer',payer_mobile='0400000456')
        self.assertTrue(self.sms(other['voucher_id'],mobile='0400000456').json()['sms_accepted'])
    def test_sms_acceptance_not_delivery_or_activation(self):
        vid=self.issue()['voucher_id'];r=self.sms(vid).json()
        self.assertTrue(r['sms_accepted']);self.assertFalse(r['delivery_confirmed']);self.assertEqual(r['mobile_last3'],'123')
        self.assertFalse(self.status(vid)['service_active'])
    def test_sms_failure_retry_limit_same_voucher(self):
        r=self.issue();vid=r['voucher_id'];a=self.sms(vid,simulate_failure=True).json()
        self.assertFalse(a['sms_accepted']);self.assertEqual(a,self.sms(vid,simulate_failure=True).json())
        self.assertEqual(self.status(vid)['sms_attempts'],1)
        self.assertTrue(self.sms(vid,'s2',simulate_failure=True).json()['needs_helpdesk'])
        self.assertEqual(self.sms(vid,'s3').json()['reason'],'resend_limit')
        self.assertEqual(self.status(vid)['pay_by'],r['pay_by']);self.assertEqual(len(self.store.records),1)
    def test_sms_corrected_recipient_and_changed_retry_guard(self):
        vid=self.issue()['voucher_id'];self.sms(vid)
        self.assertEqual(self.sms(vid,'s2',mobile='0400000456').json()['mobile_last3'],'456')
        self.assertEqual(self.sms(vid,mobile='0400000789').status_code,409)
        self.assertEqual(self.sms(vid,'s3',mobile_confirmed=False).status_code,422)
    def test_failed_and_repeated_events_do_not_extend(self):
        vid=self.issue()['voucher_id'];self.assertFalse(self.event(vid,'redeem',False)['event_accepted'])
        a=self.event(vid,'redeem');self.now+=timedelta(hours=1)
        self.assertEqual(self.event(vid,'redeem')['ends_at'],a['ends_at']);self.assertIsNone(self.event(vid,'payment',False)['paid_at'])
    def test_deadline_boundary_and_unresolved_overdue(self):
        r=self.issue();vid=r['voucher_id'];start=self.now
        for delta in [timedelta(hours=24)-timedelta(microseconds=1),timedelta(hours=24)]:
            self.now=start+delta;self.assertFalse(self.status(vid)['deadline_passed'])
        self.now+=timedelta(microseconds=1);a=self.event(vid,'redeem')
        self.assertFalse(a['event_accepted']);self.assertTrue(a['policy_review_required']);self.assertIsNone(a['service_active']);self.assertIsNone(a['redeemed_at'])
    def test_future_policy_does_not_rewrite_issued_deadline(self):
        old=self.issue();self.store.policy=VoucherPolicy(payment_clock='redemption',version='mock-experimental')
        new=self.issue(stay_reference='mock-stay-002',request_id='new-policy');self.assertIsNone(new['pay_by'])
        self.now+=timedelta(hours=1)
        self.assertEqual(self.event(old['voucher_id'],'redeem')['pay_by'],old['pay_by'])
        self.assertEqual(self.event(new['voucher_id'],'redeem')['pay_by'],(self.now+timedelta(hours=24)).isoformat())
    def test_unknown_voucher(self):
        self.assertEqual(self.client.get('/mock/vouchers/x',headers=self.headers).status_code,404)
        self.assertEqual(self.post('/mock/vouchers/x/redeem',{}).status_code,404)
    def test_paid_service_ends_without_extension(self):
        vid=self.issue()['voucher_id'];a=self.event(vid,'redeem');self.event(vid,'payment');self.now+=timedelta(hours=24)
        self.assertFalse(self.status(vid)['service_active']);self.assertEqual(self.status(vid)['ends_at'],a['ends_at'])

if __name__=='__main__':unittest.main()
